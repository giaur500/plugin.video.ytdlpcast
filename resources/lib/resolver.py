# -*- coding: utf-8 -*-
"""Stream resolution.

Deliberately free of any xbmc import so the logic can be exercised on a normal
desktop with scripts/test-resolver.py, without a running Kodi.
"""

import re
import urllib.error
import urllib.parse
import urllib.request

from yt_dlp import YoutubeDL

from . import mp4index

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


def fetch_manifest(url, headers, timeout=20):
    request = urllib.request.Request(url, headers=dict(headers or {}))
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", "replace")


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


def _iso639_1(language):
    """'en-US' -> 'en'. InputStream Adaptive only understands the two-letter form."""
    return language.split("-")[0].lower() if language else None


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


def _original_language(entries):
    for entry in entries:
        if _is_audio(entry) and "original" in entry["attrs"].get("NAME", ""):
            return _iso639_1(entry["attrs"].get("LANGUAGE"))
    return None


def filter_manifest(text, max_height=0, video_codec=VIDEO_AUTO,
                    audio_codec=AUDIO_AS_PUBLISHED, drop_auto_dubbed=False,
                    quality=QUALITY_ADAPTIVE):
    """Apply the playback settings to a master playlist.

    Returns (text, changed, original_language). When nothing had to change the
    input text comes back untouched and changed is False, so the caller can
    keep handing InputStream Adaptive the original URL.
    """
    entries = _parse(text)
    changed = False

    # Order matters: collapsing groups first means the AUDIO= rewrite still
    # sees every variant, before any of them is filtered away.
    if audio_codec in _AUDIO_TAG:
        entries, did = _collapse_audio_groups(entries, _AUDIO_TAG[audio_codec])
        changed |= did
    entries, did = _filter_variants(entries, max_height, video_codec)
    changed |= did
    # After the codec and resolution filters, so "best" means best of what the
    # device was declared able to play.
    if quality == QUALITY_BEST:
        entries, did = _keep_best(entries)
        changed |= did
    if drop_auto_dubbed:
        entries, did = _drop_auto_dubbed(entries)
        changed |= did
    entries, did = _ensure_default(entries)
    changed |= did

    if not changed:
        return text, False, _original_language(entries)
    rebuilt = "\n".join(line for entry in entries for line in entry["lines"])
    if text.endswith("\n"):
        rebuilt += "\n"
    return rebuilt, True, _original_language(entries)


# ---------------------------------------------------------------------------
# Subtitles
# ---------------------------------------------------------------------------

# Kodi reads both; srt without any surprises.
_SUBTITLE_EXTENSIONS = ("srt", "vtt")


def fetch_subtitle(url, headers=None, timeout=20):
    """Bytes of one subtitle file."""
    request = urllib.request.Request(url, headers=dict(headers or {}))
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def _primary_language(code):
    """'de-DE' -> 'de', 'es-419' -> 'es'.

    Kodi resolves the language from the file name, and a plain ISO 639-1 code
    converts reliably; a regional suffix would be the last token and might not.
    """
    return code.split("-")[0].lower()


def pick_subtitles(info):
    """[(language, url)] for every subtitle the uploader provided.

    Only info["subtitles"] is considered -- the author's own tracks. YouTube's
    automatic_captions are machine output, and the translated ones among them
    are unusable anyway: that endpoint demands browser TLS impersonation and
    answers HTTP 429 to everything else, including yt-dlp itself.

    One entry per language; a regional variant does not get a second slot.
    """
    uploaded = info.get("subtitles") or {}
    chosen = []
    seen = set()
    for code in sorted(uploaded):
        language = _primary_language(code)
        if language in seen:
            continue
        for extension in _SUBTITLE_EXTENSIONS:
            track = next((t for t in uploaded[code]
                          if t.get("ext") == extension and t.get("url")), None)
            if track:
                chosen.append((language, track["url"]))
                seen.add(language)
                break
    return chosen


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


def pick_audio_fmp4(info, languages=None):
    """(url, headers, language) of the best fMP4 AAC audio, or (None, None, None).

    Prefers a track whose language is the video's original; falls back to the
    first m4a track. languages is an ordered preference list (ISO codes).
    """
    candidates = [
        fmt for fmt in info.get("formats") or ()
        if fmt.get("container") == "m4a_dash"
        and (fmt.get("format_id") or "").split("-")[0] == AUDIO_ITAG_PREFIX
        and fmt.get("url")
    ]
    if not candidates:
        return None, None, None

    def language_of(fmt):
        return (fmt.get("language") or "").split("-")[0].lower() or None

    original = next((c for c in candidates if c.get("language_preference", 0) >= 0
                     and _is_original(c)), None)
    chosen = None
    for wanted in languages or ():
        chosen = next((c for c in candidates if language_of(c) == wanted), None)
        if chosen:
            break
    chosen = chosen or original or candidates[0]
    return chosen["url"], chosen.get("http_headers") or {}, language_of(chosen)


def _is_original(fmt):
    # yt-dlp marks the original-language track in the format note or name.
    note = "{} {}".format(fmt.get("format_note") or "", fmt.get("format_id") or "").lower()
    return "original" in note or fmt.get("language_preference", 0) > 0


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
