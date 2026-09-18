# -*- coding: utf-8 -*-
"""Just enough MP4 box reading to segment a fragmented file by byte range.

YouTube's DASH fMP4 (itag 140 audio, and the video itags) puts ftyp, moov and a
single sidx at the very start, so one small range request over the head of the
file is enough to learn the init range and every segment's byte range and
duration. That lets us describe the file as a byte-range HLS media playlist,
which InputStream Adaptive reads with its FragmentedSampleReader -- the same
reader as the video, and crucially not the packed-ADTS reader that starts
silent until a seek.

No xbmc import: scripts/test-audio-fmp4.py exercises this on a desktop.
"""

import struct

_HEADER = 8


def _boxes(data):
    """Top-level boxes as (type, start, size); stops at the first truncated one."""
    boxes = []
    offset = 0
    while offset + _HEADER <= len(data):
        size = struct.unpack(">I", data[offset:offset + 4])[0]
        box_type = data[offset + 4:offset + 8].decode("latin1")
        if size == 1:  # 64-bit largesize
            if offset + 16 > len(data):
                break
            size = struct.unpack(">Q", data[offset + 8:offset + 16])[0]
        if size < _HEADER:
            break
        boxes.append((box_type, offset, size))
        offset += size
    return boxes


def init_range(head):
    """(0, end) covering ftyp+moov -- the initialization segment.

    Raises ValueError if moov is not within the fetched head.
    """
    end = 0
    seen_moov = False
    for box_type, start, size in _boxes(head):
        if box_type in ("ftyp", "moov", "styp"):
            end = start + size
            seen_moov = seen_moov or box_type == "moov"
        elif box_type in ("sidx", "moof", "mdat"):
            break
    if not seen_moov:
        raise ValueError("moov not found in the fetched head")
    return (0, end)


def parse_sidx(head):
    """Segments as [(start, size, duration_seconds), ...] from the sidx box.

    Offsets are absolute in the file. Raises ValueError if there is no sidx in
    the fetched head (fetch a larger range and retry).
    """
    sidx = next((b for b in _boxes(head) if b[0] == "sidx"), None)
    if sidx is None:
        raise ValueError("no sidx in the fetched head")
    _, box_start, box_size = sidx
    body = head[box_start + _HEADER:box_start + box_size]
    if len(body) < box_size - _HEADER:
        raise ValueError("sidx is truncated in the fetched head")

    version = body[0]
    pos = 4  # version(1) + flags(3)
    pos += 4  # reference_ID
    timescale = struct.unpack(">I", body[pos:pos + 4])[0]
    pos += 4
    if version == 0:
        pos += 4  # earliest_presentation_time
        first_offset = struct.unpack(">I", body[pos:pos + 4])[0]
        pos += 4
    else:
        pos += 8  # earliest_presentation_time (64-bit)
        first_offset = struct.unpack(">Q", body[pos:pos + 8])[0]
        pos += 8
    pos += 2  # reserved
    count = struct.unpack(">H", body[pos:pos + 2])[0]
    pos += 2

    # Segments start right after the whole sidx box, plus first_offset.
    cursor = box_start + box_size + first_offset
    segments = []
    for _ in range(count):
        word0 = struct.unpack(">I", body[pos:pos + 4])[0]
        referenced_size = word0 & 0x7FFFFFFF  # top bit = reference_type
        subsegment_duration = struct.unpack(">I", body[pos + 4:pos + 8])[0]
        pos += 12  # size+type, duration, SAP word
        segments.append((cursor, referenced_size, subsegment_duration / timescale))
        cursor += referenced_size
    return segments
