# yt-dlp Cast (plugin.video.ytdlpcast)

Cast YouTube from the phone app to Kodi. Kodi shows up as a TV in the YouTube app; each video
is resolved by **yt-dlp** and played through **InputStream Adaptive** — streaming, seekable,
with nothing written to disk.

Up to version 1.2 this add-on was only the playback back end for a fork of TubeCast. TubeCast
is no longer maintained, so since 2.0 the add-on receives casts itself and needs no other
add-on for it.

## Why

Kodi's YouTube add-ons each carry their own extraction code, and each stops working when
YouTube changes something on their side. yt-dlp exists to solve exactly that problem and
ships fixes continuously. This add-on is the thin layer that lets Kodi's player use it.

It is deliberately small: no browsing, no search, no library. The phone is the remote: it
picks the video, Kodi plays it.

## Casting from the YouTube app

Kodi becomes a "Watch on TV" device, the same way smart TVs without Chromecast built in do it:

1. **Finding Kodi.** The YouTube app searches the local network (SSDP), Kodi answers with a
   DIAL description, and the app lists it in its cast menu. Picking it hands Kodi a one-time
   pairing code, which Kodi registers with YouTube.
2. **Or a TV code.** *Settings → Cast → Link with a TV code* shows a code to type into the app
   (*Settings → Watch on TV → Enter TV code*). This works when the phone is on another network
   or the local search does not reach Kodi — some Android TV boxes and mesh/guest Wi-Fi
   networks drop multicast.
3. **The session runs through YouTube** (the Lounge API): the phone sends commands — play this
   queue, pause, seek, volume, next — and Kodi reports back what it is doing, so the phone's
   progress bar and controls stay in step.

Kodi keeps the same screen identity across restarts, so a phone linked once stays linked.
While casting is on, Kodi therefore holds one long-lived HTTPS connection to youtube.com.

Each video the phone sends is played through `plugin://plugin.video.ytdlpcast/?video_id=…`,
the same path as any other caller, so everything below — yt-dlp, the manifest rewriting, the
subtitles — applies to casts too.

**Next to TubeCast.** Both can be installed, but the phone then lists two receivers for the
same box. They are told apart by name: this add-on calls itself `<Kodi's name> (yt-dlp)`, and
it uses its own device identity rather than TubeCast's. Disable one of them; the add-on says so
once if it finds TubeCast enabled.

**Log.** With *Detailed cast log* on (the default), every step lands in `kodi.log` at INFO
level, without Kodi's debug mode: discovery, pairing, each command from the phone (`<-`) and
each report back (`->`). Search the log for `cast/`. Raw protocol traffic is added when Kodi's
debug logging is on.

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
| `inputstream.adaptive` | the demuxer; built into CoreELEC and LibreELEC |
| `script.module.certifi` | the CA bundle for HTTPS, in the official Kodi repository |

yt-dlp itself is **not** a dependency: the add-on ships a copy and keeps it current on its own.
Nor is TubeCast: casting is built in.

## Installation

Install from zip. Kodi fetches `script.module.certifi` from its official repository by itself;
on a box without access to it, install that module's zip first.

## Keeping yt-dlp current

A stale yt-dlp is the one thing that reliably breaks playback, because YouTube keeps changing
what extraction has to cope with. So the add-on updates yt-dlp itself, straight from the yt-dlp
project — no third-party Kodi module, no repository to keep enabled.

yt-dlp's release asset named plain `yt-dlp` is not a native binary: it is a shebang line
followed by a ZIP of the package's Python sources, which Python can import directly. The add-on
downloads that file, checks it, and puts it on `sys.path`.

* **When**: a minute after Kodi starts, then once a day, in the background service — never
  during playback. **Settings → yt-dlp → Check for updates now** checks immediately.
* **Channel**: *Nightly* (default) is built every day from the project's main branch, and
  fixes for YouTube changes land there first. *Stable* is released less often; yt-dlp promises
  a release at least every 90 days.
* **Checks before a new file is used** — nothing from it is executed:
  the SHA-256 against the release's own `SHA2-256SUMS`; the minimum Python it declares; and a
  parse of every source file with the running interpreter's grammar. A release that fails is
  rejected, remembered so it is not downloaded again, and the current version stays.
* **Why the Python check matters**: Kodi 21 ships Python 3.11, and yt-dlp drops old Pythons
  over time (3.9 went in 2025-10). When it drops 3.11, the add-on keeps the last compatible
  version instead of downloading one that cannot run — playback keeps working, it just stops
  getting fresher, and the log says so.
* **Storage**: `addon_data/plugin.video.ytdlpcast/ytdlp/`, one file per version, never
  overwritten in place (zipimport caches a ZIP's directory per path), current and previous
  kept. The copy shipped in the add-on (stable) is used until the first download succeeds, so
  the very first playback works offline too.

The checksum guards against a corrupted download, not against a compromised release: trust
ends at the yt-dlp project's GitHub releases, as it did with any repackaged module.

## Usage

From the phone: open a video in the YouTube app, tap the cast button, pick
`<Kodi's name> (yt-dlp)`.

From Kodi itself — a keymap, a favourite, another add-on:

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

### Cast

* **Receive casts from the YouTube app** — on by default. Off stops the receiver entirely:
  nothing listens on the network and no connection to youtube.com is kept.
* **Name shown on the phone** — empty by default, meaning Kodi's own name followed by
  "(yt-dlp)", e.g. "CoreELEC (yt-dlp)".
* **Discoverable on the local network** — on by default: answers the app's search (SSDP on
  UDP 1900, DIAL over HTTP on a port the system picks). Off leaves only the TV code.
* **Link with a TV code** — shows a code for *Settings → Watch on TV → Enter TV code* in the
  app, until a phone links, the dialog is cancelled or five minutes pass.
* **Detailed cast log** — on by default; see *Log* above. Warnings and errors are logged
  either way.

### How the picture and sound settings work

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

## Launching it from the Kodi UI

The add-on is a playback back end, not a browsable source, so opening it from Add-ons has
nothing to list. Instead of failing, it opens its settings and leaves a single "Open settings"
entry behind. The directory is ended as *succeeded* deliberately: failing it makes Kodi log an
error, bounce back to the previous folder and let the caller raise an error dialog.

Settings and messages are translated to Polish; English is the source language.

## Limitations

* **Available formats come from yt-dlp, not from this add-on.** Which resolutions and codecs
  a given video offers is decided by yt-dlp and by what YouTube publishes for it. The settings
  above can only narrow that list, never extend it.
* **Only the manifest is served locally.** The rewritten playlist sits in Kodi's temp
  directory and is served from `127.0.0.1`; the segment URLs inside it are YouTube's absolute
  ones, so no media is proxied. Look for `manifest server listening on` and `playing a
  rewritten manifest from` in Kodi's log to confirm the path is in use.
* **VOD only in practice.** Live streams work, but seeking backwards is limited to whatever
  DVR window the broadcaster provides — a server-side limit, not one imposed here.
* **JavaScript runtime.** yt-dlp warns that YouTube extraction without a JS runtime (deno,
  bun, node, quickjs) is deprecated. As measured, the HLS path currently resolves identically
  with and without one; if that changes, a runtime will have to be installed alongside.

## Development

Everything that can be is kept free of `xbmc` imports, so it runs on a desktop without Kodi:
`resolver.py` (resolution), and on the cast side `cast_protocol.py` (stream parser, queue),
`cast_lounge.py` (identity, session, long poll), `cast_discovery.py` (SSDP, DIAL) and
`cast_receiver.py` (what each command does). `cast_kodi.py` connects them to Kodi's player.
Kodi delivers Player and Monitor callbacks only to the thread that created them, so one
controller thread owns the player and every change of cast state; the network threads only
queue what they receive.

The project's `scripts/` directory has a test for each of these and a harness that runs the
receiver on a desktop against YouTube: a phone can cast to it while a fake player prints what
Kodi would do, or a fake phone links with a TV code and casts a video, so the whole path
through YouTube is checked without a phone in hand.

## Credits

The cast receiver started as a port of [TubeCast](https://github.com/enen92/script.tubecast)
by enen92, whose SSDP code comes from [Leapcast](https://github.com/dz0ny/leapcast), with the
startup and connection fixes from [Serph91P's continuation](https://github.com/Serph91P/script.tubecast).
The persistent screen and token refresh follow
[yt-cast-receiver](https://github.com/patrickkfkan/yt-cast-receiver). Along the way the port
dropped its two module dependencies (bottle, requests) and fixed a token refresh that never
ran, sessions that kept the previous one's command counter, and a device id shared by every
install.

## License

MIT; TubeCast's MIT notice is included in `LICENSE.txt`.
