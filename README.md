# plugin.video.ytdlpcast

A Kodi playback add-on that resolves a YouTube video with **yt-dlp** and plays it through
**InputStream Adaptive** — streaming, seekable, with nothing written to disk.

Built as the playback back end for
[a fork of TubeCast](https://github.com/giaur500/script.tubecast), but it works standalone.

## Why

Kodi's YouTube add-ons each carry their own extraction code, and each stops working when
YouTube changes something on their side. yt-dlp exists to solve exactly that problem and
ships fixes continuously. This add-on is the thin layer that lets Kodi's player use it.

It is deliberately small: no browsing, no search, no library. It takes a video id and plays
it.

## How it works

1. yt-dlp resolves the page. **Nothing is downloaded** — extraction only turns a URL into
   stream URLs.
2. The add-on picks the **HLS master manifest** YouTube publishes for VOD. That single URL
   already lists every quality variant and carries the audio track, so there are no separate
   video and audio streams to stitch together.
3. The manifest goes to InputStream Adaptive, which handles bitrate switching and seeking.

Seeking works because the media playlist maps the whole timeline onto individually
addressable segments — a 33-minute video is roughly 333 segments of about 6 seconds. Jumping
to any position costs one segment fetch, not a download of everything before it.

This matters more than it sounds: modern YouTube videos usually offer **no muxed format at
all** (`yt-dlp -f b` fails outright on many of them), so the naive "resolve to one URL and
play it" approach does not work. The HLS manifest is what makes a single-URL handoff possible.

## Requirements

| add-on | why |
|---|---|
| `script.module.yt-dlp` | the extractor |
| `inputstream.adaptive` | the demuxer; built into CoreELEC and LibreELEC |
| `script.module.certifi` | yt-dlp's only external dependency, in the official Kodi repository |

## Installation

Install from zip, dependencies first: `script.module.certifi`, then `script.module.yt-dlp`,
then this add-on.

`script.module.yt-dlp` is published by [lekma](https://github.com/lekma/script.module.yt-dlp)
and available through [lekma's repository](https://github.com/lekma/repository.lekma).

### Keeping yt-dlp current

This is not a nicety. A stale yt-dlp is the one thing that reliably breaks playback, because
YouTube keeps changing what extraction has to cope with. Install `script.module.yt-dlp` from
a repository and leave add-on auto-updates enabled. If you install it from a zip instead,
Kodi will never update it and refreshing it becomes your job.

## Usage

As TubeCast's playback back end: install the fork linked above and pick **yt-dlp** under its
*Playback adddon* setting.

Standalone, from a keymap, a favourite or the built-in function:

```
RunPlugin(plugin://plugin.video.ytdlpcast/?video_id=7NU_Kr0CXTs&seek=0)
```

Parameters:

| parameter | meaning |
|---|---|
| `video_id` | YouTube video id |
| `url` | full URL, used instead of `video_id`; anything yt-dlp supports |
| `seek` | start position in seconds |
| `start_offset` | accepted as an alias of `seek`, for Tubed-shaped URLs |

## Settings

The add-on never asks yt-dlp to choose a format — InputStream Adaptive picks the variant. So
the picture and sound settings work by **rewriting the HLS master manifest** before it is
handed over. The rewritten copy is served to InputStream Adaptive from a tiny HTTP server on
`127.0.0.1` that the add-on's background service runs for as long as Kodi does. A local file
path is not an option: InputStream Adaptive does not accept one, Kodi falls back to its own
demuxer, and that plays the first variant only, without audio groups or seeking (that was
version 1.1.0's bug).

Whenever the rewrite cannot be delivered — switched off, server not running, manifest fetch
failed, nothing changed — the add-on hands over YouTube's original URL, exactly as 1.0 did.

### Picture & sound

* **Rewrite the manifest** — on by default; the master switch for everything below. Off means
  YouTube's manifest untouched, no local server involved.
* **Maximum resolution** — *No limit* (default), 2160p, 1440p, 1080p, 720p, 480p. Variants
  above the limit are removed; within it InputStream Adaptive still adapts.
* **Video codec** — *Auto* (default), *H.264 only*, *VP9 only*. YouTube offers both codecs up
  to 1080p and VP9 alone above it, so *H.264 only* caps playback at 1080p, and *VP9 only* needs
  hardware VP9 decoding. A choice that would leave nothing to play is ignored rather than
  obeyed.
* **Quality selection** — *Adaptive* (default) keeps every allowed variant and lets
  InputStream Adaptive switch by measured bandwidth. *Best available* keeps only the tallest,
  highest-bitrate variant that passed the codec and resolution settings, so playback starts
  at that quality instead of ramping up from 240p — and buffers rather than adapts if the
  connection cannot carry it. Together with the codec and resolution settings this reads as:
  "these are the codecs my device decodes, these resolutions are allowed, now give me the
  best of that".
* **Audio track** — *fMP4 (fixed)* (default) or *As published*. InputStream Adaptive on
  Kodi 21 starts YouTube's packed-ADTS HLS audio **silent until the first seek** — the audio
  demuxer only gets its PTS offset on a segment change, so the first packets are never
  selected until any seek triggers one. The fix replaces the audio with YouTube's DASH AAC
  track (itag 140) rebuilt as an fMP4 byte-range playlist, which the same fragmented reader as
  the video plays from the first frame. Confirmed working on CoreELEC Omega. *As published* keeps YouTube's own audio for
  diagnosis. This also collapses the old duplicate-audio-group and dubbing issues: the fMP4
  track is a single AAC-LC rendition, chosen in the video's original language.

### Subtitles

* **Download subtitles** — on by default. Downloads every subtitle track the video's **author
  uploaded**, as `<video id>.<lang>.srt` files in Kodi's temp directory, handed to the player
  with `setSubtitles()`.

Kodi reads the language from the file name and ranks these external tracks against its own
**Settings → Player → Language → Preferred subtitle language**, so it selects the best match
by itself — `VideoPlayer.cpp` scores "an external sub whose language matches the preferred
subtitle's language" explicitly, and honours the `original` and `forced_only` modes too.
Nothing is searched for on disk: the files are attached to the playing item.

YouTube's automatic captions are deliberately not used. They are machine output, and the
machine-*translated* ones cannot be fetched at all: that endpoint requires browser TLS
impersonation (`curl_cffi`), answering HTTP 429 to everything else — yt-dlp itself included.
There is no Kodi module providing it, and it ships as per-architecture binaries, so no add-on
can. Author-uploaded tracks have no such limit: five languages download in half a second.

### Playback

* **Fall back to a progressive stream** — if no HLS manifest is offered, play the best single
  file carrying both video and audio. Rarely helps on YouTube, since such formats mostly no
  longer exist; useful for other sites.
* **Send the legacy manifest_type property** — off by default. InputStream Adaptive 21
  detects HLS from the mime type. Enable only if an older build refuses the manifest.
* **Local manifest server port** — `50153` by default (`plugin.video.youtube` uses 50152).
  The server listens on loopback only and serves nothing but `.m3u8` files from one
  directory; it restarts by itself when the port is changed. If the port cannot be bound, the
  service logs why and playback continues with manifests as published.

## Limitations

* **Available formats come from yt-dlp, not from this add-on.** Which resolutions and codecs
  a given video offers is decided by yt-dlp and by what YouTube publishes for it. The settings
  above can only narrow that list, never extend it.
* **Only the manifest is served locally.** The rewritten playlist sits in Kodi's temp
  directory and is served from `127.0.0.1`; the segment URLs inside it are YouTube's absolute
  ones, so no media is proxied. Look for `manifest server listening on` and `playing a
  filtered manifest from` in Kodi's log to confirm the path is in use.
* **VOD only in practice.** Live streams work, but seeking backwards is limited to whatever
  DVR window the broadcaster provides — a server-side limit, not one imposed here.
* **JavaScript runtime.** yt-dlp warns that YouTube extraction without a JS runtime (deno,
  bun, node, quickjs) is deprecated. As measured, the HLS path currently resolves identically
  with and without one; if that changes, a runtime will have to be installed alongside.

## Development

`resources/lib/resolver.py` holds the resolution logic and imports no `xbmc` module on
purpose, so it can be exercised on a desktop without Kodi.

## License

MIT.
