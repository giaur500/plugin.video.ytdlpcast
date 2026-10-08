# -*- coding: utf-8 -*-
"""Kodi side of casting: the controller thread, the player, the TV code.

The protocol lives in cast_lounge, cast_discovery and cast_receiver, free of
xbmc; this module wires them to Kodi.

Threads. Kodi hands Player and Monitor callbacks only to the thread that created
the object, and only while that thread sits in waitForAbort (CallbackHandler.cpp,
PythonCallbackHandler::isStateOk). So one controller thread creates both, pumps
them, and is the only one to touch the receiver: the lounge listener just queues
what arrives, callbacks just queue what happened. TubeCast ran a player thread,
a volume thread, and a reporting loop inside onPlayBackStarted that blocked every
other callback until the video ended.
"""

import json
import queue
import threading
import time
import urllib.parse
import uuid

import xbmc
import xbmcaddon
import xbmcgui

from . import cast_discovery, cast_lounge, cast_receiver, diag, kodilog, paths
from .cast_protocol import STATUS_PAUSED, STATUS_PLAYING, STATUS_STOPPED

ADDON_ID = xbmcaddon.Addon().getAddonInfo("id")
HOME = xbmcgui.Window(10000)

# Shared between the service (which casts) and the plugin (which resolves and
# shows the TV code dialog) -- separate interpreters, so window properties.
PROP_ONLINE = ADDON_ID + ".cast.online"
PROP_REMOTES = ADDON_ID + ".cast.remotes"
PROP_RESOLVED = ADDON_ID + ".cast.resolved"

log = diag.logger("cast.kodi")


def read_settings():
    addon = xbmcaddon.Addon()  # fresh: the settings object caches values otherwise
    return {
        "enabled": addon.getSettingBool("cast_enabled"),
        "discovery": addon.getSettingBool("cast_discovery"),
        "name": addon.getSettingString("cast_name").strip(),
    }


def device_name(configured):
    """What the phone lists: the setting, else Kodi's own name with "(yt-dlp)".

    The suffix keeps the entry apart from TubeCast's, which uses the bare name.
    """
    if configured:
        return configured
    return "{} (yt-dlp)".format(xbmc.getInfoLabel("System.FriendlyName").strip() or "Kodi")


def string(string_id):
    return xbmcaddon.Addon().getLocalizedString(string_id)


def notify(message, icon=xbmcgui.NOTIFICATION_INFO, time_ms=4000):
    xbmcgui.Dialog().notification(xbmcaddon.Addon().getAddonInfo("name"), message, icon, time_ms, False)


def json_rpc(method, params=None):
    reply = json.loads(xbmc.executeJSONRPC(json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}})))
    if "error" in reply:
        log.warning("%s failed: %s", method, reply["error"])
        return None
    return reply.get("result")


def mark_resolved(nonce, result):
    """Called by the plugin: tell the controller how resolving its request went."""
    HOME.setProperty(PROP_RESOLVED, "{}:{}".format(nonce, result))


class _Player(xbmc.Player):
    """Only queues what happened; the controller decides what it means."""

    def __init__(self, events):
        super().__init__()
        self.events = events

    def onAVStarted(self):
        self.events.put(("started",))

    def onPlayBackPaused(self):
        self.events.put(("paused",))

    def onPlayBackResumed(self):
        self.events.put(("resumed",))

    def onPlayBackSeek(self, time, seekOffset):  # noqa: A002, N803 - Kodi's signature
        self.events.put(("seeked",))

    def onPlayBackStopped(self):
        self.events.put(("stopped",))

    def onPlayBackEnded(self):
        self.events.put(("ended",))

    def onPlayBackError(self):
        self.events.put(("error",))


class _Monitor(xbmc.Monitor):
    def __init__(self, events):
        super().__init__()
        self.events = events

    def onNotification(self, sender, method, data):
        if method == "Application.OnVolumeChanged":
            try:
                volume = json.loads(data)
                self.events.put(("volume", int(volume["volume"]), bool(volume["muted"])))
            except (ValueError, KeyError, TypeError):
                log.warning("unreadable volume notification: %r", data)


class KodiPlayback:
    """The player interface cast_receiver expects, on top of Kodi's player."""

    def __init__(self, player):
        self.player = player
        self.nonce = None

    def play(self, video_id, seconds):
        # The nonce comes back from the plugin once it has resolved the video,
        # so the controller knows the item that starts next is the one it asked
        # for -- and not something the user picked in Kodi meanwhile.
        self.nonce = uuid.uuid4().hex[:12]
        HOME.clearProperty(PROP_RESOLVED)
        url = "plugin://{}/?{}".format(ADDON_ID, urllib.parse.urlencode(
            {"video_id": video_id, "seek": int(seconds), "cast": self.nonce}))
        log.info("Kodi plays %s", url)
        self.player.play(url)

    def resolution(self, consume=False):
        """'ok', 'failed: …' or None while the plugin is still resolving."""
        value = HOME.getProperty(PROP_RESOLVED)
        if not self.nonce or not value.startswith(self.nonce + ":"):
            return None
        if consume:
            HOME.clearProperty(PROP_RESOLVED)
            self.nonce = None
        return value.split(":", 1)[1]

    def pause(self):
        if xbmc.getCondVisibility("Player.Playing"):
            self.player.pause()  # a toggle in Kodi

    def resume(self):
        if xbmc.getCondVisibility("Player.Paused"):
            self.player.pause()

    def stop(self):
        self.player.stop()

    def seek(self, seconds):
        self.player.seekTime(float(seconds))

    def status(self):
        if not self.player.isPlaying():
            return STATUS_STOPPED, 0, 0
        try:
            position, duration = self.player.getTime(), self.player.getTotalTime()
        except RuntimeError:  # stopped between the check and the call
            return STATUS_STOPPED, 0, 0
        state = STATUS_PAUSED if xbmc.getCondVisibility("Player.Paused") else STATUS_PLAYING
        return state, position, duration

    def volume(self):
        result = json_rpc("Application.GetProperties", {"properties": ["volume", "muted"]}) or {}
        return int(result.get("volume", 100)), bool(result.get("muted", False))

    def set_volume(self, level, muted):
        json_rpc("Application.SetVolume", {"volume": max(0, min(100, level))})
        json_rpc("Application.SetMute", {"mute": muted})

    def notify(self, name, event):
        notify(string(30191 if event == "connected" else 30192).format(name))


class CastController(threading.Thread):
    TICK_BUSY = 0.25  # a phone connected or something playing
    TICK_IDLE = 1.0
    STARTUP_WAIT = 30
    NETWORK_WAIT = 120

    def __init__(self, settings):
        super().__init__(name="ytdlpcast-cast", daemon=True)
        self.settings = settings
        self.events = queue.Queue()
        self._stopping = threading.Event()

    def stop(self):
        self._stopping.set()

    def run(self):
        try:
            self._run()
        except Exception:  # noqa: BLE001 - say so loudly, but never take the service down
            log.exception("cast controller crashed")
        finally:
            HOME.clearProperty(PROP_ONLINE)
            HOME.clearProperty(PROP_REMOTES)

    def _wait_for(self, monitor, condition, timeout, what):
        """Serph91P's startup fix: Kodi's name and the network come up late on boot."""
        started = time.monotonic()
        while not condition():
            if self._stopping.is_set() or monitor.waitForAbort(1):
                return False
            if time.monotonic() - started > timeout:
                log.warning("still waiting for %s after %ds, starting anyway", what, timeout)
                return True
        log.info("%s ready after %ds", what, time.monotonic() - started)
        return True

    def _run(self):
        monitor = _Monitor(self.events)  # created here: their callbacks arrive here
        player = _Player(self.events)
        if not self._wait_for(monitor, lambda: xbmc.getInfoLabel("System.FriendlyName"),
                              self.STARTUP_WAIT, "Kodi"):
            return
        if not self._wait_for(monitor, lambda: xbmc.getIPAddress() not in ("", "127.0.0.1", "0.0.0.0"),
                              self.NETWORK_WAIT, "the network"):
            return

        name = device_name(self.settings["name"])
        identity = cast_lounge.Identity.load(paths.cast_state_file())
        self._check_tubecast(identity)
        session = cast_lounge.LoungeSession(identity, name)
        playback = KodiPlayback(player)
        receiver = cast_receiver.CastReceiver(session, playback)
        worker = cast_lounge.LoungeWorker(session, lambda commands: self.events.put(("commands", commands)),
                                          lambda online: self.events.put(("online", online)))
        dial = ssdp = None
        if self.settings["discovery"]:
            dial, ssdp = self._start_discovery(receiver, identity)
        worker.start()
        log.log(diag.NOTICE, "casting on as \"%s\", local discovery %s", name, "on" if dial else "off")
        log.info("device %s", identity.device_id)
        try:
            while not self._stopping.is_set():
                busy = receiver.remotes or receiver.active or receiver.pending
                if monitor.waitForAbort(self.TICK_BUSY if busy else self.TICK_IDLE):
                    break
                self._drain(receiver, playback)
                receiver.tick()
        finally:
            worker.stop()
            if ssdp:
                ssdp.stop()
            if dial:
                dial.stop()
            worker.join(5)
            log.log(diag.NOTICE, "casting off")

    def _start_discovery(self, receiver, identity):
        dial = cast_discovery.DialServer(receiver)
        try:
            port = dial.start()
        except OSError:
            log.exception("DIAL server could not start; only the TV code will work")
            return None, None
        ssdp = cast_discovery.SsdpResponder(identity.device_id, port)
        try:
            ssdp.start()
        except OSError:
            log.exception("SSDP could not bind port %d; the phone will not find Kodi by itself",
                          cast_discovery.SSDP_PORT)
            ssdp = None
        return dial, ssdp

    def _check_tubecast(self, identity):
        if not xbmc.getCondVisibility("System.AddonIsEnabled(script.tubecast)"):
            return
        log.warning("TubeCast is enabled too: the phone will list two receivers for this box")
        if not identity.tubecast_warned:
            notify(string(30198), xbmcgui.NOTIFICATION_WARNING, 10000)
            identity.tubecast_warned = True
            identity.save()

    def _drain(self, receiver, playback):
        failed = playback.resolution()
        if failed and failed != "ok":
            receiver.on_failed(playback.resolution(consume=True))
        while True:
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                return
            kind = event[0]
            if kind == "commands":
                receiver.handle(event[1])
                HOME.setProperty(PROP_REMOTES, str(len(receiver.remotes)))
            elif kind == "online":
                HOME.setProperty(PROP_ONLINE, "1" if event[1] else "")
                log.log(diag.NOTICE, "screen %s", "online at YouTube" if event[1] else "offline")
            elif kind == "started":
                receiver.on_started(playback.resolution(consume=True) == "ok")
            elif kind == "paused":
                receiver.on_paused()
            elif kind == "resumed":
                receiver.on_resumed()
            elif kind == "seeked":
                receiver.on_seeked()
            elif kind == "stopped":
                receiver.on_stopped()
            elif kind == "ended":
                receiver.on_ended()
            elif kind == "error":
                receiver.on_failed("Kodi reported a playback error")
            elif kind == "volume":
                receiver.on_volume_changed(event[1], event[2])


def start(settings):
    """Start casting per the settings; return the controller, or None when off."""
    if not settings["enabled"]:
        log.log(diag.NOTICE, "casting is switched off in the settings")
        return None
    controller = CastController(settings)
    controller.start()
    return controller


def stop(controller):
    if controller is not None:
        controller.stop()
        controller.join(10)


def pair_with_tv_code():
    """Settings button: show a TV code until a phone links, the user cancels or 5 minutes pass.

    Runs in the plugin, not the service: it only needs the identity file the
    service keeps and the window properties it publishes.
    """
    settings = read_settings()
    kodilog.apply()
    if not settings["enabled"]:
        return notify(string(30193), xbmcgui.NOTIFICATION_WARNING)
    if HOME.getProperty(PROP_ONLINE) != "1":
        return notify(string(30194), xbmcgui.NOTIFICATION_WARNING)
    identity = cast_lounge.Identity.load(paths.cast_state_file())
    try:
        code = cast_lounge.get_pairing_code(cast_lounge.HttpTransport(), identity, device_name(settings["name"]))
    except Exception as error:  # noqa: BLE001 - shown to the user, logged with its cause
        log.exception("getting a TV code failed")
        return notify(string(30195).format(error), xbmcgui.NOTIFICATION_ERROR, 8000)
    log.info("TV code %s shown", code)

    linked_before = int(HOME.getProperty(PROP_REMOTES) or 0)
    text = "{}[CR][CR][B]{}[/B]".format(string(30196), code)
    dialog = xbmcgui.DialogProgress()
    dialog.create(string(30187), text)
    monitor = xbmc.Monitor()
    timeout = 300
    deadline = time.monotonic() + timeout
    try:
        while not monitor.abortRequested() and not dialog.iscanceled():
            left = deadline - time.monotonic()
            if left <= 0:
                break
            if int(HOME.getProperty(PROP_REMOTES) or 0) > linked_before:
                notify(string(30197))
                log.info("a phone linked with the TV code")
                break
            dialog.update(int(100 * left / timeout), text)
            monitor.waitForAbort(1)
    finally:
        dialog.close()
