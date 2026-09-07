# -*- coding: utf-8 -*-
"""Stream resolution.

Deliberately free of any xbmc import so the logic can be exercised on a normal
desktop with scripts/test-resolver.py, without a running Kodi.
"""

import urllib.parse

from yt_dlp import YoutubeDL

HLS_MIME = "application/vnd.apple.mpegurl"

# Headers yt-dlp attaches for its own use; forwarding them to InputStream
# Adaptive is at best useless and at worst breaks the manifest request.
_SKIP_HEADERS = ("cookie", "youtubei")


def watch_url(video_id):
    return "https://www.youtube.com/watch?v={}".format(video_id)


def extract(url):
    options = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
    }
    with YoutubeDL(options) as ydl:
        return ydl.sanitize_info(ydl.extract_info(url, download=False))


def pick_hls(info):
    """Return (manifest_url, headers) of the HLS master playlist, or (None, None).

    YouTube publishes one master manifest that already lists every variant and
    carries the audio, so InputStream Adaptive gets seeking and bitrate
    switching for free -- no stitching two streams together on our side.
    """
    for fmt in info.get("formats") or ():
        if fmt.get("protocol") == "m3u8_native" and fmt.get("manifest_url"):
            return fmt["manifest_url"], fmt.get("http_headers") or {}
    return None, None


def pick_progressive(info):
    """Best single file carrying both tracks.

    Only a fallback: most YouTube videos no longer offer a muxed format at all.
    """
    best = None
    for fmt in info.get("formats") or ():
        if fmt.get("protocol") not in ("https", "http"):
            continue
        if fmt.get("vcodec") in (None, "none") or fmt.get("acodec") in (None, "none"):
            continue
        if best is None or (fmt.get("height") or 0) > (best.get("height") or 0):
            best = fmt
    if best is None:
        return None, None
    return best["url"], best.get("http_headers") or {}


def encode_headers(headers):
    """Headers in the "a=b&c=d" shape InputStream Adaptive expects."""
    usable = {
        key: value
        for key, value in (headers or {}).items()
        if value and not key.lower().startswith(_SKIP_HEADERS)
    }
    return urllib.parse.urlencode(usable)
