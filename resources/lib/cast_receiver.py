# -*- coding: utf-8 -*-
"""The receiver: what a command from the phone does, and what the phone is told.

Ported from TubeCast's YoutubeCastV1 by enen92 (MIT, see LICENSE.txt), with the
player behind a small interface so the same logic runs in Kodi (cast_kodi) and
on a desktop (scripts/cast-harness.py). Every method is called from one thread
-- the cast controller's -- so nothing here needs a lock; the exceptions are
is_running() and launch(), which the DIAL server calls and which only read.

player must provide play(video_id, seconds), pause(), resume(), stop(),
seek(seconds), status() -> (state, position, duration), volume() ->
(level, muted), set_volume(level, muted) and notify(text, kind).
"""

import http.client
import random
import string
import time

from . import cast_lounge, cast_protocol, diag
from .cast_protocol import STATUS_LOADING, STATUS_PAUSED, STATUS_PLAYING, STATUS_STOPPED, short

log = diag.logger("cast.receiver")
raw = diag.logger("cast.raw")

# Sent every few seconds or so; logging each one would bury everything else.
QUIET_COMMANDS = ("noop",)
IGNORED_COMMANDS = ("noop", "getDiscoveryDeviceId", "onUserActivity", "getSubtitlesTrack",
                    "setSubtitlesTrack", "getPartyGamesMode", "dpadCommand", "voiceCommand")


class CastReceiver:
    REPORT_EVERY = 5  # seconds between position reports while playing
    RESOLVE_TIMEOUT = 90  # yt-dlp on a slow box takes 10-20 s; this means it is not coming

    def __init__(self, session, player):
        self.session = session
        self.player = player
        self.state = cast_protocol.CastState()
        self.remotes = {}
        self.pending = None   # video asked for, not started yet
        self.pending_since = 0
        self.active = None    # video playing because the phone asked for it
        self.cpn = None
        self.last_report = 0
        self.last_state = None
        self.last_volume = None

    # -- DIAL (called from the DIAL server's threads) -------------------------

    @property
    def name(self):
        return self.session.name

    @property
    def uuid(self):
        return self.session.identity.device_id

    def is_running(self):
        return bool(self.remotes)

    def launch(self, form):
        code = form.get("pairingCode")
        if not code:
            log.warning("DIAL launch without a pairing code: %s", form)
            return False
        if not self.session.online:
            log.warning("DIAL launch before the YouTube session is up; the phone will retry")
            return False
        try:
            self.session.register_pairing_code(code)
        except Exception:  # noqa: BLE001 - answered with 503, the phone retries
            log.exception("registering the pairing code failed")
            return False
        return True

    def stop(self):
        log.info("the phone stopped the YouTube app over DIAL")

    # -- commands from the phone -----------------------------------------------

    def handle(self, commands):
        for command in commands:
            if command.name in QUIET_COMMANDS:
                raw.info("<- %s", command.name)
            else:
                log.info("<- %s %s", command.name, short(command.data) if command.data is not None else "")
            handler = getattr(self, "_on_" + command.name, None)
            if handler is None:
                if command.name not in IGNORED_COMMANDS:
                    log.info("   (not handled)")
                continue
            try:
                handler(command.data if command.data is not None else {})
            except Exception:  # noqa: BLE001 - one bad command must not end the session
                log.exception("handling %s failed", command.name)

    def _on_loungeStatus(self, data):
        remotes = cast_protocol.parse_remotes(data)
        if remotes != self.remotes:
            log.info("phones in the lounge: %s", ", ".join(remotes.values()) or "none")
        self.remotes = remotes

    def _on_remoteConnected(self, data):
        remote_id, name = data.get("id"), data.get("name") or "?"
        self.remotes[remote_id] = name
        log.log(diag.NOTICE, "phone connected: %s", name)
        self.player.notify(name, "connected")
        # Kodi plays the phone's queue; YouTube's own autoplay would fight it.
        self._send("onAutoplayModeChanged", {"autoplayMode": "DISABLED"})
        self._send_navigation()
        self.report_now_playing()

    def _on_remoteDisconnected(self, data):
        remote_id, name = data.get("id"), data.get("name") or "?"
        self.remotes.pop(remote_id, None)
        log.log(diag.NOTICE, "phone disconnected: %s", name)
        self.player.notify(name, "disconnected")

    def _on_getNowPlaying(self, data):
        self.report_now_playing()

    def _on_setPlaylist(self, data):
        self.state.set_playlist(data)
        if self.state.video_id:
            self.play(self.state.video_id, _seconds(data.get("currentTime")))
        self._send_navigation()

    def _on_updatePlaylist(self, data):
        if not self.state.update_playlist(data):
            self._stop_playback()
            return
        self.report_now_playing()
        self._send_navigation()

    def _on_next(self, data):
        if self.state.step(1):
            self.play(self.state.video_id, 0)
            self._send_navigation()

    def _on_previous(self, data):
        if self.state.step(-1):
            self.play(self.state.video_id, 0)
            self._send_navigation()

    def _on_pause(self, data):
        if self.active:
            self.player.pause()

    def _on_play(self, data):
        if self.active:
            self.player.resume()
        elif not self.pending and self.state.video_id:
            # Stopped earlier, the phone wants it back.
            self.play(self.state.video_id, 0)

    def _on_stopVideo(self, data):
        self._stop_playback()

    def _stop_playback(self):
        if self.active:
            self.player.stop()  # Kodi's stop event reports it
        elif self.pending:
            self.pending = None
            self.player.stop()
            self.report_state(STATUS_STOPPED, 0, 0)

    def _on_seekTo(self, data):
        if self.active:
            seconds = _seconds(data.get("newTime"))
            _, _, duration = self.player.status()
            self.report_state(STATUS_LOADING, seconds, duration)
            self.player.seek(seconds)

    def _on_getVolume(self, data):
        self.report_volume(*self.player.volume())

    def _on_setVolume(self, data):
        level = int(_seconds(data.get("volume")))
        muted = str(data.get("muted", "false")).lower() == "true"
        if (level, muted) != self.player.volume():
            self.player.set_volume(level, muted)

    def _on_setAutoplayMode(self, data):
        self._send("onAutoplayModeChanged", {"autoplayMode": "DISABLED"})

    # -- playback ---------------------------------------------------------------

    def play(self, video_id, seconds):
        log.info("playing %s from %ss", video_id, int(seconds))
        self.pending, self.active = video_id, None
        self.pending_since = time.monotonic()
        self.cpn = "".join(random.choice(string.ascii_letters + string.digits) for _ in range(16))
        self.report_state(STATUS_LOADING, seconds, 0)
        self.player.play(video_id, seconds)

    def on_started(self, ours):
        """Something started playing in Kodi; ours tells whether we asked for it."""
        if ours and self.pending:
            self.active, self.pending = self.pending, None
            log.info("playback of %s started", self.active)
            self.report_now_playing()
            self.report_state()
        elif self.active or self.pending:
            # The user played something else in Kodi: ours is over, and the
            # phone must not be told about a video it did not ask for.
            log.info("Kodi switched to other content; cast playback of %s ended", self.active or self.pending)
            self.active = self.pending = None
            self.report_state(STATUS_STOPPED, 0, 0)

    def on_paused(self):
        if self.active:
            self.report_state(STATUS_PAUSED)

    def on_resumed(self):
        if self.active:
            self.report_state(STATUS_PLAYING)

    def on_seeked(self):
        if self.active:
            self.report_state()

    def on_stopped(self):
        # Only what is actually playing: switching videos stops the previous
        # one after play() has already asked for the next, which must survive.
        if self.active:
            log.info("playback of %s stopped", self.active)
            self.active = None
            self.report_state(STATUS_STOPPED, 0, 0)

    def on_ended(self):
        if not self.active:
            return
        log.info("playback of %s ended", self.active)
        self.active = None
        self.report_state(STATUS_STOPPED, 0, 0)
        if self.state.step(1):
            self.play(self.state.video_id, 0)
            self._send_navigation()
        else:
            self.report_now_playing()

    def on_failed(self, why):
        if self.active or self.pending:
            log.warning("playback of %s failed: %s", self.active or self.pending, why)
            self.active = self.pending = None
            self.report_state(STATUS_STOPPED, 0, 0)
            self.report_now_playing()

    def on_volume_changed(self, level, muted):
        if self.remotes and (level, muted) != self.last_volume:
            self.report_volume(level, muted)

    def tick(self, now=None):
        """Called often; sends the position every REPORT_EVERY seconds while playing."""
        now = now if now is not None else time.monotonic()
        if self.pending and now - self.pending_since > self.RESOLVE_TIMEOUT:
            self.on_failed("did not start within {} s".format(self.RESOLVE_TIMEOUT))
        elif self.active and self.remotes and now - self.last_report >= self.REPORT_EVERY:
            self.report_state()

    # -- reports ----------------------------------------------------------------

    def report_state(self, state=None, position=None, duration=None):
        current_state, current_position, current_duration = self.player.status()
        state = current_state if state is None else state
        position = current_position if position is None else position
        duration = current_duration if duration is None else duration
        self.last_report = time.monotonic()
        # A change of state is a cast event; the same state again every five
        # seconds (the position ticking on) belongs to the raw traffic.
        quiet = state == self.last_state
        self.last_state = state
        self._send("onStateChange", _timing(state, position, duration, self.cpn), quiet=quiet)

    def report_now_playing(self):
        data = self.state.now_playing()
        if data and (self.active or self.pending):
            state, position, duration = self.player.status()
            if self.pending:
                state = STATUS_LOADING
            data.update(_timing(state, position, duration, self.cpn))
        self._send("nowPlaying", data)

    def report_volume(self, level, muted):
        self.last_volume = (level, muted)
        self._send("onVolumeChanged", {"volume": str(level), "muted": "true" if muted else "false"})

    def _send_navigation(self):
        self._send("onHasPreviousNextChanged", {
            "hasPrevious": "true" if self.state.has_previous else "false",
            "hasNext": "true" if self.state.has_next else "false"})

    def _send(self, sc, payload, quiet=False):
        (raw if quiet else log).info("-> %s %s", sc, short(payload))
        try:
            self.session.send(sc, payload)
        except (cast_lounge.LoungeError, OSError, http.client.HTTPException) as error:
            log.warning("sending %s failed: %s", sc, error)


def _seconds(value):
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return 0.0


def _timing(state, position, duration, cpn):
    duration = int(duration or 0)
    loaded = duration if state in (STATUS_PLAYING, STATUS_PAUSED, STATUS_LOADING) else 0
    return {
        "state": str(state),
        "currentTime": str(int(position or 0)),
        "duration": str(duration),
        "loadedTime": str(loaded),
        "seekableStartTime": "0",
        "seekableEndTime": str(duration),
        "cpn": cpn or "",
    }
