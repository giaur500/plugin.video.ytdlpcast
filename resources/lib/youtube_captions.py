# -*- coding: utf-8 -*-
"""YouTube's automatic captions, drawn the way YouTube's own player draws them.

YouTube writes them for a window of two rows that scrolls up: each line is an
event lasting until the line after next begins, its words timed one by one,
and a newline appended to the window pushes the older line up. In SRT --
what the plugin used to fetch -- that comes out as cues overlapping by
seconds. Kodi hands each to libass as an event of its own, and libass stacks
overlapping events by collision: the newer line above the older, after a seek
in no particular order (DVDSubtitlesLibass.cpp says as much). WebVTT carries
the word times, but Kodi's WebVTT reader ignores them.

json3, the format YouTube's player reads, keeps the window, its position, the
word times and the pens -- srv3 by another name, whose attributes
YTSubConverter documents (ytt.ytt). Here each window is followed through
time: whenever a word appears or a line comes or goes, what the window shows
until the next change becomes one ASS event. So one window's events never
overlap, the words appear as they are spoken, and the older line stays above
the newer. There are no karaoke tags: nothing depends on how libass draws the
background of words not yet spoken, and Kodi's style override still applies.

to_srt() draws the same windows as whole lines in plain SRT, for Kodi's own
subtitle style. Only automatic captions come here; the uploader's tracks stay
YouTube's SRT (resolver.pick_subtitles).

No xbmc import: scripts/test-youtube-captions.py exercises it on a desktop.
"""

import json

# What Kodi itself uses for text subtitles, so sizes mean the same as there.
PLAY_RES_X, PLAY_RES_Y = 1920, 1080
# Kodi's subtitles.fontsize is in pixels of a 720-line picture
# (DVDSubtitlesLibass.cpp: scaleDefault = playResY / 720).
KODI_FONT_SIZE = 42
# YouTube's 10 ms steps between lines, or two words 30 ms apart, would show for
# a frame at most; such a state goes to the one before it.
SHORTEST_MS = 50
# YouTube's anchor points, row by row from the top left, as ASS's numpad \an.
ANCHORS = {0: 7, 1: 8, 2: 9, 3: 4, 4: 5, 5: 6, 6: 1, 7: 2, 8: 3}
# Where YouTube puts a caption no window places: bottom centre.
DEFAULT_POSITION = {"apPoint": 7, "ahHorPos": 50, "avVerPos": 100}
# A character's average width, in font sizes: a guess wide enough for Roboto,
# Arial and Kodi's own font, to keep a window of ccCols columns on the screen.
CHARACTER_WIDTH = 0.6
# fsFontStyle; 0 and 4 are the default, Roboto. A font the box lacks becomes Kodi's.
FONTS = {1: "Courier New", 2: "Times New Roman", 3: "Lucida Console", 5: "Comic Sans MS",
         6: "Monotype Corsiva", 7: "Arial"}

_HEADER = """[Script Info]
; YouTube's automatic captions as YouTube's player draws them, by plugin.video.ytdlpcast
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 0
ScaledBorderAndShadow: yes
YCbCr Matrix: None

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, \
Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, \
MarginV, Encoding
Style: Box,Roboto,{size},&H00FFFFFF,&H00FFFFFF,&H40000000,&H40000000,0,0,0,0,100,100,0,0,3,{padding},0,2,0,0,0,1
Style: Edge,Roboto,{size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,0,0,2,0,0,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


class _Event:
    __slots__ = ("start", "end", "order", "segments")

    def __init__(self, start, end, order, segments):
        self.start, self.end, self.order = start, end, order
        self.segments = segments  # [(shown at, text, pen index)]


class _Box:
    """Where text appears: one of the track's windows, or a place of its own."""

    def __init__(self, position, rows=None, window=False):
        self.position = dict(DEFAULT_POSITION, **position)
        self.rows = rows or None  # rcRows: the rows a window shows, the newest ones
        # A window's events run on into each other, their newlines (aAppend)
        # making the rows; captions with no window are a row of their own each.
        self.window = window
        self.events = []


def to_ass(data, kodi_font_size=KODI_FONT_SIZE):
    """An ASS script of the json3 track, as YouTube's player shows it.

    kodi_font_size is Kodi's subtitles.fontsize, so the captions are as big as
    Kodi's own. ValueError when data is not json3 or shows nothing.
    """
    pens, boxes = _load(data)
    size = max(1, round(kodi_font_size * PLAY_RES_Y / 720))
    dialogues = []
    for box in boxes:
        boxed = _boxed(box, pens)
        anchor, x, y = _placement(box.position, size)
        place = "{{\\an{}\\pos({},{})}}".format(anchor, x, y)
        for start, end, rows in _states(box, words=True):
            begin, finish = _ass_time(start), _ass_time(end)
            if begin != finish:
                dialogues.append((start, "Dialogue: 0,{},{},{},,0,0,0,,{}{}".format(
                    begin, finish, "Box" if boxed else "Edge", place, _ass_text(rows, pens, boxed))))
    if not dialogues:
        raise ValueError("no captions in the track")
    dialogues.sort(key=lambda dialogue: dialogue[0])
    header = _HEADER.format(width=PLAY_RES_X, height=PLAY_RES_Y, size=size, padding=max(2, round(size / 10)))
    return header + "\n".join(line for _, line in dialogues) + "\n"


def to_srt(data):
    """The same windows as plain SRT: whole lines, the older above the newer.

    Never two cues at once, so nothing is left for Kodi to stack. ValueError
    when data is not json3 or shows nothing.
    """
    _, boxes = _load(data)
    timelines = [_states(box, words=False) for box in boxes]
    times = sorted({moment for states in timelines for start, end, _ in states for moment in (start, end)})
    cues, current = [], [0] * len(timelines)
    for start, end in zip(times, times[1:]):
        lines = []
        for number, states in enumerate(timelines):
            while current[number] < len(states) and states[current[number]][1] <= start:
                current[number] += 1
            if current[number] < len(states) and states[current[number]][0] <= start:
                lines.extend("".join(text for text, _ in row) for row in states[current[number]][2])
        if lines:
            cues.append((start, end, tuple(lines)))
    cues = _settle(cues)
    if not cues:
        raise ValueError("no captions in the track")
    return "".join("{}\n{} --> {}\n{}\n\n".format(number, _srt_time(start), _srt_time(end), "\n".join(lines))
                   for number, (start, end, lines) in enumerate(cues, 1))


# ---------------------------------------------------------------------------
# Reading json3
# ---------------------------------------------------------------------------

def _load(data):
    """(pens, boxes) of a json3 track, boxes in the order their text first comes."""
    try:
        doc = json.loads(data)
    except ValueError as error:  # UnicodeDecodeError included
        raise ValueError("not json3: {}".format(error)) from None
    if not isinstance(doc, dict) or not isinstance(doc.get("events"), list):
        raise ValueError("not json3: no event list")
    pens = [pen if isinstance(pen, dict) else {} for pen in doc.get("pens") or ()] or [{}]
    positions = doc.get("wpWinPositions") if isinstance(doc.get("wpWinPositions"), list) else []
    windows, places, boxes = {}, {}, []
    for order, event in enumerate(doc["events"]):
        if not isinstance(event, dict):
            continue
        if "segs" not in event:
            if _key(event.get("id")) is not None:  # a window: where its text goes, until the track ends
                position = _entry(positions, event.get("wpWinPosId"))
                windows[event["id"]] = _Box(position, _number(position.get("rcRows")), window=True)
            continue
        start = _number(event.get("tStartMs"))
        end = start + _number(event.get("dDurationMs"))
        if end <= start:
            continue  # nothing shown: YouTube's newline before a pause has no duration
        segments = []
        for segment in event["segs"] if isinstance(event["segs"], list) else ():
            text = segment.get("utf8") if isinstance(segment, dict) else None
            text = text.replace("\r", "") if isinstance(text, str) else ""
            if text:
                shown = min(max(start + _number(segment.get("tOffsetMs")), start), end)
                pen = _number(segment.get("pPenId", event.get("pPenId")))
                segments.append((shown, text, pen if 0 <= pen < len(pens) else 0))
        box = windows.get(_key(event.get("wWinId")))
        if box is None:
            # No window: one box per place, so captions meant for the same spot
            # stack in it rather than land on top of each other.
            place = _key(event.get("wpWinPosId"))
            box = places.get(place)
            if box is None:
                box = places[place] = _Box(_entry(positions, place))
        if not box.events:
            boxes.append(box)
        box.events.append(_Event(start, end, order, segments))
    return pens, boxes


def _key(value):
    return value if isinstance(value, (int, str)) else None


def _entry(entries, index):
    if isinstance(index, int) and 0 <= index < len(entries) and isinstance(entries[index], dict):
        return entries[index]
    return {}


def _number(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


# ---------------------------------------------------------------------------
# A box through time
# ---------------------------------------------------------------------------

def _states(box, words):
    """[(start, end, rows)]: what the box shows from start to end, one entry per change.

    With words a segment shows from its own time, so the words appear as
    spoken; without, from its event's start: whole lines.
    """
    events = sorted(box.events, key=lambda event: (event.start, event.order))
    times = set()
    for event in events:
        times.update((event.start, event.end))
        if words:
            times.update(shown for shown, _, _ in event.segments)
    times = sorted(times)
    states, active, upcoming = [], [], 0
    for start, end in zip(times, times[1:]):
        while upcoming < len(events) and events[upcoming].start <= start:
            active.append(events[upcoming])
            upcoming += 1
        active = [event for event in active if event.end > start]
        rows = _rows(active, start if words else None, box)
        if rows:
            states.append((start, end, rows))
    return _settle(states)


def _rows(events, now, box):
    """The rows the box's events show at now (None: all of them), each a tuple of (text, pen).

    A window's text is its events' text in order; a newline starts a row, and
    of more rows than the window has only the newest show -- the scroll.
    """
    rows, row = [], []
    for event in events:
        if row and not box.window:
            rows.append(row)
            row = []
        for shown, text, pen in event.segments:
            if now is not None and shown > now:
                continue
            for number, part in enumerate(text.split("\n")):
                if number:
                    rows.append(row)
                    row = []
                if not part:
                    continue
                if row and row[-1][1] == pen:
                    row[-1] = (row[-1][0] + part, pen)
                else:
                    row.append((part, pen))
    rows.append(row)
    rows = [_strip(row) for row in rows]
    # Trimmed before the limit: the newline that comes 10 ms before the next
    # line would otherwise scroll the older line out for those 10 ms.
    while rows and not rows[-1]:
        rows.pop()
    while rows and not rows[0]:
        rows.pop(0)
    return tuple(rows[-box.rows:] if box.rows else rows)


def _strip(row):
    row = list(row)
    while row and not row[0][0].strip():
        row.pop(0)
    while row and not row[-1][0].strip():
        row.pop()
    if row:
        row[0] = (row[0][0].lstrip(), row[0][1])
        row[-1] = (row[-1][0].rstrip(), row[-1][1])
    return tuple(row)


def _settle(states):
    """Give a state to the one before when it shows the same, or too briefly to be seen."""
    settled = []
    for start, end, rows in states:
        if settled and settled[-1][1] == start and (settled[-1][2] == rows or end - start < SHORTEST_MS):
            settled[-1] = (settled[-1][0], end, settled[-1][2])
        else:
            settled.append((start, end, rows))
    return settled


# ---------------------------------------------------------------------------
# Writing ASS and SRT
# ---------------------------------------------------------------------------

def _placement(position, size):
    """(\\an, x, y) for a window position: YouTube's anchor point at its percentages,
    which the player itself maps to 2..98 % of the picture (ytt.ytt).

    A window of ccCols columns that would reach past that, in a large font,
    moves in as far as it has to -- once, for all its text, so it never jumps.
    """
    point = _number(position.get("apPoint"))
    anchor = ANCHORS.get(point, 2)
    x = (_percent(position.get("ahHorPos"), 50) * 0.96 + 2) / 100 * PLAY_RES_X
    y = (_percent(position.get("avVerPos"), 100) * 0.96 + 2) / 100 * PLAY_RES_Y
    width = _number(position.get("ccCols")) * CHARACTER_WIDTH * size
    low, high = 0.02 * PLAY_RES_X, 0.98 * PLAY_RES_X
    if width and point in (0, 3, 6):  # anchored by the left edge
        x = max(low, min(x, high - width))
    elif width and point in (2, 5, 8):
        x = min(high, max(x, low + width))
    return anchor, round(x), round(y)


def _percent(value, default):
    try:
        return min(max(float(value), 0.0), 100.0)
    except (TypeError, ValueError):
        return default


def _boxed(box, pens):
    """False when the track switches the background off (bo="0"): outlines or shadows instead."""
    return not any("boBackAlpha" in pens[pen] and _number(pens[pen]["boBackAlpha"]) == 0
                   for event in box.events for _, _, pen in event.segments)


def _ass_text(rows, pens, boxed):
    return "\\N".join("".join(_styled(text, pens[pen], boxed) for text, pen in row) for row in rows)


def _styled(text, pen, boxed):
    tags = _pen_tags(pen, boxed)
    text = _escape(text)
    return "{{{}}}{}{{\\r}}".format(tags, text) if tags else text


def _pen_tags(pen, boxed):
    """ASS override tags for a YouTube pen; the attributes as ytt.ytt documents them."""
    tags = []
    for attribute, tag in (("bAttr", "\\b1"), ("iAttr", "\\i1"), ("uAttr", "\\u1")):
        if _number(pen.get(attribute)):
            tags.append(tag)
    if "fcForeColor" in pen:
        tags.append("\\1c" + _color(pen["fcForeColor"]))
    if "foForeAlpha" in pen:
        tags.append("\\1a" + _alpha(pen["foForeAlpha"]))
    if boxed:
        # BorderStyle 3: the outline colour is the box behind the text.
        if "bcBackColor" in pen:
            tags.append("\\3c" + _color(pen["bcBackColor"]))
        if "boBackAlpha" in pen:
            tags.append("\\3a" + _alpha(pen["boBackAlpha"]))
    else:
        edge = _color(pen.get("ecEdgeColor", 0x000000))
        tags.append({1: "\\bord0\\shad3\\4c" + edge,                # hard shadow
                     2: "\\bord0\\shad2\\4c" + edge,                # bevel, as near as ASS gets
                     3: "\\bord3\\shad0\\3c" + edge,                # glow / outline
                     4: "\\bord0\\shad3\\blur3\\4c" + edge}.get(    # soft shadow
                         _number(pen.get("etEdgeType")), ""))
    size = _number(pen.get("szPenSize"))
    if size and size != 100:
        scale = max(1, round(100 + (size - 100) / 4))
        tags.append("\\fscx{0}\\fscy{0}".format(scale))
    font = FONTS.get(_number(pen.get("fsFontStyle")))
    if font:
        tags.append("\\fn" + font)
    return "".join(tags)


def _color(value):
    """ASS's &HBBGGRR& from YouTube's colour, a number or "#RRGGBB"."""
    try:
        rgb = int(value.lstrip("#"), 16) if isinstance(value, str) else int(value)
    except (TypeError, ValueError):
        rgb = 0xFFFFFF
    return "&H{:02X}{:02X}{:02X}&".format(rgb & 0xFF, (rgb >> 8) & 0xFF, (rgb >> 16) & 0xFF)


def _alpha(opacity):
    """ASS's alpha, 0 opaque, from YouTube's opacity, 255 opaque."""
    return "&H{:02X}&".format(255 - min(max(_number(opacity), 0), 255))


def _escape(text):
    # libass reads {...} as tags and \N, \n, \h as breaks and spaces: a word
    # joiner after a backslash keeps it one; braces have no escape, so
    # look-alikes stand in.
    return text.replace("\\", "\\⁠").replace("{", "｛").replace("}", "｝")


def _ass_time(ms):
    return "{}:{:02d}:{:02d}.{:02d}".format(ms // 3600000, ms // 60000 % 60, ms // 1000 % 60, ms % 1000 // 10)


def _srt_time(ms):
    return "{:02d}:{:02d}:{:02d},{:03d}".format(ms // 3600000, ms // 60000 % 60, ms // 1000 % 60, ms % 1000)
