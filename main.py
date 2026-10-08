# -*- coding: utf-8 -*-
"""Entry point.

Called as plugin://plugin.video.ytdlpcast/?video_id=ID&seek=SECONDS -- by the
add-on's own cast receiver (which adds &cast=NONCE, see cast_kodi.KodiPlayback),
or by anything else that wants a YouTube video played through yt-dlp. Without a
video it opens the settings; ?action=update_ytdlp and ?action=pair are the
settings buttons.
"""

import os
import re
import struct
import sys
import time
import urllib.error
import urllib.parse

import xbmc
import xbmcaddon
import xbmcgui
import xbmcplugin
import xbmcvfs

from resources.lib import (cast_kodi, diag, kodilog, manifest_server, mp4index, paths, playback_diag,
                           resolver, ytdlp_info, ytdlp_loader)

ADDON = xbmcaddon.Addon()
ADDON_ID = ADDON.getAddonInfo("id")
ADDON_NAME = ADDON.getAddonInfo("name")
HANDLE = int(sys.argv[1])

# Set when the cast receiver asked for this playback: it waits to hear how
# resolving went (cast_kodi.KodiPlayback.resolution).
CAST_NONCE = None
STARTED = time.monotonic()

resolve_log = diag.logger("resolve")
manifest_log = diag.logger("manifest")
subtitles_log = diag.logger("subtitles")
ytdlp_log = diag.logger("ytdlp")


def log(message, level=xbmc.LOGINFO):
    """The lines logged whatever the Diagnostics switches say: summaries, warnings, errors."""
    xbmc.log("[{}] {}".format(ADDON_ID, message), level)


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


def report_to_cast(result):
    if CAST_NONCE:
        cast_kodi.mark_resolved(CAST_NONCE, result)


def fail(message_id):
    """Tell Kodi the item is unplayable, and say why on screen.

    Resolving to False matters, and so does telling the cast receiver: the phone
    would otherwise keep showing a video that never starts.
    """
    log("{} (after {:.1f} s)".format(ADDON.getLocalizedString(message_id), time.monotonic() - STARTED),
        xbmc.LOGERROR)
    report_to_cast("failed: " + ADDON.getLocalizedString(message_id))
    xbmcgui.Dialog().notification(
        ADDON_NAME, ADDON.getLocalizedString(message_id), xbmcgui.NOTIFICATION_ERROR
    )
    xbmcplugin.setResolvedUrl(HANDLE, False, xbmcgui.ListItem())


def as_seconds(value):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def server_base_url():
    return "http://127.0.0.1:{}".format(ADDON.getSettingInt("http_port"))


def server_alive(base_url):
    """True when the service's manifest server answers on loopback."""
    try:
        resolver.fetch(base_url + manifest_server.HEALTH_PATH, timeout=1)
        return True
    except (urllib.error.URLError, OSError, ValueError):
        return False


AUDIO_FMP4, AUDIO_AS_PUBLISHED = 0, 1


def fetch_head(url, headers, length=65535):
    return resolver.fetch(url, {**dict(headers or {}), "Range": "bytes=0-{}".format(length)}, timeout=15)


def build_audio_playlist(info, out_dir, name):
    """Write an fMP4 audio media playlist for this video; return its file name.

    Returns (file_name, language) or (None, None) when the video has no fMP4
    audio to swap in, in which case the caller keeps YouTube's own audio.
    """
    audio_url, audio_headers, language = resolver.pick_audio_fmp4(info)
    if not audio_url:
        log("no fMP4 audio track; keeping YouTube's packed audio (may start silent)",
            xbmc.LOGWARNING)
        return None, None
    try:
        head = fetch_head(audio_url, audio_headers)
        init = mp4index.init_range(head)
        segments = mp4index.parse_sidx(head)
    except (urllib.error.URLError, OSError, ValueError, struct.error) as error:
        # Expected: the audio moved, the head was short, the boxes were odd.
        log("could not index the fMP4 audio, keeping YouTube's: {}".format(error), xbmc.LOGWARNING)
        return None, None
    except Exception as error:  # noqa: BLE001 - never break playback, but say it loudly
        # A bug on our side (a missing import once hid here for a whole release).
        # Still fall back so the video plays, but at error level and named.
        log("BUG indexing the fMP4 audio ({}: {}); keeping YouTube's audio"
            .format(type(error).__name__, error), xbmc.LOGERROR)
        return None, None
    playlist = resolver.build_fmp4_audio_playlist(audio_url, init, segments)
    audio_name = name + ".audio.m3u8"
    with xbmcvfs.File(os.path.join(out_dir, audio_name), "w") as handle:
        handle.write(playlist)
    itag = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(audio_url).query)).get("itag", "?")
    manifest_log.info("fMP4 audio: itag %s, language %s, init bytes %d-%d, %d segments, %.0f s",
                      itag, language or "?", init[0], init[1], len(segments),
                      sum(duration for _, _, duration in segments))
    return audio_name, language


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
    version, _ = ytdlp_loader.activate_bundled(paths.bundled_ytdlp())
    import yt_dlp  # noqa: F401,F811
    ytdlp_loader.mark_broken(store, True)
    ytdlp_info.refresh(ADDON)
    return version, "bundled, fallback"


def update_ytdlp_now():
    """Settings button: check the configured channel right away and report."""
    channel = ytdlp_loader.SETTING_CHANNELS[ADDON.getSettingInt("ytdlp_channel")]
    notify = xbmcgui.Dialog().notification
    notify(ADDON_NAME, ADDON.getLocalizedString(30174), xbmcgui.NOTIFICATION_INFO, 3000)
    result = ytdlp_loader.update(paths.ytdlp_directory(), channel)
    log("manual yt-dlp update on {}: {}".format(channel, result))
    ytdlp_info.refresh(ADDON)
    if result.status == ytdlp_loader.UPDATED:
        message, icon = ADDON.getLocalizedString(30171).format(result.version), xbmcgui.NOTIFICATION_INFO
    elif result.status == ytdlp_loader.UP_TO_DATE:
        message, icon = ADDON.getLocalizedString(30170).format(result.version), xbmcgui.NOTIFICATION_INFO
    elif result.status in (ytdlp_loader.REJECTED, ytdlp_loader.SKIPPED):
        message, icon = ADDON.getLocalizedString(30172).format(result.reason), xbmcgui.NOTIFICATION_WARNING
    else:
        message, icon = ADDON.getLocalizedString(30173).format(result.reason), xbmcgui.NOTIFICATION_ERROR
    notify(ADDON_NAME, message, icon, 6000)


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


def safe_name(video_id):
    return re.sub(r"[^A-Za-z0-9_-]", "_", video_id or "video")


def download_subtitles(info, name):
    """Save the uploader's subtitles as local files; return their paths.

    Kodi reads the language from the file name: CUtil::GetExternalStreamDetailsFromFilename
    strips the video's base name, splits the rest on " .-" and walks the tokens
    backwards until one converts to an ISO code, so "<video id>.<lang>.srt"
    labels each track. Kodi then ranks these external tracks against its own
    "Preferred subtitle language" setting and selects the best match itself.
    """
    if not ADDON.getSettingBool("subtitles_enabled"):
        subtitles_log.info("switched off in the settings")
        return []

    out_dir = paths.manifest_directory()
    chosen = resolver.pick_subtitles(info)
    subtitles_log.info("uploaded by the author: %s; automatic captions (not used): %d languages; downloading: %s",
                       ", ".join(sorted(info.get("subtitles") or {})) or "none",
                       len(info.get("automatic_captions") or {}),
                       ", ".join(language for language, _ in chosen) or "nothing")
    saved = []
    for language, url in chosen:
        target = os.path.join(out_dir, "{}.{}.srt".format(name, language))
        started = time.monotonic()
        try:
            data = resolver.fetch_subtitle(url)
        except Exception as error:  # noqa: BLE001 - one bad track must not stop playback
            log("subtitles: {} failed ({}: {}), skipping".format(
                language, type(error).__name__, error), xbmc.LOGWARNING)
            continue
        with xbmcvfs.File(target, "w") as handle:
            handle.write(data)
        subtitles_log.info("%s: %d bytes in %.0f ms -> %s", language, len(data),
                           (time.monotonic() - started) * 1000, os.path.basename(target))
        saved.append(target)
    return saved


def apply_manifest_filters(info, url, headers, video_id):
    """Rewrite the master playlist according to the settings.

    Returns (url_for_isa, served_locally). Whenever the rewrite cannot be
    delivered -- switched off, server down, fetch failed, nothing to change --
    the original URL comes back, which is exactly what 1.0.0 did. A local file
    path is never returned: InputStream Adaptive does not accept one.
    """
    if not ADDON.getSettingBool("rewrite_manifest"):
        manifest_log.info("rewriting switched off: playing the manifest as published")
        return url, False

    base_url = server_base_url()
    if not server_alive(base_url):
        log("manifest server not reachable at {}, playing the manifest as published"
            .format(base_url), xbmc.LOGWARNING)
        return url, False

    try:
        text = resolver.fetch_manifest(url, headers)
    except Exception as error:  # noqa: BLE001 - a network hiccup must not stop playback
        log("could not fetch the manifest for filtering, playing it as published: {}"
            .format(error), xbmc.LOGWARNING)
        return url, False

    out_dir = paths.manifest_directory()
    name = safe_name(video_id)
    if diag.enabled("manifest"):
        manifest_log.info("as published: %s", resolver.describe_variants(text))
        original = os.path.join(out_dir, name + ".original.txt")
        with xbmcvfs.File(original, "w") as handle:
            handle.write(text)
        manifest_log.info("original kept as %s", original)

    # Video only: resolution, codec and best-quality. Audio is handled by the swap.
    text, video_changed, _ = resolver.filter_manifest(
        text,
        max_height=ADDON.getSettingInt("max_height"),
        video_codec=ADDON.getSettingInt("video_codec"),
        quality=ADDON.getSettingInt("quality_mode"))

    audio_changed = False
    if ADDON.getSettingInt("audio_mode") == AUDIO_FMP4:
        # The fix for the packed-ADTS start-silence: replace the audio with the
        # fMP4 track, read by the same reader as the video.
        audio_name, language = build_audio_playlist(info, out_dir, name)
        if audio_name:
            audio_uri = "{}/{}".format(base_url, audio_name)
            text = resolver.swap_audio_to_fmp4(text, audio_uri, language)
            audio_changed = True

    if not (video_changed or audio_changed):
        manifest_log.info("nothing to rewrite: playing the manifest as published")
        return url, False

    master_name = name + ".m3u8"
    with xbmcvfs.File(os.path.join(out_dir, master_name), "w") as handle:
        handle.write(text)
    served = "{}/{}".format(base_url, master_name)
    if diag.enabled("manifest"):
        manifest_log.info("rewritten: %s", resolver.describe_variants(text))
    manifest_log.info("served to InputStream Adaptive from %s", served)
    return served, True


def main():
    global CAST_NONCE
    switches = kodilog.apply(ADDON)
    params = dict(urllib.parse.parse_qsl(sys.argv[2].lstrip("?")))
    resolve_log.info("called with %s", params)
    CAST_NONCE = params.get("cast")

    # The settings buttons, not playback requests -- handled before anything else.
    if params.get("action") == "update_ytdlp":
        return update_ytdlp_now()
    if params.get("action") == "pair":
        return cast_kodi.pair_with_tv_code()

    video_id = params.get("video_id")
    url = params.get("url") or (resolver.watch_url(video_id) if video_id else None)
    if not url:
        return show_settings()

    ytdlp_version, ytdlp_source = prepare_ytdlp()
    if not ytdlp_version:
        return fail(30011)

    # The receiver sends "seek"; Tubed calls the same thing "start_offset".
    seek = as_seconds(params.get("seek") or params.get("start_offset"))

    extract_started = time.monotonic()
    verbose = switches.get(diag.ALL) or switches.get("diag_ytdlp_verbose")
    try:
        info = resolver.extract(url, logger=YtdlpLog(), verbose=verbose)
    except Exception as error:  # noqa: BLE001 - any extractor failure is the same to us
        log("extraction failed after {:.1f} s: {}: {}".format(
            time.monotonic() - extract_started, type(error).__name__, error), xbmc.LOGERROR)
        return fail(30011)
    resolve_log.info("extracted in %.1f s: %s", time.monotonic() - extract_started,
                     resolver.describe_formats(info) if diag.enabled("resolve") else "")

    stream, headers = resolver.pick_hls(info)
    adaptive = stream is not None

    if stream is None and ADDON.getSettingBool("allow_progressive"):
        log("no HLS manifest, falling back to a progressive format", xbmc.LOGWARNING)
        stream, headers = resolver.pick_progressive(info)

    if stream is None:
        return fail(30012)

    served_locally = False
    if adaptive:
        stream, served_locally = apply_manifest_filters(
            info, stream, headers, video_id or info.get("id"))

    item = xbmcgui.ListItem(path=stream)
    item.setContentLookup(False)

    if adaptive:
        item.setMimeType(resolver.HLS_MIME)
        item.setProperty("inputstream", "inputstream.adaptive")
        encoded = resolver.encode_headers(headers)
        if encoded:
            # Segments always come from YouTube; the manifest only does when it
            # was not rewritten and served from loopback.
            item.setProperty("inputstream.adaptive.stream_headers", encoded)
            if not served_locally:
                item.setProperty("inputstream.adaptive.manifest_headers", encoded)
        # ISA 21 detects the type from the mime type; the explicit property is
        # only there for older builds that still want to be told.
        if ADDON.getSettingBool("legacy_manifest_type"):
            item.setProperty("inputstream.adaptive.manifest_type", "hls")

    subtitle_files = download_subtitles(info, safe_name(video_id or info.get("id")))
    if subtitle_files:
        item.setSubtitles(subtitle_files)

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

    video = video_id or info.get("id")
    log("play {}: yt-dlp {} ({}), {}, manifest {}, {} subtitle track(s), seek {}s, ready in {:.1f} s{}".format(
        video, ytdlp_version, ytdlp_source, "HLS" if adaptive else "progressive",
        "rewritten" if served_locally else "as published", len(subtitle_files), seek,
        time.monotonic() - STARTED, ", for a cast" if CAST_NONCE else ""))
    playback_diag.mark_playing(video, "HLS" if adaptive else "progressive", served_locally)
    report_to_cast("ok")
    xbmcplugin.setResolvedUrl(HANDLE, True, item)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Kodi logs the traceback; the cast receiver must still hear it failed.
        report_to_cast("failed: the plugin crashed, see kodi.log")
        raise
