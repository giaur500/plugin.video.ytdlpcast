# -*- coding: utf-8 -*-
"""Stream resolution.

Deliberately free of any xbmc import so the logic can be exercised on a normal
desktop with scripts/test-resolver.py, without a running Kodi.
"""

import re
import time
import urllib.error
import urllib.parse
import urllib.request

from . import diag

manifest_log = diag.logger("manifest")

HLS_MIME = "application/vnd.apple.mpegurl"
DASH_MIME = "application/dash+xml"

# Headers yt-dlp attaches for its own use; forwarding them to InputStream
# Adaptive is at best useless and at worst breaks the manifest request.
_SKIP_HEADERS = ("youtubei",)

# A playlist link is queued, not played in one go; this many entries at most.
PLAYLIST_LIMIT = 200

# Cookie attributes in yt-dlp's per-format "cookies" string -- not cookies.
_COOKIE_ATTRIBUTES = {"domain", "path", "expires", "max-age", "secure", "httponly", "samesite"}


def short_error(error):
    """yt-dlp's message in one line: what to show a user ("…requires login")."""
    text = (str(error).strip().splitlines() or [type(error).__name__])[0]
    return re.sub(r"^ERROR:\s*", "", text)[:200]


def watch_url(video_id):
    return "https://www.youtube.com/watch?v={}".format(video_id)


def is_youtube(info):
    """Everything YouTube-specific (manifest rewriting, audio swap) keys off this."""
    return (info.get("extractor_key") or info.get("ie_key") or "").lower().startswith("youtube")


def is_playlist(info):
    return info.get("_type") in ("playlist", "multi_video")


def playlist_entries(info):
    """[(url, title, thumbnail)] of a flat playlist, in order, without the unplayable."""
    entries = []
    for entry in info.get("entries") or ():
        if not entry or not (entry.get("url") or entry.get("webpage_url")):
            continue
        thumbnails = entry.get("thumbnails") or ()
        thumbnail = entry.get("thumbnail") or (thumbnails[-1].get("url") if thumbnails else None)
        entries.append((entry.get("webpage_url") or entry["url"], entry.get("title") or entry.get("id") or "",
                        thumbnail))
    return entries


def extract(url, logger=None, verbose=False, flat_playlists=False):
    """yt-dlp's info dict for url.

    logger, when given, receives yt-dlp's messages (debug/warning/error, as
    yt-dlp's logger interface defines them); without one it stays silent.
    verbose turns on yt-dlp's own verbose mode: versions, clients, requests.
    flat_playlists lists a playlist's entries without resolving each of them
    (they are resolved one by one as Kodi reaches them); a single video is
    extracted in full either way.
    """
    # Imported here, not at module level: the plugin first puts the right yt-dlp
    # on sys.path (ytdlp_loader.activate), and only then may it be imported.
    from yt_dlp import YoutubeDL

    options = {
        "quiet": True,
        "no_warnings": logger is None,
        "noplaylist": True,
        "skip_download": True,
    }
    if logger is not None:
        options["logger"] = logger
    if verbose:
        options["verbose"] = True
    if flat_playlists:
        options["extract_flat"] = "in_playlist"
        options["playlistend"] = PLAYLIST_LIMIT
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
            return fmt["manifest_url"], headers_for(info, fmt)
    return None, None


def pick_dash(info):
    """(manifest_url, headers) of a DASH manifest, or (None, None) -- for sites without HLS."""
    for fmt in info.get("formats") or ():
        if fmt.get("protocol") == "http_dash_segments" and fmt.get("manifest_url"):
            return fmt["manifest_url"], headers_for(info, fmt)
    return None, None


# Containers Kodi's own player decodes without surprises, best first.
_CONTAINERS = ("mp4", "m4v", "mov", "webm", "mkv")


def pick_progressive(info):
    """Best single file carrying both tracks, for Kodi's own player.

    Most YouTube videos no longer offer one, but other sites often offer
    nothing else -- and often say little about it: Facebook's "sd" and "hd" are
    MP4 files with no codec or height in the metadata, archive.org's likewise.
    A format is skipped only when it is known to lack a track; then known
    codecs beat unknown ones, height beats nothing, a container Kodi plays well
    beats the rest, and where nothing tells them apart (Facebook) the format
    yt-dlp itself chose wins.
    """
    chosen = info.get("format_id")

    def rank(fmt):
        known = fmt.get("vcodec") not in (None, "none") and fmt.get("acodec") not in (None, "none")
        container = (fmt.get("ext") or "").lower()
        return (known, fmt.get("height") or 0, container in _CONTAINERS, fmt.get("format_id") == chosen,
                fmt.get("tbr") or 0)

    candidates = [
        fmt for fmt in info.get("formats") or ()
        if fmt.get("protocol") in ("https", "http") and fmt.get("url")
        and fmt.get("vcodec") != "none" and fmt.get("acodec") != "none"
    ]
    if not candidates and info.get("url") and info.get("protocol") in ("https", "http"):
        candidates = [info]  # a site that returns one file and no format list
    if not candidates:
        return None, None
    best = max(candidates, key=rank)
    return best["url"], headers_for(info, best)


def pick_audio_only(info):
    """Best audio file, for sites that have no video at all (podcasts, music)."""
    if any(fmt.get("vcodec") not in ("none",) for fmt in info.get("formats") or ()):
        return None, None
    candidates = [fmt for fmt in info.get("formats") or ()
                  if fmt.get("protocol") in ("https", "http") and fmt.get("url") and fmt.get("acodec") != "none"]
    if not candidates:
        return None, None
    best = max(candidates, key=lambda fmt: (fmt.get("abr") or fmt.get("tbr") or 0))
    return best["url"], headers_for(info, best)


def _cookie_header(cookies):
    """yt-dlp's "name=value; Domain=...; Path=...; name2=..." as a Cookie header value."""
    pairs = []
    for part in (cookies or "").split(";"):
        name, sep, value = part.strip().partition("=")
        if sep and name and name.lower() not in _COOKIE_ATTRIBUTES:
            pairs.append("{}={}".format(name, value))
    return "; ".join(pairs)


def headers_for(info, fmt):
    """The headers a player needs to fetch this format, as yt-dlp would send them.

    Cookies only off YouTube: there they are yt-dlp's own business and only get
    in the way, while elsewhere (archive.org, CDNs that set a session cookie on
    the page) the media request may need them.
    """
    headers = dict(fmt.get("http_headers") or info.get("http_headers") or {})
    if is_youtube(info):
        headers = {key: value for key, value in headers.items() if key.lower() != "cookie"}
    else:
        cookie = _cookie_header(fmt.get("cookies") or info.get("cookies"))
        if cookie:
            headers["Cookie"] = cookie
    return headers


def _usable_headers(headers):
    """headers without empty values and without yt-dlp's own (_SKIP_HEADERS)."""
    return {key: value for key, value in (headers or {}).items()
            if value and not key.lower().startswith(_SKIP_HEADERS)}


def kodi_url(url, headers):
    """A URL with headers in Kodi's own "url|Name=value&..." form, for its player."""
    usable = _usable_headers(headers)
    return url + ("|" + urllib.parse.urlencode(usable) if usable else "")


def encode_headers(headers):
    """Headers in the "a=b&c=d" shape InputStream Adaptive expects."""
    return urllib.parse.urlencode(_usable_headers(headers))


# ---------------------------------------------------------------------------
# Master manifest filtering
#
# InputStream Adaptive picks the variant itself, so the only way to influence
# resolution, codec or which audio tracks exist is to rewrite the master
# playlist before handing it over. Everything below works on the raw lines and
# makes targeted substitutions, so whatever is not touched survives byte for
# byte.
# ---------------------------------------------------------------------------

# Values of the settings, kept in step with resources/settings.xml.
VIDEO_AUTO, VIDEO_H264, VIDEO_VP9 = 0, 1, 2
AUDIO_AS_PUBLISHED, AUDIO_AAC_LC, AUDIO_HE_AAC = 0, 1, 2
QUALITY_ADAPTIVE, QUALITY_BEST = 0, 1

_VIDEO_PREFIX = {VIDEO_H264: "avc1", VIDEO_VP9: "vp09"}
_AUDIO_TAG = {AUDIO_AAC_LC: "mp4a.40.2", AUDIO_HE_AAC: "mp4a.40.5"}

_ATTRIBUTE = re.compile(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)')


def fetch(url, headers=None, timeout=20, method="GET"):
    """Bytes at url, with the request in the HTTP diagnostics either way."""
    request = urllib.request.Request(url, headers=dict(headers or {}), method=method)
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = response.read()
            diag.log_request(method, url, started, response.status, len(data))
            return data
    except Exception as error:
        diag.log_request(method, url, started, error=error)
        raise


def fetch_manifest(url, headers, timeout=20):
    return fetch(url, headers, timeout).decode("utf-8", "replace")


def describe_formats(info):
    """yt-dlp's result in one line: what YouTube offered for this video."""
    formats = info.get("formats") or ()
    video = [f for f in formats if (f.get("vcodec") or "none") != "none"]
    audio = [f for f in formats if (f.get("vcodec") or "none") == "none" and (f.get("acodec") or "none") != "none"]
    heights = sorted({f.get("height") for f in video if f.get("height")})
    codecs = sorted({(f.get("vcodec") or "").split(".")[0] for f in video})
    hls = any(f.get("protocol") == "m3u8_native" and f.get("manifest_url") for f in formats)
    audio_ids = sorted({"{}{}".format(f.get("format_id"), "/" + f["language"] if f.get("language") else "")
                        for f in audio if (f.get("format_id") or "").split("-")[0] in ("139", "140", "251", "250")})
    return ("{} formats, HLS manifest {}, video {}–{}p in {}, audio {}, subtitles {}, automatic captions {}, "
            "duration {}s, live {}").format(
        len(formats), "yes" if hls else "NO", heights[0] if heights else "?", heights[-1] if heights else "?",
        "/".join(c for c in codecs if c) or "?", ", ".join(audio_ids) or "?",
        ",".join(sorted(info.get("subtitles") or {})) or "none", len(info.get("automatic_captions") or {}),
        info.get("duration"), info.get("is_live"))


def describe_variants(text):
    """A master playlist in one line: variants by height and codec, audio groups."""
    entries = _parse(text)
    variants = [e for e in entries if e["kind"] == "variant"]
    groups = sorted({e["attrs"].get("GROUP-ID") for e in entries if _is_audio(e)})
    by_codec = {}
    for entry in variants:
        video, _ = _codecs(entry["attrs"])
        by_codec.setdefault(video.split(".")[0] or "?", []).append(_height(entry["attrs"]))
    detail = "; ".join("{} {}".format(codec, ",".join("{}p".format(h) for h in sorted(heights)))
                       for codec, heights in sorted(by_codec.items()))
    return "{} variants ({}), audio groups: {}".format(len(variants), detail or "-", ", ".join(groups) or "none")


def _attributes(line):
    _, _, tail = line.partition(":")
    return {key: value.strip('"') for key, value in _ATTRIBUTE.findall(tail)}


def _parse(text):
    """Split a master playlist into entries that can be dropped or rewritten.

    A variant is the #EXT-X-STREAM-INF tag together with the URI line that
    follows it; the two travel as one entry so they are never separated.
    """
    entries = []
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.startswith("#EXT-X-STREAM-INF"):
            uri = lines[index + 1] if index + 1 < len(lines) else ""
            entries.append({"kind": "variant", "attrs": _attributes(line), "lines": [line, uri]})
            index += 2
            continue
        if line.startswith("#EXT-X-MEDIA:"):
            entries.append({"kind": "media", "attrs": _attributes(line), "lines": [line]})
        else:
            entries.append({"kind": "other", "attrs": {}, "lines": [line]})
        index += 1
    return entries


def _height(attrs):
    resolution = attrs.get("RESOLUTION", "")
    _, _, height = resolution.partition("x")
    return int(height) if height.isdigit() else 0


def _codecs(attrs):
    """(video, audio) codec strings from CODECS, either possibly empty."""
    parts = [part.strip() for part in attrs.get("CODECS", "").split(",") if part.strip()]
    video = next((part for part in parts if not part.startswith("mp4a")), "")
    audio = next((part for part in parts if part.startswith("mp4a")), "")
    return video, audio


def _is_audio(entry):
    return entry["kind"] == "media" and entry["attrs"].get("TYPE") == "AUDIO"


def _collapse_audio_groups(entries, wanted_tag):
    """Keep one audio group and point every variant at it.

    YouTube ships the same audio twice: an HE-AAC group for the 144p/240p
    variants and an AAC-LC group for everything above. InputStream Adaptive
    exposes both as selectable "default" tracks, and playback can start silent
    until the user switches between them. One group removes the ambiguity.
    """
    codec_by_group = {}
    for entry in entries:
        if entry["kind"] == "variant" and entry["attrs"].get("AUDIO"):
            _, audio = _codecs(entry["attrs"])
            codec_by_group.setdefault(entry["attrs"]["AUDIO"], audio)

    targets = [group for group, tag in codec_by_group.items() if tag == wanted_tag]
    if not targets:
        return entries, False
    target = targets[0]
    doomed = {group for group, tag in codec_by_group.items() if group != target}
    if not doomed:
        return entries, False

    kept = []
    for entry in entries:
        if _is_audio(entry) and entry["attrs"].get("GROUP-ID") in doomed:
            continue
        if entry["kind"] == "variant" and entry["attrs"].get("AUDIO") in doomed:
            old_group = entry["attrs"]["AUDIO"]
            _, old_tag = _codecs(entry["attrs"])
            line = entry["lines"][0]
            line = line.replace('AUDIO="{}"'.format(old_group), 'AUDIO="{}"'.format(target))
            if old_tag:
                line = line.replace(old_tag, wanted_tag)
            entry = dict(entry, lines=[line, entry["lines"][1]], attrs=_attributes(line))
        kept.append(entry)
    return kept, True


def _filter_variants(entries, max_height, video_codec):
    prefix = _VIDEO_PREFIX.get(video_codec)

    def acceptable(entry):
        if max_height and _height(entry["attrs"]) > max_height:
            return False
        if prefix:
            video, _ = _codecs(entry["attrs"])
            if not video.startswith(prefix):
                return False
        return True

    variants = [entry for entry in entries if entry["kind"] == "variant"]
    survivors = [entry for entry in variants if acceptable(entry)]
    # A filter that would leave nothing to play is ignored, not obeyed.
    if not survivors or len(survivors) == len(variants):
        return entries, False
    return [entry for entry in entries if entry["kind"] != "variant" or acceptable(entry)], True


def _bandwidth(attrs):
    value = attrs.get("BANDWIDTH", "")
    return int(value) if value.isdigit() else 0


def _keep_best(entries):
    """Leave a single variant: the tallest, and among those the highest bitrate.

    With one variant InputStream Adaptive has nothing to adapt between, so it
    plays that quality from the first second instead of ramping up from 240p.
    The cost is that bandwidth measurement no longer matters -- a link that
    cannot carry the top variant will buffer, which is the user's choice here.
    """
    variants = [entry for entry in entries if entry["kind"] == "variant"]
    if len(variants) < 2:
        return entries, False
    best = max(variants, key=lambda entry: (_height(entry["attrs"]), _bandwidth(entry["attrs"])))
    return [entry for entry in entries if entry["kind"] != "variant" or entry is best], True


def _drop_auto_dubbed(entries):
    """Remove YouTube's machine-dubbed tracks where an undubbed one remains.

    Never empty a group: a variant that points at a group with no renditions
    left would play without sound.
    """
    by_group = {}
    for entry in entries:
        if _is_audio(entry):
            by_group.setdefault(entry["attrs"].get("GROUP-ID"), []).append(entry)

    doomed = set()
    for renditions in by_group.values():
        dubbed = [r for r in renditions if "dubbed-auto" in r["attrs"].get("NAME", "")]
        if dubbed and len(dubbed) < len(renditions):
            doomed.update(id(r) for r in dubbed)
    if not doomed:
        return entries, False
    return [entry for entry in entries if id(entry) not in doomed], True


def _ensure_default(entries):
    """Give every audio group a DEFAULT track when the manifest provides none.

    Without one Kodi has nothing to auto-select. The original-language track
    wins when it can be told apart; otherwise the first one does.
    """
    by_group = {}
    for entry in entries:
        if _is_audio(entry):
            by_group.setdefault(entry["attrs"].get("GROUP-ID"), []).append(entry)

    changed = False
    for renditions in by_group.values():
        if any(r["attrs"].get("DEFAULT") == "YES" for r in renditions):
            continue
        chosen = next((r for r in renditions if "original" in r["attrs"].get("NAME", "")), renditions[0])
        line = chosen["lines"][0]
        if "DEFAULT=NO" in line:
            line = line.replace("DEFAULT=NO", "DEFAULT=YES")
        else:
            line = line.rstrip() + ",DEFAULT=YES"
        chosen["lines"] = [line]
        chosen["attrs"] = _attributes(line)
        changed = True
    return entries, changed


def filter_manifest(text, max_height=0, video_codec=VIDEO_AUTO,
                    audio_codec=AUDIO_AS_PUBLISHED, drop_auto_dubbed=False,
                    quality=QUALITY_ADAPTIVE):
    """Apply the playback settings to a master playlist.

    Returns (text, changed). When nothing had to change the input text comes
    back untouched and changed is False, so the caller can keep handing
    InputStream Adaptive the original URL.
    """
    entries = _parse(text)
    changed = False
    explain = diag.enabled("manifest")

    def step(name, result):
        nonlocal entries, changed
        before = len([e for e in entries if e["kind"] == "variant"]), len([e for e in entries if _is_audio(e)])
        entries, did = result
        changed |= did
        if explain:
            after = len([e for e in entries if e["kind"] == "variant"]), len([e for e in entries if _is_audio(e)])
            manifest_log.info("filter %s: %s (variants %d -> %d, audio renditions %d -> %d)", name,
                              "changed" if did else "nothing to do", before[0], after[0], before[1], after[1])

    # Order matters: collapsing groups first means the AUDIO= rewrite still
    # sees every variant, before any of them is filtered away.
    if audio_codec in _AUDIO_TAG:
        step("one audio group ({})".format(_AUDIO_TAG[audio_codec]),
             _collapse_audio_groups(entries, _AUDIO_TAG[audio_codec]))
    step("max height {} / codec {}".format(max_height or "any", _VIDEO_PREFIX.get(video_codec, "any")),
         _filter_variants(entries, max_height, video_codec))
    # After the codec and resolution filters, so "best" means best of what the
    # device was declared able to play.
    if quality == QUALITY_BEST:
        step("best variant only", _keep_best(entries))
    if drop_auto_dubbed:
        step("drop auto-dubbed audio", _drop_auto_dubbed(entries))
    step("default audio track", _ensure_default(entries))

    if not changed:
        return text, False
    rebuilt = "\n".join(line for entry in entries for line in entry["lines"])
    if text.endswith("\n"):
        rebuilt += "\n"
    return rebuilt, True


# ---------------------------------------------------------------------------
# Subtitles
# ---------------------------------------------------------------------------

# Kodi reads both; srt without any surprises.
_SUBTITLE_EXTENSIONS = ("srt", "vtt")
# YouTube still uses a few withdrawn ISO 639 codes; Kodi and the settings use the current ones.
_LANGUAGE_ALIASES = {"iw": "he", "in": "id", "ji": "yi", "jw": "jv"}


def _primary_language(code):
    """'de-DE' -> 'de', 'es-419' -> 'es', 'iw' -> 'he', None -> ''.

    Kodi resolves the language from a subtitle's file name and InputStream
    Adaptive from LANGUAGE; both take a plain ISO 639-1 code reliably, a
    regional suffix not always.
    """
    primary = (code or "").split("-")[0].lower()
    return _LANGUAGE_ALIASES.get(primary, primary)


def _subtitle_file(tracks):
    """(url, ext) of the first format Kodi reads well, or None."""
    for extension in _SUBTITLE_EXTENSIONS:
        track = next((t for t in tracks if t.get("ext") == extension and t.get("url")), None)
        if track:
            return track["url"], extension
    return None


def pick_subtitles(info, languages):
    """[(language, url, ext)] to try in order until one downloads; each track once.

    For every wanted language (ISO 639-1 codes, most wanted first) the track
    the uploader provided in it -- the plain code before a regional one, "en"
    before "en-GB" -- then the first track the video has, in yt-dlp's order,
    whatever its language.

    Only info["subtitles"] is considered -- the author's own tracks. YouTube's
    automatic_captions are machine output, and the translated ones among them
    are unusable anyway: that endpoint demands browser TLS impersonation and
    answers HTTP 429 to everything else, including yt-dlp itself.
    """
    usable = []  # (code, language, url, ext), in yt-dlp's order
    for code, tracks in (info.get("subtitles") or {}).items():
        found = _subtitle_file(tracks or ())
        if found:
            usable.append((code, _primary_language(code)) + found)
    ordered = []
    for wanted in languages:
        matches = [track for track in usable if track[1] == _primary_language(wanted)]
        ordered.extend(sorted(matches, key=lambda track: "-" in track[0])[:1])
    ordered.extend(usable[:1])
    candidates, seen = [], set()
    for code, language, url, ext in ordered:
        if code not in seen:
            seen.add(code)
            candidates.append((language, url, ext))
    return candidates


# ---------------------------------------------------------------------------
# fMP4 audio, to avoid InputStream Adaptive's packed-ADTS start-silence
#
# YouTube's HLS audio is packed ADTS, read by a demuxer that stays silent until
# the first seek (the PTS offset is only applied on a segment change). Its DASH
# audio (itag 140) is the same AAC in fMP4, which the fragmented reader -- the
# one already handling the video -- plays from the first frame. We keep the
# video HLS rendition untouched and swap only the audio.
# ---------------------------------------------------------------------------

AUDIO_ITAG_PREFIX = "140"  # m4a AAC-LC; "-0"/"-1" suffixes are per-language


def pick_audio_fmp4(info):
    """(url, headers, language) of the best fMP4 AAC audio, or (None, None, None).

    Prefers the track in the video's original language; falls back to the
    first m4a track.
    """
    candidates = [
        fmt for fmt in info.get("formats") or ()
        if fmt.get("container") == "m4a_dash"
        and (fmt.get("format_id") or "").split("-")[0] == AUDIO_ITAG_PREFIX
        and fmt.get("url")
    ]
    if not candidates:
        return None, None, None
    chosen = next((c for c in candidates if _is_original(c)), candidates[0])
    return chosen["url"], chosen.get("http_headers") or {}, _primary_language(chosen.get("language")) or None


def _is_original(fmt):
    """yt-dlp ranks the original-language track above the others and names it in the format note."""
    preference = fmt.get("language_preference") or 0
    note = "{} {}".format(fmt.get("format_note") or "", fmt.get("format_id") or "").lower()
    return preference > 0 or (preference == 0 and "original" in note)


def build_fmp4_audio_playlist(audio_url, init_range, segments):
    """An fMP4 byte-range HLS media playlist for a single remote audio file.

    init_range is (0, end); segments is [(start, size, duration), ...] from
    mp4index.parse_sidx. Every segment and the init map point at the one
    absolute audio_url with its own BYTERANGE -- InputStream Adaptive issues
    range requests against it, with the stream headers the plugin sets.
    """
    target = max((int(duration) + 1 for _, _, duration in segments), default=10)
    init_length = init_range[1] - init_range[0]
    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:7",
        "#EXT-X-PLAYLIST-TYPE:VOD",
        "#EXT-X-TARGETDURATION:{}".format(target),
        "#EXT-X-MEDIA-SEQUENCE:0",
        '#EXT-X-MAP:URI="{}",BYTERANGE="{}@{}"'.format(audio_url, init_length, init_range[0]),
    ]
    for start, size, duration in segments:
        lines.append("#EXTINF:{:.3f},".format(duration))
        lines.append("#EXT-X-BYTERANGE:{}@{}".format(size, start))
        lines.append(audio_url)
    lines.append("#EXT-X-ENDLIST")
    return "\n".join(lines) + "\n"


AUDIO_GROUP_ID = "ytdlpcast-audio"


def swap_audio_to_fmp4(master, audio_playlist_uri, language=None):
    """Replace the packed-ADTS audio group with our single fMP4 rendition.

    Drops every existing audio EXT-X-MEDIA, adds one pointing at the local
    playlist, and repoints every variant's AUDIO attribute at it.
    """
    entries = _parse(master)
    kept = []
    for entry in entries:
        if _is_audio(entry):
            continue  # our single rendition replaces all of these
        if entry["kind"] == "variant" and entry["attrs"].get("AUDIO"):
            old = entry["attrs"]["AUDIO"]
            line = entry["lines"][0].replace(
                'AUDIO="{}"'.format(old), 'AUDIO="{}"'.format(AUDIO_GROUP_ID))
            entry = dict(entry, lines=[line, entry["lines"][1]], attrs=_attributes(line))
        kept.append(entry)

    media = ('#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="{}",NAME="Audio",DEFAULT=YES,'
             'AUTOSELECT=YES{},URI="{}"').format(
                 AUDIO_GROUP_ID,
                 ',LANGUAGE="{}"'.format(language) if language else "",
                 audio_playlist_uri)

    # Put the media declaration just before the first variant.
    out = []
    inserted = False
    for entry in kept:
        if not inserted and entry["kind"] == "variant":
            out.append(media)
            inserted = True
        out.extend(entry["lines"])
    if not inserted:
        out.append(media)
    text = "\n".join(out)
    if master.endswith("\n"):
        text += "\n"
    return text
