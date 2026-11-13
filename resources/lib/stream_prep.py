# -*- coding: utf-8 -*-
"""What the plugin prepares between yt-dlp's answer and handing Kodi the item.

For YouTube's HLS: the master manifest rewritten per the Picture & sound
settings, its packed audio swapped for the fMP4 track, both served to
InputStream Adaptive from the service's loopback server (manifest_server).
A fresh video is checked first: when the server YouTube names is not delivering
yet, the media comes through the next one. A live stream that has just ended
has no manifest at all, so one is built from its fragments.
For every site: one of the uploader's subtitle tracks -- in the language the
settings ask for -- as a local file Kodi labels by name. YouTube's automatic
captions come as json3 and are drawn as YouTube's player draws them
(youtube_captions), or as plain lines if the settings say so.

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
import xbmcgui
import xbmcvfs

from . import diag, kodiutil, manifest_server, mp4index, paths, resolver, youtube_captions
from .kodilog import log
from .kodiutil import ADDON_ID, HOME

manifest_log = diag.logger("manifest")
subtitles_log = diag.logger("subtitles")

# Values of the audio_mode setting.
AUDIO_MODE_FMP4, AUDIO_MODE_AS_PUBLISHED = 0, 1
# Values of the subtitles_automatic_look setting.
LOOK_YOUTUBE, LOOK_PLAIN = 0, 1

# The manifest, the head of the fMP4 audio file, the subtitles; then a fresh
# video's checks or copies of its playlists, a finished stream's fragments.
WORKERS = 8

# What pick_stream calls a live stream that has just ended (resolver.post_live_video).
POST_LIVE = "post-live"

# How long a fresh video's segment may take to answer. A server that has them
# answers in a fraction of a second; one that has not holds the request for
# half a minute and then says 503, while InputStream Adaptive waits in silence.
PROBE_SECONDS = 4
# Where a live stream that has just ended may start, when its very first fragments do not answer yet.
FIRST_FRAGMENTS_TRIED = 3

# When the service's manifest server does not answer, this plugin call serves
# the manifest itself, until InputStream Adaptive has fetched it or this long.
STANDBY_SECONDS = 30
_standby = None  # {"server", "names", "served"}: see release_standby

# A 429 to a subtitle request is YouTube refusing this address for a while
# (Google's "Sorry..." page); every further request only feeds the block. So
# none is made for this long -- across plugin calls, hence a window property.
REFUSED_PROPERTY = ADDON_ID + ".subtitles.refused"
COOLDOWN_SECONDS = 30 * 60


def prepare(addon, info, stream, headers, kind, video_id):
    """(url for Kodi, served_locally, subtitle) for the stream pick_stream chose.

    served_locally tells that url is a rewritten manifest on loopback rather
    than the site's own; subtitle is (language, local file) or None. The
    requests this takes -- the manifest, the audio file's head, the subtitles
    -- do not depend on each other, so they run at once and playback waits for
    the slowest instead of their sum. All of them are finished when this
    returns: nothing may outlive the plugin call. url is None only for a live
    stream that has just ended and whose playlists could not be built.
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
        if kind == POST_LIVE:
            stream = _build_post_live(addon, pool, info, headers, name)
            served_locally = bool(stream)
        elif job:
            stream, served_locally = _finish_rewrite(addon, pool, job, stream, name)
        subtitle = _finish_subtitles(subtitles, name, resolver.is_youtube(info))
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


def fetch_head(url, headers, length=65535, timeout=15):
    return resolver.fetch(url, {**dict(headers or {}), "Range": "bytes=0-{}".format(length)}, timeout=timeout)


def _start_standby(service_url):
    """Serve this playback's manifest from the plugin call itself; its base URL, or None.

    The service's server is not answering (its port taken, the service not
    running). A loopback server on a port the system picks stands in until
    release_standby, which the plugin calls once the item went to Kodi.
    """
    global _standby
    server = manifest_server.ManifestServer(paths.manifest_directory(), 0)
    served = set()
    server.set(served=served)
    try:
        server.start()
    except OSError as error:
        log("manifest server not reachable at {} and none could be started here ({}); playing the manifest "
            "as published".format(service_url, error), xbmc.LOGWARNING)
        return None
    _standby = {"server": server, "names": set(), "served": served}
    log("manifest server not reachable at {}; this playback's manifest is served by the plugin itself at {}"
        .format(service_url, server.base_url), xbmc.LOGWARNING)
    return server.base_url


def release_standby(wait=True, timeout=STANDBY_SECONDS):
    """Stop the plugin's own manifest server, if this call started one.

    With wait, first until InputStream Adaptive has fetched every playlist it
    serves -- the master, the fMP4 audio -- or timeout. Called after the item
    went to Kodi: Kodi starts playing then, while this call still runs.
    """
    global _standby
    if _standby is None:
        return
    standby, _standby = _standby, None
    missing = standby["names"] - standby["served"]
    started = time.monotonic()
    monitor = xbmc.Monitor()
    while wait and missing and time.monotonic() - started < timeout and not monitor.waitForAbort(0.1):
        missing = standby["names"] - standby["served"]
    standby["server"].stop()
    if wait and missing:
        log("the plugin's manifest server stopped after {:.0f} s; never fetched: {}".format(
            time.monotonic() - started, ", ".join(sorted(missing))), xbmc.LOGWARNING)
    else:
        manifest_log.info("the plugin's manifest server stopped after %.1f s", time.monotonic() - started)


# ---------------------------------------------------------------------------
# The manifest
# ---------------------------------------------------------------------------

class _Rewrite:
    """A rewrite under way: where it will be served, and the requests it waits for."""

    def __init__(self, base_url, manifest, headers, fmp4_wanted, minimal, fresh):
        self.base_url = base_url
        self.manifest = manifest  # future: the master playlist's text
        self.headers = headers
        self.fmp4_wanted = fmp4_wanted
        self.minimal = minimal  # rewriting switched off: only YouTube's subtitle renditions go
        self.fresh = fresh  # its media may not have reached the server YouTube names
        self.audio = None  # (url, headers, language, future: the head of the file) of the fMP4 track


def _start_rewrite(addon, pool, info, url, headers):
    """Start the requests the rewrite needs; None when the manifest plays as published.

    With rewriting switched off YouTube's own subtitle renditions still go
    (resolver.strip_subtitle_renditions), and nothing else changes. Without
    the service's server this plugin call serves the manifest (_start_standby):
    as published, InputStream Adaptive would get those renditions.
    """
    minimal = not addon.getSettingBool("rewrite_manifest")
    if minimal:
        manifest_log.info("rewriting switched off: only YouTube's subtitle renditions are removed")
    base_url = server_base_url(addon)
    if not server_alive(base_url):
        base_url = _start_standby(base_url)
        if not base_url:
            return None
    job = _Rewrite(base_url, pool.submit(resolver.fetch_manifest, url, headers), headers,
                   not minimal and addon.getSettingInt("audio_mode") == AUDIO_MODE_FMP4, minimal,
                   not minimal and resolver.is_fresh(info))
    if job.fmp4_wanted:
        audio_url, audio_headers, language = resolver.pick_audio_fmp4(info)
        if audio_url:
            job.audio = (audio_url, audio_headers, language, pool.submit(fetch_head, audio_url, audio_headers))
        else:
            log("no fMP4 audio track; keeping YouTube's packed audio (may start silent)", xbmc.LOGWARNING)
    return job


def _finish_rewrite(addon, pool, job, url, name):
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

    host = None
    variant_names = []
    if job.minimal:
        audio_name = None
        text, changed = resolver.strip_subtitle_renditions(text)
    else:
        host = _server_for_fresh(addon, pool, text, job.headers) if job.fresh else None
        # The fix for the packed-ADTS start-silence: replace the audio with the
        # fMP4 track, read by the same reader as the video.
        audio_name, language = _build_audio_playlist(job.audio, out_dir, name, host) if job.audio else (None, None)
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
            quality=_quality(addon, local_playlists=bool(host)),
            audio_codec=resolver.AUDIO_AAC_LC if fallback else resolver.AUDIO_AS_PUBLISHED,
            drop_auto_dubbed=fallback)
        if audio_name:
            text = resolver.swap_audio_to_fmp4(text, "{}/{}".format(job.base_url, audio_name), language)
            changed = True
        if host:
            text, variant_names = _read_variants_through(pool, text, host, job, name)
            changed = True

    if not changed:
        manifest_log.info("nothing to rewrite: playing the manifest as published")
        return url, False

    master_name = name + ".m3u8"
    with xbmcvfs.File(os.path.join(out_dir, master_name), "w") as handle:
        handle.write(text)
    if _standby is not None:
        _standby["names"].update(n for n in (master_name, audio_name, *variant_names) if n)
    served = "{}/{}".format(job.base_url, master_name)
    if diag.enabled("manifest"):
        manifest_log.info("rewritten: %s", resolver.describe_variants(text))
    manifest_log.info("served to InputStream Adaptive from %s", served)
    return served, True


def _quality(addon, local_playlists):
    """The quality_mode to filter with. The plugin's own server is gone once the
    playlists it serves were fetched, so with local variants only one may remain:
    InputStream Adaptive could not switch to another."""
    return resolver.QUALITY_BEST if local_playlists and _standby is not None else addon.getSettingInt("quality_mode")


def _delivers(url, headers):
    """True when url answers within PROBE_SECONDS."""
    try:
        resolver.fetch(url, {**dict(headers or {}), "Range": "bytes=0-1023"}, timeout=PROBE_SECONDS)
        return True
    except Exception as error:  # noqa: BLE001 - whatever went wrong, it is not delivering
        manifest_log.info("not delivering: %s: %s", type(error).__name__, error)
        return False


def _server_for_fresh(addon, pool, master, headers):
    """The server to fetch a fresh video's media from instead of the one YouTube names, or None.

    A video minutes old may not have reached the nearest server, which then
    holds requests for half a minute and answers 503; InputStream Adaptive just
    waits, and nothing on screen says why. The next server in the same URL
    has it. The first segment alone proves little, it may be all the server
    has. When no server delivers, playback still goes ahead, but the user is told.
    """
    try:
        segments = resolver.media_segments(resolver.fetch_manifest(resolver.lowest_variant_uri(master), headers,
                                                                   timeout=PROBE_SECONDS))
    except Exception as error:  # noqa: BLE001 - this check must not stop playback
        manifest_log.info("fresh video: could not read a variant to check: %s: %s", type(error).__name__, error)
        return None
    sample = segments[:1] + segments[len(segments) // 2:][:1]
    if not sample or all(pool.map(lambda url: _delivers(url, headers), sample)):
        manifest_log.info("fresh video: the server YouTube names delivers")
        return None
    nearest = urllib.parse.urlsplit(sample[0]).netloc
    host = resolver.fallback_host(sample[0])
    if host and all(pool.map(lambda url: _delivers(resolver.through_host(url, host), headers), sample)):
        log("fresh video: {} is not delivering yet, the media comes through {}".format(nearest, host),
            xbmc.LOGWARNING)
        return host
    log("fresh video: neither {} nor {} delivers yet; playback may not start".format(nearest, host or "any other"),
        xbmc.LOGWARNING)
    xbmcgui.Dialog().notification(addon.getAddonInfo("name"), addon.getLocalizedString(30260),
                                  xbmcgui.NOTIFICATION_WARNING, 8000)
    return None


def _read_variants_through(pool, master, host, job, name):
    """(master, file names): every variant now a local copy of its media playlist, going to host."""
    def fetch(uri):
        try:
            return uri, resolver.fetch_manifest(uri, job.headers)
        except Exception as error:  # noqa: BLE001 - one variant less
            log("could not read a variant's playlist, dropping it: {}".format(error), xbmc.LOGWARNING)
            return uri, None

    uris, names = {}, []
    out_dir = paths.manifest_directory()
    for number, (uri, playlist) in enumerate(pool.map(fetch, resolver.variant_uris(master))):
        if playlist:
            names.append("{}.v{}.m3u8".format(name, number))
            with xbmcvfs.File(os.path.join(out_dir, names[-1]), "w") as handle:
                handle.write(resolver.through_host(playlist, host))
            uris[uri] = "{}/{}".format(job.base_url, names[-1])
    # With none readable the manifest stays as it is: the same request may work for InputStream Adaptive.
    return (resolver.repoint_variants(master, uris), names) if uris else (master, names)


def _build_post_live(addon, pool, info, headers, name):
    """An HLS master on loopback for a live stream that has just ended; its URL, or None.

    The fMP4 audio goes with it as everywhere, so without that track, or
    without a variant that answers, there is nothing to play.
    """
    base_url = server_base_url(addon)
    if not server_alive(base_url):
        base_url = _start_standby(base_url)
        if not base_url:
            return None
    audio_url, audio_headers, language = resolver.pick_audio_fmp4(info)
    if not audio_url:
        log("finished live stream without an fMP4 audio track: nothing to play", xbmc.LOGWARNING)
        return None

    def uri(fmt):
        return "{}/{}.{}.m3u8".format(base_url, name, fmt["format_id"])

    video = resolver.post_live_video(info)
    audio_name = name + ".audio.m3u8"
    audio_uri = "{}/{}".format(base_url, audio_name)
    # What the settings keep is all that gets asked for.
    draft, _ = resolver.filter_manifest(
        resolver.build_fragment_master([(fmt, uri(fmt)) for fmt in video], audio_uri, language),
        max_height=addon.getSettingInt("max_height"),
        video_codec=addon.getSettingInt("video_codec"),
        quality=_quality(addon, local_playlists=True))
    kept = resolver.variant_uris(draft)
    video = [fmt for fmt in video if uri(fmt) in kept]
    target = video[0]["target_duration"]

    # Minutes after the end single fragments may not answer yet, on any server,
    # the very first as any other. Every fragment carries its own moov, so then
    # the stream starts a few seconds in: picture and sound at the same fragment.
    tracks = [(audio_url, audio_headers)] + [(fmt["url"], headers) for fmt in video]
    for first in range(FIRST_FRAGMENTS_TRIED):
        inits = _fragment_inits(pool, tracks, first)
        variants = [(fmt, uri(fmt)) for fmt in video if inits[fmt["url"]]]
        if inits[audio_url] and variants:
            break
    else:
        log("finished live stream: its first fragments do not answer yet", xbmc.LOGWARNING)
        return None

    out_dir = paths.manifest_directory()

    def write(file_name, url):
        with xbmcvfs.File(os.path.join(out_dir, file_name), "w") as handle:
            handle.write(resolver.build_fragment_playlist(url, inits[url], info["duration"], target, first))

    write(audio_name, audio_url)
    for fmt, _ in variants:
        write("{}.{}.m3u8".format(name, fmt["format_id"]), fmt["url"])
    master_name = name + ".m3u8"
    with xbmcvfs.File(os.path.join(out_dir, master_name), "w") as handle:
        handle.write(resolver.build_fragment_master(variants, audio_uri, language))
    if _standby is not None:
        _standby["names"].update([master_name, audio_name, *(os.path.basename(u) for _, u in variants)])
    manifest_log.info("finished live stream: %d of %d H.264 variant(s) and fMP4 audio built from fragments, "
                      "%.0f s from fragment %d", len(variants), len(video), info["duration"], first)
    return "{}/{}".format(base_url, master_name)


def _fragment_inits(pool, tracks, number):
    """{url: init range, or None when it does not answer} of fragment number of the tracks [(url, headers)]."""
    def init_of(track):
        url, headers = track
        try:
            # The smallest formats often hang; playback should not wait for them.
            return url, mp4index.init_range(fetch_head(resolver.fragment_url(url, number), headers,
                                                       timeout=PROBE_SECONDS))
        except (urllib.error.URLError, OSError, ValueError, struct.error) as error:
            manifest_log.info("fragment %d of a format does not answer: %s", number, error)
            return url, None

    return dict(pool.map(init_of, tracks))


def _build_audio_playlist(audio, out_dir, name, host=None):
    """Write an fMP4 audio media playlist for this video.

    Returns (file_name, language), or (None, None) when the track cannot be
    indexed, in which case the caller keeps YouTube's own audio. With host the
    audio comes through that server, like the video.
    """
    audio_url, audio_headers, language, head = audio
    try:
        if host:
            audio_url = resolver.through_host(audio_url, host)
            head = fetch_head(audio_url, audio_headers)
        else:
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

    The author's track in the chosen language, then in the fallback, then --
    when the settings allow -- YouTube's automatic captions in the video's own
    language, then the first track the author uploaded (resolver.pick_subtitles).
    """
    if not addon.getSettingBool("subtitles_enabled"):
        subtitles_log.info("switched off in the settings")
        return None
    refused = refused_at() if resolver.is_youtube(info) else None
    if refused:
        log("subtitles: not asked for: YouTube refused this address at {} (429); next try after {}".format(
            _clock(refused), _clock(refused + COOLDOWN_SECONDS)))
        return None
    languages = subtitle_languages(addon)
    automatic = addon.getSettingBool("subtitles_automatic")
    candidates = resolver.pick_subtitles(info, languages, automatic)
    subtitles_log.info("uploaded by the author: %s; automatic captions: %d languages (%s); "
                       "wanted: %s, then the first track; trying: %s",
                       ", ".join(sorted(info.get("subtitles") or {})) or "none",
                       len(info.get("automatic_captions") or {}),
                       "the original if the author added none" if automatic else "switched off",
                       ", ".join(languages) or "-",
                       ", ".join(_label(language, machine) for language, _, _, machine in candidates) or "nothing")
    look = None
    if any(ext == "json3" for _, _, ext, _ in candidates):
        look = (addon.getSettingInt("subtitles_automatic_look"), kodiutil.subtitle_font_size())
    return (pool.submit(_fetch_first, candidates, look), candidates) if candidates else None


def refused_at():
    """When YouTube last answered a subtitle request 429, if that is under COOLDOWN_SECONDS ago."""
    try:
        refused = float(HOME.getProperty(REFUSED_PROPERTY) or 0)
    except ValueError:
        refused = 0
    if refused and time.time() - refused < COOLDOWN_SECONDS:
        return refused
    HOME.clearProperty(REFUSED_PROPERTY)
    return None


def _clock(seconds):
    return time.strftime("%H:%M", time.localtime(seconds))


def _label(language, automatic):
    return language + (" (auto)" if automatic else "")


def _fetch_first(candidates, look=None):
    """(track, problems): track is (language, ext, automatic, bytes, seconds) of
    the first candidate that downloads, or None; problems are (label, error) of
    the ones that did not.

    A 429 ends the attempt: it is YouTube refusing this client, not one track,
    and every other request would get it too. json3 is drawn here, in this
    worker, while the manifest is still being prepared; look is
    (subtitles_automatic_look, Kodi's font size) for it.
    """
    problems = []
    for language, url, ext, automatic in candidates:
        started = time.monotonic()
        try:
            data = resolver.fetch(url)
        except Exception as error:  # noqa: BLE001 - the next candidate may still work
            problems.append((_label(language, automatic), error))
            if isinstance(error, urllib.error.HTTPError) and error.code == 429:
                break
            continue
        took = time.monotonic() - started
        if ext == "json3":
            try:
                data, ext = _draw_captions(data, look or (LOOK_YOUTUBE, youtube_captions.KODI_FONT_SIZE))
            except Exception as error:  # noqa: BLE001 - the next candidate may still work
                problems.append((_label(language, automatic), error))
                continue
        return (language, ext, automatic, data, took), problems
    return None, problems


def _draw_captions(data, look):
    """(bytes, ext) for Kodi from YouTube's json3 captions, in the look the settings ask for.

    As YouTube's player draws them, in ASS; as plain SRT lines when asked, or
    when the drawing fails -- from the same data, so the track is never asked
    for twice (429).
    """
    mode, font_size = look
    started = time.monotonic()
    if mode == LOOK_YOUTUBE:
        try:
            text, ext = youtube_captions.to_ass(data, font_size), "ass"
        except Exception as error:  # noqa: BLE001 - the plain lines may still come out of it
            log("subtitles: YouTube's captions could not be drawn ({}: {}), plain lines instead".format(
                type(error).__name__, error), xbmc.LOGWARNING)
            text, ext = youtube_captions.to_srt(data), "srt"
    else:
        text, ext = youtube_captions.to_srt(data), "srt"
    subtitles_log.info("json3 of %d bytes drawn %s in %.0f ms", len(data),
                       "as on YouTube" if ext == "ass" else "as plain lines", (time.monotonic() - started) * 1000)
    return text.encode("utf-8"), ext


def _finish_subtitles(job, name, youtube):
    """Save the track that arrived; (language, path, automatic), or None.

    Kodi reads the language from the file name: CUtil::GetExternalStreamDetailsFromFilename
    strips the video's base name, splits the rest on " .-" and walks the tokens
    backwards until one converts to an ISO code, so "<video id>.<lang>.srt"
    labels the track -- and "<video id>.<lang>.ass" alike.
    """
    if job is None:
        return None
    future, candidates = job
    track, problems = future.result()
    for language, error in problems:
        log("subtitles: {} failed ({}: {}), skipping".format(
            language, type(error).__name__, error), xbmc.LOGWARNING)
    refused = any(isinstance(error, urllib.error.HTTPError) and error.code == 429 for _, error in problems)
    if refused and youtube:
        HOME.setProperty(REFUSED_PROPERTY, str(time.time()))
        log("subtitles: YouTube answered 429 Too Many Requests: no subtitle requests to it for {} min".format(
            COOLDOWN_SECONDS // 60), xbmc.LOGWARNING)
    if track is None:
        untried = len(candidates) - len(problems)
        if untried:
            log("subtitles: {} more track(s) not tried after the 429".format(untried), xbmc.LOGWARNING)
        return None
    language, ext, automatic, data, took = track
    target = os.path.join(paths.manifest_directory(), "{}.{}.{}".format(name, language, ext))
    with xbmcvfs.File(target, "w") as handle:
        handle.write(data)
    subtitles_log.info("%s: %d bytes in %.0f ms -> %s", _label(language, automatic), len(data), took * 1000,
                       os.path.basename(target))
    return language, target, automatic
