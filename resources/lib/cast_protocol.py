# -*- coding: utf-8 -*-
"""YouTube cast (Lounge API) protocol: the parts that need neither network nor Kodi.

Ported from TubeCast by enen92 (MIT, see LICENSE.txt). Deliberately free of any
xbmc import, so scripts/test-cast-protocol.py exercises it on a desktop.
"""

import json
from collections import namedtuple

from . import diag

log = diag.logger("cast.lounge")

Command = namedtuple("Command", ("code", "name", "data"))

# Player states as the YouTube app understands them.
STATUS_PLAYING, STATUS_PAUSED, STATUS_LOADING, STATUS_STOPPED = 1, 2, 3, 4


def utf16_length(text):
    """Length as JavaScript counts it -- the unit of the stream's frame headers."""
    return len(text) + sum(1 for char in text if ord(char) > 0xFFFF)


def short(value, limit=300):
    """A command's data, compact enough for one log line."""
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return text if len(text) <= limit else text[:limit] + "…"


class CommandParser:
    """Incremental parser for the bind channel's stream.

    The stream is a sequence of "<length>\\n<JSON array>" frames, each array a
    list of [code, [name, *args]] events, and it arrives in arbitrary pieces.

    TubeCast matched events with a regex and evaluated their data as Python
    literals: JSON's true/false/null broke that, and a nested array cut the data
    short. Here every frame is decoded as the JSON it is. The length header is
    only used to tell an incomplete frame (wait for more) from a broken one
    (skip it): it counts JavaScript string units, not bytes or code points.
    """

    def __init__(self):
        self._buffer = ""
        self._decoder = json.JSONDecoder()

    def feed(self, text):
        """Add received text; return the commands it completed, in order."""
        self._buffer += text
        commands = []
        while True:
            frame = self._next_frame()
            if frame is None:
                return commands
            commands.extend(self._commands(frame))

    def _next_frame(self):
        buffer = self._buffer.lstrip()
        newline = buffer.find("\n")
        if newline < 0:
            self._buffer = buffer
            return None
        header = buffer[:newline].strip()
        if not header.isdigit():
            # Not a frame boundary: drop the line and resynchronise on the next.
            log.warning("stream: skipping unexpected line %r", short(header, 80))
            self._buffer = buffer[newline + 1:]
            return self._next_frame()
        body = buffer[newline + 1:]
        try:
            frame, end = self._decoder.raw_decode(body, len(body) - len(body.lstrip()))
        except ValueError:
            if utf16_length(body) < int(header):
                self._buffer = buffer
                return None  # the rest of the frame is still on its way
            log.warning("stream: dropping a malformed frame: %s", short(body[:int(header)]))
            self._buffer = body[int(header):]
            return self._next_frame()
        self._buffer = body[end:]
        return frame

    @staticmethod
    def _commands(frame):
        if not isinstance(frame, list):
            log.warning("stream: frame is not a list: %s", short(frame))
            return []
        commands = []
        for event in frame:
            try:
                code, (name, *args) = event
                code = int(code)
            except (TypeError, ValueError):
                log.warning("stream: event of unknown shape: %s", short(event))
                continue
            data = None if not args else args[0] if len(args) == 1 else tuple(args)
            commands.append(Command(code, name, data))
        return commands


def parse_remotes(lounge_status):
    """{id: name} of the phones in a loungeStatus event's device list."""
    try:
        devices = json.loads((lounge_status or {}).get("devices") or "[]")
    except (TypeError, ValueError):
        log.warning("loungeStatus: unreadable device list: %s", short(lounge_status))
        return {}
    return {device.get("id"): device.get("name") or device.get("id")
            for device in devices
            if isinstance(device, dict) and device.get("type") == "REMOTE_CONTROL" and device.get("id")}


class CastState:
    """The queue the phone has sent, and which entry of it is current."""

    def __init__(self):
        self.ctt = None
        self.params = None
        self.playlist_id = None
        self.playlist = []
        self.index = None

    @property
    def video_id(self):
        if not self.playlist or self.index is None:
            return None
        return self.playlist[self.index]

    @property
    def has_previous(self):
        return bool(self.playlist) and self.index is not None and self.index > 0

    @property
    def has_next(self):
        return bool(self.playlist) and self.index is not None and self.index < len(self.playlist) - 1

    def set_playlist(self, data):
        self.ctt = data.get("ctt") or self.ctt
        self.params = data.get("params")
        self.playlist_id = data.get("listId")
        self.playlist = [v for v in (data.get("videoIds") or data.get("videoId") or "").split(",") if v]
        try:
            self.index = int(data.get("currentIndex"))
        except (TypeError, ValueError):
            video = data.get("videoId")
            self.index = self.playlist.index(video) if video in self.playlist else (0 if self.playlist else None)
        if self.index is not None and not 0 <= self.index < len(self.playlist):
            self.index = 0 if self.playlist else None

    def update_playlist(self, data):
        """The phone edited the queue. Returns False when it emptied it."""
        current = self.video_id
        self.playlist = [v for v in (data.get("videoIds") or "").split(",") if v]
        if "listId" in data:
            self.playlist_id = data.get("listId")
        if not self.playlist:
            self.index = None
            return False
        if current in self.playlist:
            self.index = self.playlist.index(current)
        elif self.index is None or self.index >= len(self.playlist):
            self.index = len(self.playlist) - 1
        return True

    def step(self, change):
        """Move through the queue; False at either end or without a queue."""
        if self.index is None:
            return False
        target = self.index + change
        if not 0 <= target < len(self.playlist):
            return False
        self.index = target
        return True

    def now_playing(self):
        """The queue half of a nowPlaying report; empty without a queue."""
        if self.video_id is None:
            return {}
        data = {"videoId": self.video_id, "currentIndex": str(self.index)}
        for key, value in (("listId", self.playlist_id), ("ctt", self.ctt), ("params", self.params)):
            if value:
                data[key] = value
        return data
