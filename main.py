# -*- coding: utf-8 -*-
"""Entry point.

Called as plugin://plugin.video.ytdlpcast/?video_id=ID&seek=SECONDS -- the same
shape TubeCast already uses for the YouTube add-on, so the fork only has to
change the plugin id in the URL it builds.
"""

import os
import re
import struct
import sys
import urllib.error
import urllib.parse
import urllib.request

import xbmc
import xbmcaddon
import xbmcgui
import xbmcplugin
import xbmcvfs

from resources.lib import manifest_server, mp4index, paths, resolver

ADDON = xbmcaddon.Addon()
ADDON_ID = ADDON.getAddonInfo("id")
ADDON_NAME = ADDON.getAddonInfo("name")
HANDLE = int(sys.argv[1])


def log(message, level=xbmc.LOGINFO):
    xbmc.log("[{}] {}".format(ADDON_ID, message), level)


def fail(message_id):
    """Tell Kodi the item is unplayable, and say why on screen.

    Resolving to False matters: TubeCast waits on the player and would otherwise
    sit there with the phone showing a video that never starts.
    """
    log(ADDON.getLocalizedString(message_id), xbmc.LOGERROR)
    xbmcgui.Dialog().notification(
        ADDON_NAME, ADDON.getLocalizedString(message_id), xbmcgui.NOTIFICATION_ERROR
    )
    xbmcplugin.setResolvedUrl(HANDLE, False, xbmcgui.ListItem())


def as_seconds(value):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def subtitle_languages():
    """Languages from the setting, or Kodi's own interface language."""
    configured = ADDON.getSettingString("subtitles_langs")
    languages = [code.strip().lower() for code in configured.split(",") if code.strip()]
    if not languages:
        language = xbmc.getLanguage(xbmc.ISO_639_1)
        languages = [language] if language else []
    return languages


def server_base_url():
    return "http://127.0.0.1:{}".format(ADDON.getSettingInt("http_port"))


def server_alive(base_url):
    """True when the service's manifest server answers on loopback."""
    try:
        with urllib.request.urlopen(base_url + manifest_server.HEALTH_PATH, timeout=1) as response:
            return response.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


AUDIO_FMP4, AUDIO_AS_PUBLISHED = 0, 1


def fetch_head(url, headers, length=65535):
    request = urllib.request.Request(url, headers={**dict(headers or {}), "Range": "bytes=0-{}".format(length)})
    with urllib.request.urlopen(request, timeout=15) as response:
        return response.read()


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
    return audio_name, language


def apply_manifest_filters(info, url, headers, video_id):
    """Rewrite the master playlist according to the settings.

    Returns (url_for_isa, served_locally). Whenever the rewrite cannot be
    delivered -- switched off, server down, fetch failed, nothing to change --
    the original URL comes back, which is exactly what 1.0.0 did. A local file
    path is never returned: InputStream Adaptive does not accept one.
    """
    if not ADDON.getSettingBool("rewrite_manifest"):
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

    # Video only: resolution, codec and best-quality. Audio is handled by the swap.
    text, video_changed, _ = resolver.filter_manifest(
        text,
        max_height=ADDON.getSettingInt("max_height"),
        video_codec=ADDON.getSettingInt("video_codec"),
        quality=ADDON.getSettingInt("quality_mode"))

    out_dir = paths.manifest_directory()
    name = re.sub(r"[^A-Za-z0-9_-]", "_", video_id or "video")

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
        return url, False

    master_name = name + ".m3u8"
    with xbmcvfs.File(os.path.join(out_dir, master_name), "w") as handle:
        handle.write(text)
    served = "{}/{}".format(base_url, master_name)
    log("playing a rewritten manifest from {}".format(served))
    return served, True


def main():
    params = dict(urllib.parse.parse_qsl(sys.argv[2].lstrip("?")))
    log("called with {}".format(params))

    video_id = params.get("video_id")
    url = params.get("url") or (resolver.watch_url(video_id) if video_id else None)
    if not url:
        return fail(30010)

    # TubeCast sends "seek"; Tubed calls the same thing "start_offset".
    seek = as_seconds(params.get("seek") or params.get("start_offset"))

    try:
        info = resolver.extract(url)
    except Exception as error:  # noqa: BLE001 - any extractor failure is the same to us
        log("extraction failed: {}".format(error), xbmc.LOGERROR)
        return fail(30011)

    stream, headers = resolver.pick_hls(info)
    adaptive = stream is not None

    if stream is None and ADDON.getSettingBool("allow_progressive"):
        log("no HLS manifest, falling back to a progressive format")
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

    subtitles = resolver.pick_subtitles(
        info, subtitle_languages(), ADDON.getSettingInt("subtitles_mode"))
    if subtitles:
        item.setSubtitles([url for _, url in subtitles])
        log("subtitles: {}".format(", ".join(language for language, _ in subtitles)))

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

    log("playing {} stream, seek={}s".format("adaptive" if adaptive else "progressive", seek))
    xbmcplugin.setResolvedUrl(HANDLE, True, item)


if __name__ == "__main__":
    main()
