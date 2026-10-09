# -*- coding: utf-8 -*-
"""Entry point.

As a playable item:
    plugin://plugin.video.ytdlpcast/?video_id=ID&seek=SECONDS   (the cast receiver)
    plugin://plugin.video.ytdlpcast/?url=ANY_YT_DLP_LINK          (queue items, anyone)
resolves the video and hands Kodi a stream. ?req=NONCE asks for the outcome to
be reported back (request_state).

As an action, run with RunPlugin:
    ?action=play_url&url=…&mode=now|queue   the web interface: put a link -- a
                                            video or a whole playlist -- in Kodi's
                                            video playlist and play it
    ?action=update_ytdlp, ?action=pair,     settings buttons
    ?action=web_new_pin

Without anything to play it opens the settings.
"""

import hashlib
import json
import os
import sys
import time
import urllib.parse

import xbmc
import xbmcaddon
import xbmcgui
import xbmcplugin

from resources.lib import (cast_kodi, diag, kodilog, kodiutil, paths, playback_diag, request_state, resolver,
                           stream_prep, web_kodi, ytdlp_info, ytdlp_loader)
from resources.lib.kodilog import log
from resources.lib.kodiutil import ADDON_ID

ADDON = xbmcaddon.Addon()
ADDON_NAME = ADDON.getAddonInfo("name")
HANDLE = int(sys.argv[1])

# Set when someone -- the cast receiver, the web interface -- waits to hear how
# this request went (request_state).
REQUEST = None
STARTED = time.monotonic()
# play_url extracts a single video before queueing it; the queued item reuses
# that extraction instead of repeating it seconds later.
CACHE_SECONDS = 300

resolve_log = diag.logger("resolve")
ytdlp_log = diag.logger("ytdlp")


class YtdlpLog:
    """yt-dlp's own messages, into kodi.log.

    Progress lines ("[youtube] ID: Downloading ...") belong to the "Resolving the
    video" diagnostics, verbose-mode lines ("[debug] ...") to "yt-dlp verbose
    mode"; warnings and errors are always logged.
    """

    @staticmethod
    def debug(message):
        if message.startswith("[debug] "):
            ytdlp_log.info(message)
        else:
            resolve_log.info("yt-dlp: %s", message)

    @staticmethod
    def info(message):
        resolve_log.info("yt-dlp: %s", message)

    @staticmethod
    def warning(message):
        log("yt-dlp: " + message, xbmc.LOGWARNING)

    @staticmethod
    def error(message):
        log("yt-dlp: " + message, xbmc.LOGERROR)


def report(result):
    request_state.report(REQUEST, result)


def fail(message_id, detail=None):
    """Tell Kodi the item is unplayable, and say why on screen.

    Resolving to False matters, and so does telling whoever asked (the phone,
    the web page): they would otherwise keep waiting for a video that never
    starts. detail is yt-dlp's own reason, when there is one.
    """
    message = ADDON.getLocalizedString(message_id) + (": " + detail if detail else "")
    log("{} (after {:.1f} s)".format(message, time.monotonic() - STARTED), xbmc.LOGERROR)
    report("failed: " + message)
    xbmcgui.Dialog().notification(ADDON_NAME, message, xbmcgui.NOTIFICATION_ERROR)
    if HANDLE >= 0:
        xbmcplugin.setResolvedUrl(HANDLE, False, xbmcgui.ListItem())


def _cache_path(url):
    directory = os.path.join(paths.manifest_directory(), "cache")
    os.makedirs(directory, exist_ok=True)
    return os.path.join(directory, hashlib.sha1(url.encode("utf-8")).hexdigest() + ".json")


def cache_info(url, info):
    now = time.time()
    path = _cache_path(url)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump({"at": now, "info": info}, handle)
    for name in os.listdir(os.path.dirname(path)):  # drop what has gone stale
        other = os.path.join(os.path.dirname(path), name)
        try:
            if now - os.path.getmtime(other) > CACHE_SECONDS:
                os.remove(other)
        except OSError:
            pass


def cached_info(url):
    try:
        with open(_cache_path(url), encoding="utf-8") as handle:
            entry = json.load(handle)
    except (OSError, ValueError):
        return None
    return entry["info"] if time.time() - entry.get("at", 0) <= CACHE_SECONDS else None


def extract(url, switches, flat_playlists=False):
    """yt-dlp's info for url, timed and logged. Raises whatever yt-dlp raises."""
    started = time.monotonic()
    verbose = switches.get(diag.ALL) or switches.get("diag_ytdlp_verbose")
    try:
        info = resolver.extract(url, logger=YtdlpLog(), verbose=verbose, flat_playlists=flat_playlists)
    except Exception as error:
        log("extraction failed after {:.1f} s: {}: {}".format(
            time.monotonic() - started, type(error).__name__, error), xbmc.LOGERROR)
        raise
    if resolver.is_playlist(info):
        what = "playlist"
    else:
        what = resolver.describe_formats(info) if diag.enabled("resolve") else ""
    resolve_log.info("extracted in %.1f s: %s", time.monotonic() - started, what)
    return info


def as_seconds(value):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def prepare_ytdlp():
    """Make yt-dlp importable; return (version, source), or (None, None) if no copy works.

    The downloaded release is preferred. Should importing it fail anyway -- it
    passed validation, but validation is static -- the copy shipped inside the
    add-on is used instead, so a bad download cannot stop playback.
    """
    store = paths.ytdlp_directory()
    started = time.monotonic()
    version, source = ytdlp_loader.activate(store, paths.bundled_ytdlp())
    try:
        import yt_dlp  # noqa: F401 - imported for its side effect of loading
        resolve_log.info("yt-dlp %s (%s) imported in %.1f s", version, source, time.monotonic() - started)
        if source == "downloaded":
            ytdlp_loader.mark_broken(store, False)
        return version, source
    except Exception as error:  # noqa: BLE001 - any import failure means the same
        if source == "bundled":
            log("BUG: the bundled yt-dlp does not import ({}: {})".format(
                type(error).__name__, error), xbmc.LOGERROR)
            return None, None
        log("downloaded yt-dlp {} does not import ({}: {}); using the bundled copy".format(
            version, type(error).__name__, error), xbmc.LOGERROR)
    for name in [name for name in sys.modules if name == "yt_dlp" or name.startswith("yt_dlp.")]:
        del sys.modules[name]
    version, _ = ytdlp_loader.activate_bundled(paths.bundled_ytdlp(), store)
    import yt_dlp  # noqa: F401,F811
    ytdlp_loader.mark_broken(store, True)
    ytdlp_info.refresh(ADDON)
    return version, "bundled, fallback"


def show_settings():
    """Opened from the Kodi UI with nothing to play.

    This add-on is a playback back end rather than a browsable source, so there
    is no listing to show. Open the settings straight away and leave one entry
    behind, so the folder is not empty and the settings stay a click away.

    The directory is ended as succeeded on purpose: failing it makes Kodi log an
    error, bounce back to the previous folder and let the caller raise an error
    message -- exactly what a plain launch should not do.
    """
    ytdlp_info.refresh(ADDON)
    ADDON.openSettings()
    if HANDLE < 0:
        return
    item = xbmcgui.ListItem(label=ADDON.getLocalizedString(30013))
    item.setArt({"icon": "DefaultAddonService.png"})
    xbmcplugin.addDirectoryItem(
        HANDLE, "plugin://{}/".format(ADDON_ID), item, isFolder=False)
    xbmcplugin.endOfDirectory(HANDLE, succeeded=True, cacheToDisc=False)


def pick_stream(info):
    """(url, headers, kind): HLS, then DASH, then a single file (with both tracks, else audio)."""
    stream, headers = resolver.pick_hls(info)
    if stream:
        return stream, headers, "HLS"
    stream, headers = resolver.pick_dash(info)
    if stream:
        return stream, headers, "DASH"
    if not ADDON.getSettingBool("allow_progressive"):
        return None, None, None
    stream, headers = resolver.pick_progressive(info)
    if stream:
        return stream, headers, "file"
    stream, headers = resolver.pick_audio_only(info)
    return (stream, headers, "audio file") if stream else (None, None, None)


def resolve_item(params, switches):
    """The playable item: resolve the video and hand Kodi a stream."""
    video_id = params.get("video_id")
    url = params.get("url") or resolver.watch_url(video_id)

    ytdlp_version, ytdlp_source = prepare_ytdlp()
    if not ytdlp_version:
        return fail(30011)

    # The receiver sends "seek"; Tubed calls the same thing "start_offset".
    seek = as_seconds(params.get("seek") or params.get("start_offset"))

    info = cached_info(url)
    if info:
        resolve_log.info("using the extraction play_url made moments ago")
    else:
        try:
            info = extract(url, switches, flat_playlists=True)
            if resolver.is_playlist(info):
                # A playlist link handed to a playable item: play its first video.
                entries = resolver.playlist_entries(info)
                if not entries:
                    return fail(30012)
                resolve_log.info("a playlist given as one item: playing its first video")
                url = entries[0][0]
                info = extract(url, switches)
        except Exception as error:  # noqa: BLE001 - any extractor failure is the same to us
            return fail(30011, resolver.short_error(error))

    stream, headers, kind = pick_stream(info)
    if not stream:
        return fail(30012)
    if kind in ("HLS", "DASH"):
        warn_if_isa_disabled()
    video = video_id or info.get("id")
    stream, served_locally, subtitle = stream_prep.prepare(ADDON, info, stream, headers, kind, video)

    # A link with ?t=… starts there, unless the caller asked for a position.
    if not seek and info.get("start_time"):
        seek = as_seconds(info.get("start_time"))
    item = _list_item(info, stream, headers, kind, served_locally, subtitle[1] if subtitle else None, seek)

    log("play {} ({}): yt-dlp {} ({}), {}, manifest {}, subtitles {}, seek {}s, ready in {:.1f} s".format(
        video, info.get("extractor_key") or "?", ytdlp_version, ytdlp_source, kind,
        "rewritten" if served_locally else ("as published" if kind in ("HLS", "DASH") else "-"),
        subtitle[0] if subtitle else "-", seek, time.monotonic() - STARTED))
    playback_diag.mark_playing(video, kind, served_locally, source=params.get("url") or url,
                               site=info.get("extractor_key"), title=info.get("title") or "")
    report("ok")
    xbmcplugin.setResolvedUrl(HANDLE, True, item)


def manifest_type_reason():
    """Why inputstream.adaptive.manifest_type goes with the item, or None when it does not."""
    if kodiutil.isa_needs_manifest_type():
        return "InputStream Adaptive {} needs it".format(kodiutil.inputstream_adaptive()[0] or "before 21")
    if ADDON.getSettingBool("legacy_manifest_type"):
        return "forced in the settings"
    return None


def warn_if_isa_disabled():
    """Say so when InputStream Adaptive is disabled: Kodi then plays the manifest
    itself, in its lowest quality, without a word in the log. Playback goes on."""
    version, enabled = kodiutil.inputstream_adaptive()
    if enabled:
        return
    log("InputStream Adaptive {}is disabled: Kodi plays the manifest itself, in the lowest quality; "
        "enable it in Add-ons > My add-ons > VideoPlayer InputStream".format(version + " " if version else ""),
        xbmc.LOGWARNING)
    xbmcgui.Dialog().notification(ADDON_NAME, ADDON.getLocalizedString(30257), xbmcgui.NOTIFICATION_WARNING, 8000)


def _list_item(info, stream, headers, kind, served_locally, subtitle_file, seek):
    """What Kodi plays: the stream, what InputStream Adaptive needs for it, subtitles, title, start position."""
    adaptive = kind in ("HLS", "DASH")
    item = xbmcgui.ListItem(path=stream if adaptive else resolver.kodi_url(stream, headers))
    item.setContentLookup(False)

    if adaptive:
        item.setMimeType(resolver.HLS_MIME if kind == "HLS" else resolver.DASH_MIME)
        item.setProperty("inputstream", "inputstream.adaptive")
        encoded = resolver.encode_headers(headers)
        if encoded:
            # Segments always come from the site; the manifest only does when it
            # was not rewritten and served from loopback.
            item.setProperty("inputstream.adaptive.stream_headers", encoded)
            if not served_locally:
                item.setProperty("inputstream.adaptive.manifest_headers", encoded)
        # ISA 21 detects the type from the mime type (and warns about the
        # property); ISA 20, Kodi 20 Nexus's, opens no manifest without it.
        reason = manifest_type_reason()
        if reason:
            item.setProperty("inputstream.adaptive.manifest_type", "hls" if kind == "HLS" else "mpd")
            resolve_log.info("manifest_type sent: %s", reason)

    if subtitle_file:
        item.setSubtitles([subtitle_file])

    tag = item.getVideoInfoTag()
    tag.setTitle(info.get("title") or "")
    tag.setPlot(info.get("description") or "")
    duration = as_seconds(info.get("duration"))
    tag.setDuration(duration)

    thumbnail = info.get("thumbnail")
    if thumbnail:
        item.setArt({"thumb": thumbnail, "icon": thumbnail})

    # Resuming needs both halves; Kodi ignores ResumeTime on its own.
    if seek > 0 and duration > 0:
        item.setProperty("ResumeTime", str(seek))
        item.setProperty("TotalTime", str(duration))
    return item


def play_url(params, switches):
    """Web interface: put a link in Kodi's video playlist and play it.

    A playlist link becomes one item per video, each resolved only when Kodi
    gets to it; a single video becomes one item, its extraction kept for the
    moment the item is resolved. mode=now replaces the playlist and plays it,
    mode=queue appends (and starts playing if nothing plays).
    """
    url = (params.get("url") or "").strip()
    mode = params.get("mode") or "now"
    if not url:
        report("failed: no link")
        return
    if not prepare_ytdlp()[0]:
        return fail(30011)
    try:
        info = extract(url, switches, flat_playlists=True)
    except Exception as error:  # noqa: BLE001 - reported to the web page and on screen
        return fail(30011, resolver.short_error(error))

    if resolver.is_playlist(info):
        entries = resolver.playlist_entries(info)
        if not entries:
            return fail(30012)
    else:
        cache_info(url, info)
        entries = [(url, info.get("title") or url, info.get("thumbnail"))]

    playlist = xbmc.PlayList(xbmc.PLAYLIST_VIDEO)
    if mode == "now":
        playlist.clear()
    first = playlist.size()
    for index, (entry_url, title, thumbnail) in enumerate(entries):
        query = {"url": entry_url}
        if index == 0:
            query["req"] = REQUEST  # the web page follows its request through the first item
        item = xbmcgui.ListItem(label=title)
        item.getVideoInfoTag().setTitle(title)
        if thumbnail:
            item.setArt({"thumb": thumbnail, "icon": thumbnail})
        item.setProperty("IsPlayable", "true")
        playlist.add("plugin://{}/?{}".format(ADDON_ID, urllib.parse.urlencode(query)), item)
    log("queue ({}): {} item(s) from {} {}".format(
        mode, len(entries), info.get("extractor_key") or "?", "playlist" if resolver.is_playlist(info) else "video"))
    report("queued: {}".format(len(entries)))

    player = xbmc.Player()
    if mode == "now" or not player.isPlaying():
        player.play(playlist, startpos=first)


def main():
    global REQUEST
    switches = kodilog.apply(ADDON)
    params = dict(urllib.parse.parse_qsl(sys.argv[2].lstrip("?")))
    resolve_log.info("called with %s", params)
    REQUEST = params.get("req")

    # Actions, not playable items -- handled before anything else.
    action = params.get("action")
    if action == "update_ytdlp":
        return ytdlp_info.update_dialog(ADDON)
    if action == "pair":
        return cast_kodi.pair_with_tv_code()
    if action == "play_url":
        return play_url(params, switches)
    if action == "web_new_pin":
        state = web_kodi.new_pin()
        log("web interface: new PIN set; every browser has to enter it again")
        return xbmcgui.Dialog().notification(ADDON_NAME, ADDON.getLocalizedString(30244).format(state["pin"]),
                                             xbmcgui.NOTIFICATION_INFO, 8000)

    if not (params.get("url") or params.get("video_id")):
        return show_settings()
    return resolve_item(params, switches)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Kodi logs the traceback; whoever asked must still hear it failed.
        report("failed: the plugin crashed, see kodi.log")
        raise
