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

* **Fall back to a progressive stream** — if no HLS manifest is offered, play the best single
  file carrying both video and audio. Rarely helps on YouTube, since such formats mostly no
  longer exist; useful for other sites.
* **Send the legacy manifest_type property** — off by default. InputStream Adaptive 21
  detects HLS from the mime type. Enable only if an older build refuses the manifest.

Picture quality is InputStream Adaptive's business, not this add-on's: cap the resolution in
that add-on's own settings.

## Limitations

* **1080p ceiling.** YouTube's HLS manifest tops out at 1920x1080. Higher resolutions exist
  only as separate video and audio DASH streams, which would require generating an MPD
  manifest instead.
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
