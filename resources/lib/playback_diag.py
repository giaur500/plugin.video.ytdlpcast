# -*- coding: utf-8 -*-
"""Diagnostics of Kodi's own player, for what this add-on hands it.

yt-dlp and the manifest rewrite decide what is offered; what Kodi makes of it
is the other half: which decoder, in hardware or not, which quality InputStream
Adaptive settles on and switches to, whether playback stalls to buffer. All of
it is in Kodi's info labels (Player.Process(...), Player.Caching,
VideoPlayer.*), so this only has to watch and write it down.

Runs in the service as one thread, and only while its Diagnostics switch is on.
Kodi delivers Player callbacks solely to the thread that created the Player, in
that thread's waitForAbort, so the thread creates its own and polls in the
same loop. To tell our playback from anything else, main.py leaves a window
property right before handing the item to Kodi (mark_playing); a start without
a fresh mark is not ours and is not followed.
"""

import json
import queue
import threading
import time

import xbmc
import xbmcaddon
import xbmcgui

from . import diag

ADDON_ID = xbmcaddon.Addon().getAddonInfo("id")
HOME = xbmcgui.Window(10000)
PROP_PLAYING = ADDON_ID + ".playing"
# What the plugin last handed Kodi, kept (not consumed) for the web interface.
PROP_SOURCE = ADDON_ID + ".playing.source"
MARK_FRESH = 120  # seconds from resolving to the player starting, at most
POLL = 2.0
SEEK_SETTLE = 5.0  # buffering this soon after a seek is the seek, not a stall

log = diag.logger("player")


def mark_playing(video_id, kind, rewritten, source=None, site=None, title=None):
    """Called by the plugin: the next start is ours."""
    mark = {"id": video_id, "kind": kind, "rewritten": bool(rewritten), "source": source, "site": site,
            "title": title, "at": time.time()}
    HOME.setProperty(PROP_PLAYING, json.dumps(mark))
    HOME.setProperty(PROP_SOURCE, json.dumps(mark))


def last_source():
    """The plugin's last mark -- link, site, title -- or None."""
    try:
        return json.loads(HOME.getProperty(PROP_SOURCE) or "null")
    except ValueError:
        return None


def _take_mark():
    raw = HOME.getProperty(PROP_PLAYING)
    HOME.clearProperty(PROP_PLAYING)
    try:
        mark = json.loads(raw) if raw else None
    except ValueError:
        return None
    if mark and time.time() - mark.get("at", 0) <= MARK_FRESH:
        return mark
    return None


def _process(key):
    return xbmc.getInfoLabel("Player.Process({})".format(key))


class _Player(xbmc.Player):
    """Only queues what happened."""

    def __init__(self, events):
        super().__init__()
        self.events = events

    def onAVStarted(self):
        self.events.put("started")

    def onPlayBackPaused(self):
        self.events.put("paused")

    def onPlayBackResumed(self):
        self.events.put("resumed")

    def onPlayBackSeek(self, time, seekOffset):  # noqa: A002, N803 - Kodi's signature
        self.events.put("seek")

    def onPlayBackStopped(self):
        self.events.put("stopped")

    def onPlayBackEnded(self):
        self.events.put("ended")

    def onPlayBackError(self):
        self.events.put("error")


class _Session:
    """One playback of ours, from start to stop."""

    def __init__(self, mark, player):
        self.mark = mark
        self.player = player
        self.started = time.monotonic()
        self.resolution = None
        self.resolutions = []
        self.changes = 0
        self.stall_since = None
        self.stall_at = 0
        self.stall_after_seek = False
        self.stalls = []
        self.seek_buffering = []
        self.last_seek = None
        self.seeks = 0
        self.pauses = 0

    def position(self):
        try:
            return int(self.player.getTime())
        except RuntimeError:
            return 0

    def _resolution(self):
        width, height = _process("videowidth"), _process("videoheight")
        if width and height:
            return "{}x{}".format(width, height)
        return xbmc.getInfoLabel("VideoPlayer.VideoResolution") or None

    def begin(self):
        try:
            playing = diag.safe_url(self.player.getPlayingFile())
        except RuntimeError:
            playing = "?"
        log.info("started %s (%s, manifest %s) from %s", self.mark.get("id"), self.mark.get("kind"),
                 "rewritten" if self.mark.get("rewritten") else "as published", playing)
        log.info("video: %s %sx%s @ %s fps, decoder %s (%s), pixel format %s, deinterlacing %s",
                 xbmc.getInfoLabel("VideoPlayer.VideoCodec") or "?", _process("videowidth") or "?",
                 _process("videoheight") or "?", _process("videofps") or "?", _process("videodecoder") or "?",
                 "hardware" if xbmc.getCondVisibility("Player.Process(videohwdecoder)") else "software",
                 _process("pixformat") or "?", _process("deintmethod") or "none")
        log.info("audio: %s via %s, %s channels, %s Hz, %s bits", xbmc.getInfoLabel("VideoPlayer.AudioCodec") or "?",
                 _process("audiodecoder") or "?", _process("audiochannels") or "?",
                 _process("audiosamplerate") or "?", _process("audiobitspersample") or "?")
        self.resolution = self._resolution()
        if self.resolution:
            self.resolutions.append(self.resolution)

    def poll(self):
        now = time.monotonic()
        resolution = self._resolution()
        if resolution and resolution != self.resolution:
            log.info("quality change at %ds: %s -> %s (video bitrate %s)", self.position(), self.resolution,
                     resolution, xbmc.getInfoLabel("VideoPlayer.VideoBitrate") or "?")
            self.changes += 1
            self.resolution = resolution
            if resolution not in self.resolutions:
                self.resolutions.append(resolution)

        caching = xbmc.getCondVisibility("Player.Caching")
        if caching and self.stall_since is None:
            self.stall_since = now
            self.stall_at = self.position()
            self.stall_after_seek = self.last_seek is not None and now - self.last_seek < SEEK_SETTLE
            log.info("buffering at %ds%s, cache %s%%", self.stall_at,
                     " after a seek" if self.stall_after_seek else "", xbmc.getInfoLabel("Player.CacheLevel") or "?")
        elif not caching and self.stall_since is not None:
            self._end_stall(now)

    def _end_stall(self, now):
        took = now - self.stall_since
        (self.seek_buffering if self.stall_after_seek else self.stalls).append(took)
        log.info("buffering at %ds over after %.1f s", self.stall_at, took)
        self.stall_since = None

    def seeked(self):
        self.seeks += 1
        self.last_seek = time.monotonic()
        log.info("seek to %ds", self.position())

    def paused(self):
        self.pauses += 1
        log.info("paused at %ds", self.position())

    def resumed(self):
        log.info("resumed at %ds", self.position())

    def finish(self, how):
        if self.stall_since is not None:
            self._end_stall(time.monotonic())
        log.info("%s %s after %.0f s: %d stall(s) for %.1f s in total, %d buffering after seeks for %.1f s; "
                 "%d quality change(s), resolutions %s; %d seek(s), %d pause(s)",
                 how, self.mark.get("id"), time.monotonic() - self.started, len(self.stalls), sum(self.stalls),
                 len(self.seek_buffering), sum(self.seek_buffering), self.changes,
                 " -> ".join(self.resolutions) or "?", self.seeks, self.pauses)


class PlaybackDiagnostics(threading.Thread):

    def __init__(self):
        super().__init__(name="ytdlpcast-player-diag", daemon=True)
        self.events = queue.Queue()
        self._stopping = threading.Event()

    def stop(self):
        self._stopping.set()

    def run(self):
        try:
            self._run()
        except Exception:  # noqa: BLE001 - diagnostics must never take the service down
            log.exception("player diagnostics crashed")

    def _run(self):
        monitor = xbmc.Monitor()
        player = _Player(self.events)  # created here: its callbacks arrive here
        session = None
        last_poll = 0.0
        log.info("watching Kodi's player")
        while not self._stopping.is_set():
            if monitor.waitForAbort(0.5 if session else 1.0):
                break
            while True:
                try:
                    event = self.events.get_nowait()
                except queue.Empty:
                    break
                if event == "started":
                    if session:
                        session.finish("replaced")
                    mark = _take_mark()
                    session = _Session(mark, player) if mark else None
                    if session:
                        session.begin()
                    else:
                        log.info("something else started playing; not followed")
                elif session is None:
                    continue
                elif event in ("stopped", "ended", "error"):
                    session.finish({"stopped": "stopped", "ended": "ended", "error": "playback error in"}[event])
                    session = None
                elif event == "seek":
                    session.seeked()
                elif event == "paused":
                    session.paused()
                elif event == "resumed":
                    session.resumed()
            if session and time.monotonic() - last_poll >= POLL:
                session.poll()
                last_poll = time.monotonic()
        if session:
            session.finish("no longer watching")
        log.info("stopped watching Kodi's player")


def start(enabled):
    if not enabled:
        return None
    watcher = PlaybackDiagnostics()
    watcher.start()
    return watcher


def stop(watcher):
    if watcher is not None:
        watcher.stop()
        watcher.join(5)
