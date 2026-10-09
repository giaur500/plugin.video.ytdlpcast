# -*- coding: utf-8 -*-
"""Kodi side of the web interface: the backend, the PIN, the settings lines.

The backend speaks JSON-RPC (xbmc.executeJSONRPC, safe from any thread) for
everything about the player and the playlist. Playing a link goes through the
plugin (?action=play_url, by RunPlugin) rather than here, so yt-dlp is never
loaded into the long-lived service.
"""

import json
import os
import random
import secrets
import threading
import urllib.parse

import xbmc
import xbmcaddon
import xbmcvfs

from . import diag, fileutil, kodiutil, playback_diag, request_state, web_server
from .kodiutil import ADDON_ID, json_rpc

ADDRESS_SETTING = "web_address"
PIN_SETTING = "web_pin"
VIDEO_PLAYLIST = 1
MAX_URL = 2048

log = diag.logger("web")


def _seconds(value):
    if not isinstance(value, dict):
        return 0.0
    return (value.get("hours", 0) * 3600 + value.get("minutes", 0) * 60 + value.get("seconds", 0)
            + value.get("milliseconds", 0) / 1000.0)


def _time(seconds):
    seconds = max(0.0, float(seconds))
    whole = int(seconds)
    return {"hours": whole // 3600, "minutes": whole // 60 % 60, "seconds": whole % 60,
            "milliseconds": int((seconds - whole) * 1000)}


def art_url(value):
    """Kodi's "image://<url-encoded>/" back into a URL a browser can load, or None."""
    if not value:
        return None
    if value.startswith("image://"):
        value = urllib.parse.unquote(value[len("image://"):].rstrip("/"))
    return value if value.startswith(("http://", "https://")) else None


class KodiBackend:
    """What web_server needs from Kodi."""

    def _video_player(self):
        players = json_rpc("Player.GetActivePlayers") or []
        return next((p["playerid"] for p in players if p.get("type") in ("video", "audio")), None)

    def status(self, request=None):
        result = {"state": "stopped"}
        player = self._video_player()
        if player is not None:
            props = json_rpc("Player.GetProperties", {"playerid": player, "properties": [
                "time", "totaltime", "speed", "live", "playlistid", "position"]})
            item = (json_rpc("Player.GetItem", {"playerid": player, "properties": ["title", "art"]})
                    or {}).get("item", {})
            title = item.get("title") or item.get("label") or ""
            result = {
                "state": "playing" if props.get("speed") else "paused",
                "title": title,
                "time": _seconds(props.get("time")),
                "duration": _seconds(props.get("totaltime")),
                "live": bool(props.get("live")),
                "thumbnail": art_url((item.get("art") or {}).get("thumb")),
                "position": props.get("position", -1),
                "playlist": props.get("playlistid", -1),
            }
            if xbmc.getCondVisibility("Player.Caching"):
                result["buffering"] = True
            source = playback_diag.last_source()
            if source and source.get("title") == title:
                result.update(source=source.get("source"), site=source.get("site"))
        size = (json_rpc("Playlist.GetProperties", {"playlistid": VIDEO_PLAYLIST, "properties": ["size"]}) or {})
        result["queue_size"] = size.get("size", 0)
        if request:
            result["request"] = request_state.peek(request) or "pending"
        return result

    def play(self, url, mode):
        url = (url or "").strip()
        if not url.startswith(("http://", "https://")) or len(url) > MAX_URL:
            raise ValueError("not a web link")
        if mode not in ("now", "queue"):
            raise ValueError("mode must be now or queue")
        nonce = request_state.new_nonce()
        target = "plugin://{}/?{}".format(ADDON_ID, urllib.parse.urlencode(
            {"action": "play_url", "url": url, "mode": mode, "req": nonce}))
        log.info("play (%s) %s", mode, diag.safe_url(url))
        xbmc.executebuiltin('RunPlugin("{}")'.format(target))
        return {"request": nonce}

    def control(self, action, seconds=None):
        player = self._video_player()
        if player is None:
            raise ValueError("nothing is playing")
        if action in ("pause", "resume"):
            json_rpc("Player.PlayPause", {"playerid": player, "play": action == "resume"})
        elif action == "stop":
            json_rpc("Player.Stop", {"playerid": player})
        elif action in ("next", "previous"):
            json_rpc("Player.GoTo", {"playerid": player, "to": action})
        elif action == "seek":
            if seconds is None:
                raise ValueError("seek needs seconds")
            json_rpc("Player.Seek", {"playerid": player, "value": {"time": _time(seconds)}})
        else:
            raise ValueError("unknown action {!r}".format(action))
        log.info("control: %s%s", action, " {:.0f} s".format(float(seconds)) if action == "seek" else "")
        return {}

    def queue(self):
        items = (json_rpc("Playlist.GetItems", {"playlistid": VIDEO_PLAYLIST, "properties": ["title"],
                                           "limits": {"start": 0, "end": 500}}) or {}).get("items") or []
        player = self._video_player()
        position = -1
        if player is not None:
            props = json_rpc("Player.GetProperties", {"playerid": player, "properties": ["playlistid", "position"]})
            if props.get("playlistid") == VIDEO_PLAYLIST:
                position = props.get("position", -1)
        return {"items": [item.get("title") or item.get("label") or "" for item in items], "position": position}

    def goto(self, index):
        if not isinstance(index, int) or index < 0:
            raise ValueError("index must be a position in the queue")
        player = self._video_player()
        if player is not None:
            json_rpc("Player.GoTo", {"playerid": player, "to": index})
        else:
            json_rpc("Player.Open", {"item": {"playlistid": VIDEO_PLAYLIST, "position": index}})
        return {}


# ---------------------------------------------------------------------------
# PIN and settings
# ---------------------------------------------------------------------------

def read_settings():
    addon = xbmcaddon.Addon()  # fresh: the settings object caches values
    return {"enabled": addon.getSettingBool("web_enabled"), "port": addon.getSettingInt("web_port"),
            "pin": addon.getSettingBool("web_pin_enabled")}


def _state_path():
    profile = xbmcvfs.translatePath(xbmcaddon.Addon().getAddonInfo("profile"))
    xbmcvfs.mkdirs(profile)
    return os.path.join(profile, "web.json")


def _load_state():
    try:
        with open(_state_path(), encoding="utf-8") as handle:
            state = json.load(handle)
        if state.get("pin") and state.get("secret"):
            return state
    except (OSError, ValueError):
        pass
    return new_pin(write_settings=False)


def new_pin(write_settings=True):
    """A fresh PIN and secret: every browser has to enter the new PIN."""
    state = {"pin": "{:06d}".format(random.SystemRandom().randrange(1000000)), "secret": secrets.token_hex(16)}
    fileutil.write_json_atomic(_state_path(), state)
    if write_settings:
        # Changing the shown PIN is a settings change: the service picks the
        # new one up in onSettingsChanged.
        _write(PIN_SETTING, state["pin"])
    return state


def auth_for(settings):
    if not settings["pin"]:
        return web_server.Auth()
    state = _load_state()
    return web_server.Auth(state["pin"], state["secret"])


def _write(setting, value):
    addon = xbmcaddon.Addon()
    if addon.getSettingString(setting) != value:
        addon.setSettingString(setting, value)


def _address(port):
    ip = kodiutil.network_address()
    return "http://{}:{}".format(ip, port) if ip else ""


class WebController:
    """The server while the interface is on, plus the address line kept current."""

    ADDRESS_EVERY = 60  # seconds; DHCP may hand the box a new address

    def __init__(self, settings):
        self.settings = settings
        addon_path = xbmcvfs.translatePath(xbmcaddon.Addon().getAddonInfo("path"))
        self.server = web_server.WebServer(
            KodiBackend(), os.path.join(addon_path, "resources", "web"),
            os.path.join(xbmcvfs.translatePath("special://logpath/"), "kodi.log"),
            settings["port"], auth=auth_for(settings))
        self._stopping = threading.Event()
        self._thread = None

    def start(self):
        try:
            self.server.start()
        except OSError as error:
            log.error("web interface could not bind port %d (%s)", self.settings["port"], error)
            _write(ADDRESS_SETTING, "")
            return False
        address = _address(self.server.port)
        log.log(diag.NOTICE, "web interface on at %s%s", address or "port {}".format(self.server.port),
                " (PIN required)" if self.settings["pin"] else "")
        self._refresh(address)
        self._thread = threading.Thread(target=self._keep_address, name="ytdlpcast-web-address", daemon=True)
        self._thread.start()
        return True

    def _refresh(self, address):
        _write(ADDRESS_SETTING, address)
        _write(PIN_SETTING, self.server.auth.pin or "")

    def _keep_address(self):
        last = _address(self.server.port)
        while not self._stopping.wait(self.ADDRESS_EVERY):
            current = _address(self.server.port)
            if current != last:
                log.log(diag.NOTICE, "web interface now at %s", current or "no network")
                _write(ADDRESS_SETTING, current)
                last = current

    def auth_outdated(self, settings):
        """Was the PIN switched on or off, or replaced ("New PIN") since start?"""
        if settings["pin"] != self.settings["pin"]:
            return True
        return bool(settings["pin"]) and _load_state()["pin"] != self.server.auth.pin

    def reload_auth(self, settings):
        self.settings = settings
        self.server.set_auth(auth_for(settings))
        _write(PIN_SETTING, self.server.auth.pin or "")
        log.info("PIN %s", "required" if settings["pin"] else "not required")

    def stop(self):
        self._stopping.set()
        self.server.stop()
        log.log(diag.NOTICE, "web interface off")


def start(settings):
    if not settings["enabled"]:
        _write(ADDRESS_SETTING, "")
        return None
    controller = WebController(settings)
    return controller if controller.start() else None


def stop(controller):
    if controller is not None:
        controller.stop()
