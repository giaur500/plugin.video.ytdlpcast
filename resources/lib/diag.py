# -*- coding: utf-8 -*-
"""Diagnostic logging shared by every part of the add-on. No xbmc import.

Each area logs through its own logger, "ytdlpcast.<area>". The Kodi side
(kodilog.apply) sets an area to INFO when its switch under Settings ->
Diagnostics is on, and to NOTICE otherwise: then only the area's summary lines
(logged at NOTICE), warnings and errors get through. A switched-off area costs
one level check per call; anything expensive to compute is guarded with
enabled().

Kodi 21 writes INFO to kodi.log without its debug mode, so a switch is all it
takes -- nobody has to turn on Kodi's debug log and wade through the rest.
"""

import logging
import ssl
import time
import urllib.parse

ROOT = "ytdlpcast"

# Between INFO and WARNING: the few lines that are always logged.
NOTICE = 25
logging.addLevelName(NOTICE, "NOTICE")

# Switch (setting id) -> the areas it opens. "diag_all" opens every one.
SWITCHES = {
    "diag_resolve": ("resolve",),
    "diag_ytdlp_verbose": ("ytdlp",),
    "diag_manifest": ("manifest",),
    "diag_manifest_server": ("manifest.server",),
    "diag_subtitles": ("subtitles",),
    "diag_player": ("player",),
    "diag_http": ("http",),
    "diag_updates": ("updates",),
    "diag_cast": ("cast",),
    "diag_cast_traffic": ("cast.raw",),
    "diag_discovery": ("cast.discovery",),
    "diag_service": ("service",),
}
ALL = "diag_all"


def logger(area):
    return logging.getLogger("{}.{}".format(ROOT, area))


def enabled(area):
    """Is the area's diagnostics switch on? For work worth skipping when not."""
    return logger(area).isEnabledFor(logging.INFO)


def configure(switches, handler=None):
    """Set every area's level from {setting id: bool}; add handler once if given."""
    root = logging.getLogger(ROOT)
    root.setLevel(NOTICE)
    root.propagate = False
    if handler is not None and not any(type(h) is type(handler) for h in root.handlers):
        root.addHandler(handler)
    everything = bool(switches.get(ALL))
    for switch, areas in SWITCHES.items():
        level = logging.INFO if everything or switches.get(switch) else NOTICE
        for area in areas:
            logger(area).setLevel(level)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

http_log = logger("http")
_tls_described = False


def safe_url(url):
    """Where a request went, without what must not end up in a log.

    The query string goes (tokens, signatures), and so does most of a
    googlevideo path, which carries the signature as path segments.
    """
    parts = urllib.parse.urlsplit(url)
    path = parts.path or "/"
    segments = path.split("/")
    if parts.hostname and parts.hostname.endswith("googlevideo.com") and len(segments) > 4:
        path = "/".join(segments[:4]) + "/…"
    elif len(path) > 80:
        path = path[:80] + "…"
    return "{}://{}{}".format(parts.scheme, parts.netloc, path)


def describe_tls():
    """Which CA bundle this process verifies against -- the usual suspect on Android."""
    try:
        import certifi
        certifi_path = certifi.where()
    except ImportError:
        certifi_path = None
    defaults = ssl.get_default_verify_paths()
    return "{}; certifi: {}; default context: cafile={} capath={}".format(
        ssl.OPENSSL_VERSION, certifi_path or "not available", defaults.cafile, defaults.capath)


def log_request(method, url, started, status=None, size=None, error=None, note=""):
    """One line per request the add-on itself makes (not yt-dlp's own)."""
    global _tls_described
    if not http_log.isEnabledFor(logging.INFO):
        return
    if not _tls_described:
        _tls_described = True
        http_log.info("TLS: %s", describe_tls())
    took = (time.monotonic() - started) * 1000
    if error is not None:
        http_log.info("%s %s -> %s: %s after %.0f ms%s", method, safe_url(url),
                      type(error).__name__, error, took, note)
    else:
        http_log.info("%s %s -> %s, %s bytes, %.0f ms%s", method, safe_url(url), status,
                      size if size is not None else "?", took, note)
