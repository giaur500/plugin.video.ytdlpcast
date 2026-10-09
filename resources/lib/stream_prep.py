# -*- coding: utf-8 -*-
"""What the plugin prepares between yt-dlp's answer and handing Kodi the item.

For YouTube's HLS: the master manifest rewritten per the Picture & sound
settings, its packed audio swapped for the fMP4 track, both served to
InputStream Adaptive from the service's loopback server (manifest_server).
For every site: one of the uploader's subtitle tracks -- in the language the
settings ask for -- as a local file Kodi labels by name.

Whatever cannot be done -- server down, a fetch failing, nothing to change --
falls back to what the site published; nothing here may stop playback.
"""

import os
import re
import struct
import time
import urllib.error
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

import xbmc
import xbmcvfs

from . import diag, manifest_server, mp4index, paths, resolver
from .kodilog import log

manifest_log = diag.logger("manifest")
subtitles_log = diag.logger("subtitles")

# Values of the audio_mode setting.
AUDIO_MODE_FMP4, AUDIO_MODE_AS_PUBLISHED = 0, 1

# The manifest, the head of the fMP4 audio file, the subtitles.
WORKERS = 3


def prepare(addon, info, stream, headers, kind, video_id):
    """(url for Kodi, served_locally, subtitle) for the stream pick_stream chose.

    served_locally tells that url is a rewritten manifest on loopback rather
    than the site's own; subtitle is (language, local file) or None. The
    requests this takes -- the manifest, the audio file's head, the subtitles
    -- do not depend on each other, so they run at once and playback waits for
    the slowest instead of their sum. All of them are finished when this
    returns: nothing may outlive the plugin call.
    """
    name = safe_name(video_id)
    # The manifest rewrite answers YouTube's quirks (packed audio silent at the
    # start, duplicate audio groups, no DEFAULT); other sites' manifests have
    # none of them and go to InputStream Adaptive exactly as published.
    rewrite = kind == "HLS" and resolver.is_youtube(info)
    if not rewrite and kind in ("HLS", "DASH"):
        manifest_log.info("not YouTube (%s): playing the %s manifest as published", info.get("extractor_key"), kind)
    with ThreadPoolExecutor(max_workers=WORKERS, thread_name_prefix="ytdlpcast-fetch") as pool:
        # The manifest's requests go first: they are what playback waits for.
        job = _start_rewrite(addon, pool, info, stream, headers) if rewrite else None
        subtitles = _start_subtitles(addon, pool, info)
        served_locally = False
        if job:
            stream, served_locally = _finish_rewrite(addon, job, stream, name)
        subtitle = _finish_subtitles(subtitles, name)
    return stream, served_locally, subtitle


def safe_name(video_id):
    return re.sub(r"[^A-Za-z0-9_-]", "_", video_id or "video")


def server_base_url(addon):
    return "http://127.0.0.1:{}".format(addon.getSettingInt("http_port"))


def server_alive(base_url):
    """True when the service's manifest server answers on loopback."""
    try:
        resolver.fetch(base_url + manifest_server.HEALTH_PATH, timeout=1)
        return True
    except (urllib.error.URLError, OSError, ValueError):
        return False


def fetch_head(url, headers, length=65535):
    return resolver.fetch(url, {**dict(headers or {}), "Range": "bytes=0-{}".format(length)}, timeout=15)


# ---------------------------------------------------------------------------
# The manifest
# ---------------------------------------------------------------------------

class _Rewrite:
    """A rewrite under way: where it will be served, and the requests it waits for."""

    def __init__(self, base_url, manifest, fmp4_wanted):
        self.base_url = base_url
        self.manifest = manifest  # future: the master playlist's text
        self.fmp4_wanted = fmp4_wanted
        self.audio = None  # (url, language, future: the head of the file) of the fMP4 track


def _start_rewrite(addon, pool, info, url, headers):
    """Start the requests the rewrite needs; None when the manifest plays as published."""
    if not addon.getSettingBool("rewrite_manifest"):
        manifest_log.info("rewriting switched off: playing the manifest as published")
        return None
    base_url = server_base_url(addon)
    if not server_alive(base_url):
        log("manifest server not reachable at {}, playing the manifest as published"
            .format(base_url), xbmc.LOGWARNING)
        return None
    job = _Rewrite(base_url, pool.submit(resolver.fetch_manifest, url, headers),
                   addon.getSettingInt("audio_mode") == AUDIO_MODE_FMP4)
    if job.fmp4_wanted:
        audio_url, audio_headers, language = resolver.pick_audio_fmp4(info)
        if audio_url:
            job.audio = (audio_url, language, pool.submit(fetch_head, audio_url, audio_headers))
        else:
            log("no fMP4 audio track; keeping YouTube's packed audio (may start silent)", xbmc.LOGWARNING)
    return job


def _finish_rewrite(addon, job, url, name):
    """Rewrite the master playlist according to the settings.

    Returns (url_for_isa, served_locally). Whenever the rewrite cannot be
    delivered -- fetch failed, nothing to change -- the original URL comes
    back, which is exactly what 1.0.0 did. A local file path is never
    returned: InputStream Adaptive does not accept one.
    """
    try:
        text = job.manifest.result()
    except Exception as error:  # noqa: BLE001 - a network hiccup must not stop playback
        log("could not fetch the manifest for filtering, playing it as published: {}"
            .format(error), xbmc.LOGWARNING)
        return url, False

    out_dir = paths.manifest_directory()
    if diag.enabled("manifest"):
        manifest_log.info("as published: %s", resolver.describe_variants(text))
        original = os.path.join(out_dir, name + ".original.txt")
        with xbmcvfs.File(original, "w") as handle:
            handle.write(text)
        manifest_log.info("original kept as %s", original)

    # The fix for the packed-ADTS start-silence: replace the audio with the
    # fMP4 track, read by the same reader as the video.
    audio_name, language = _build_audio_playlist(job.audio, out_dir, name) if job.audio else (None, None)
    # Without it YouTube's own audio stays, two groups of it and machine
    # dubbing included: then at least one AAC-LC group, no dubbing, as the
    # swap would have had it. "As published" leaves the audio alone.
    fallback = job.fmp4_wanted and not audio_name
    if fallback:
        manifest_log.info("no fMP4 swap: one AAC-LC audio group, machine dubbing dropped")
    text, changed = resolver.filter_manifest(
        text,
        max_height=addon.getSettingInt("max_height"),
        video_codec=addon.getSettingInt("video_codec"),
        quality=addon.getSettingInt("quality_mode"),
        audio_codec=resolver.AUDIO_AAC_LC if fallback else resolver.AUDIO_AS_PUBLISHED,
        drop_auto_dubbed=fallback)
    if audio_name:
        text = resolver.swap_audio_to_fmp4(text, "{}/{}".format(job.base_url, audio_name), language)
        changed = True

    if not changed:
        manifest_log.info("nothing to rewrite: playing the manifest as published")
        return url, False

    master_name = name + ".m3u8"
    with xbmcvfs.File(os.path.join(out_dir, master_name), "w") as handle:
        handle.write(text)
    served = "{}/{}".format(job.base_url, master_name)
    if diag.enabled("manifest"):
        manifest_log.info("rewritten: %s", resolver.describe_variants(text))
    manifest_log.info("served to InputStream Adaptive from %s", served)
    return served, True


def _build_audio_playlist(audio, out_dir, name):
    """Write an fMP4 audio media playlist for this video.

    Returns (file_name, language), or (None, None) when the track cannot be
    indexed, in which case the caller keeps YouTube's own audio.
    """
    audio_url, language, head = audio
    try:
        head = head.result()
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


# ---------------------------------------------------------------------------
# Subtitles
# ---------------------------------------------------------------------------

def subtitle_languages(addon):
    """The languages to look for, most wanted first: the chosen one -- Kodi's
    interface language unless the settings name another -- and the fallback."""
    chosen = addon.getSettingString("subtitles_language")
    if chosen == "system":
        chosen = xbmc.getLanguage(xbmc.ISO_639_1)
    fallback = addon.getSettingString("subtitles_fallback")
    return [code for code in (chosen, "" if fallback == "none" else fallback) if code]


def _start_subtitles(addon, pool, info):
    """Start looking for one subtitle track: (future of _fetch_first, candidates), or None.

    The chosen language, then the fallback, then the first track the author
    uploaded (resolver.pick_subtitles).
    """
    if not addon.getSettingBool("subtitles_enabled"):
        subtitles_log.info("switched off in the settings")
        return None
    languages = subtitle_languages(addon)
    candidates = resolver.pick_subtitles(info, languages)
    subtitles_log.info("uploaded by the author: %s; automatic captions (not used): %d languages; "
                       "wanted: %s, then the first track; trying: %s",
                       ", ".join(sorted(info.get("subtitles") or {})) or "none",
                       len(info.get("automatic_captions") or {}),
                       ", ".join(languages) or "-",
                       ", ".join(language for language, _, _ in candidates) or "nothing")
    return (pool.submit(_fetch_first, candidates), candidates) if candidates else None


def _fetch_first(candidates):
    """(track, problems): track is (language, ext, bytes, seconds) of the first
    candidate that downloads, or None; problems are (language, error) of the
    ones that did not.

    A 429 ends the attempt: it is YouTube refusing this client, not one track,
    and every other request would get it too.
    """
    problems = []
    for language, url, ext in candidates:
        started = time.monotonic()
        try:
            return (language, ext, resolver.fetch(url), time.monotonic() - started), problems
        except Exception as error:  # noqa: BLE001 - the next candidate may still work
            problems.append((language, error))
            if isinstance(error, urllib.error.HTTPError) and error.code == 429:
                break
    return None, problems


def _finish_subtitles(job, name):
    """Save the track that arrived; (language, path), or None.

    Kodi reads the language from the file name: CUtil::GetExternalStreamDetailsFromFilename
    strips the video's base name, splits the rest on " .-" and walks the tokens
    backwards until one converts to an ISO code, so "<video id>.<lang>.srt"
    labels the track.
    """
    if job is None:
        return None
    future, candidates = job
    track, problems = future.result()
    for language, error in problems:
        log("subtitles: {} failed ({}: {}), skipping".format(
            language, type(error).__name__, error), xbmc.LOGWARNING)
    if track is None:
        untried = len(candidates) - len(problems)
        if untried:
            log("subtitles: YouTube answered 429 Too Many Requests; {} more track(s) not tried".format(untried),
                xbmc.LOGWARNING)
        return None
    language, ext, data, took = track
    target = os.path.join(paths.manifest_directory(), "{}.{}.{}".format(name, language, ext))
    with xbmcvfs.File(target, "w") as handle:
        handle.write(data)
    subtitles_log.info("%s: %d bytes in %.0f ms -> %s", language, len(data), took * 1000, os.path.basename(target))
    return language, target
