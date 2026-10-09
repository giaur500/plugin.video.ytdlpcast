# -*- coding: utf-8 -*-
"""Keeps a current yt-dlp available without any third-party Kodi module.

yt-dlp's release asset named plain "yt-dlp" is not a native binary: it is a
shebang line followed by a ZIP of the package's Python sources. Python can
import a package straight out of a ZIP on sys.path, so this module downloads
that file from the yt-dlp project itself, checks it, and puts it on sys.path.

Two properties matter more than freshness:

* A new file is only switched to after it has been shown to fit the running
  Python -- read without executing anything: the declared minimum version and a
  parse of every source file with this interpreter's grammar. yt-dlp drops old
  Pythons over time (3.9 went in 2025-10); when it drops the one Kodi ships,
  this keeps the last compatible version instead of breaking playback.
* Files are versioned and never overwritten in place. zipimport caches a ZIP's
  directory per path, so replacing a file under the same name inside a running
  process would make later imports read the wrong offsets.

* Every \\N{NAME} escape in yt-dlp's string literals is rewritten to the
  equivalent \\uXXXX before anything compiles the sources. CPython 3.11, the
  Python of Kodi 21, decodes \\N{} through a pointer it keeps in a process-wide
  static (Objects/unicodeobject.c, ucnhash_capi) and fills from the unicodedata
  module of whichever interpreter decodes one first. Kodi runs every plugin call
  in its own sub-interpreter and ends it afterwards, freeing that module -- so
  the next \\N{} anywhere in Kodi jumps through freed memory and Kodi crashes.
  yt-dlp is imported from source (a ZIP has no .pyc), so YoutubeDL.py's
  '\\N{FULLWIDTH COMMA}' hit that on every playback after the first. Fixed in
  CPython 3.12; until Kodi ships it, no \\N{} may ever be decoded here.

* Compiled once. A plugin call is a fresh interpreter: importing from the
  archive compiles every yt-dlp module it needs (about a hundred) from source,
  on every playback -- seconds on an ARM box. So the service extracts each
  archive it will use into a directory and byte-compiles it once, in the
  background; the plugin imports the .pyc files from there and only falls back
  to the archive until that directory is ready.

No xbmc import here: scripts/test-loader.py exercises all of it on a desktop.
"""

import ast
import hashlib
import importlib.util
import io
import json
import os
import py_compile
import re
import shutil
import sys
import tempfile
import time
import tokenize
import unicodedata
import urllib.parse
import urllib.request
import zipfile

from . import diag, fileutil

log = diag.logger("updates")

CHANNELS = {
    "nightly": "yt-dlp/yt-dlp-nightly-builds",
    "stable": "yt-dlp/yt-dlp",
}
# Index = value of the add-on's ytdlp_channel setting.
SETTING_CHANNELS = ("nightly", "stable")
ASSET = "yt-dlp"
SUMS = "SHA2-256SUMS"
STATE = "state.json"
KEEP = 2  # the current file and the one before it

UP_TO_DATE, UPDATED, SKIPPED, REJECTED, FAILED = (
    "up_to_date", "updated", "skipped", "rejected", "failed")
CANCELLED = "cancelled"

# Written as the ZIP comment of every archive whose \\N{} escapes were rewritten.
PATCH_MARK = b"ytdlpcast:named-escapes-1"
_NAMED_ESCAPE = re.compile(r"\\N\{([^}\\]+)\}")

_MIN_SUPPORTED = re.compile(r"MIN_SUPPORTED\s*,\s*MIN_RECOMMENDED\s*=\s*\(\s*(\d+)\s*,\s*(\d+)\s*\)")
_VERSION = re.compile(r"""^__version__\s*=\s*['"]([^'"]+)['"]""", re.M)


class Cancelled(Exception):
    """The user stopped a check they started from the settings."""


def _report(progress, stage, fraction):
    if progress is not None:
        progress(stage, max(0.0, min(1.0, fraction)))


def _check(should_stop):
    if should_stop is not None and should_stop():
        raise Cancelled()


class Result:
    """Outcome of an update attempt, readable in a log line or a notification."""

    def __init__(self, status, version=None, reason=None):
        self.status = status
        self.version = version
        self.reason = reason

    def __repr__(self):
        parts = [self.status]
        if self.version:
            parts.append(self.version)
        if self.reason:
            parts.append(self.reason)
        return "Result({})".format(", ".join(parts))


# ---------------------------------------------------------------------------
# Reading a downloaded file without running it
# ---------------------------------------------------------------------------

def inspect(zip_path):
    """(version, (major, minor) minimum Python) read from inside the archive."""
    with zipfile.ZipFile(zip_path) as archive:
        version_src = archive.read("yt_dlp/version.py").decode("utf-8")
        update_src = archive.read("yt_dlp/update.py").decode("utf-8")
    version = _VERSION.search(version_src)
    minimum = _MIN_SUPPORTED.search(update_src)
    if not version:
        raise ValueError("no __version__ in yt_dlp/version.py")
    if not minimum:
        raise ValueError("no MIN_SUPPORTED in yt_dlp/update.py")
    return version.group(1), (int(minimum.group(1)), int(minimum.group(2)))


def compatible(zip_path, python=None, progress=None, should_stop=None):
    """(ok, reason): can this interpreter load the archive?

    Nothing from the archive is executed. The declared minimum is compared
    first; then every source file is parsed with the grammar of `python`, which
    catches syntax a newer release relies on even if the minimum was not bumped.
    """
    python = tuple(python or sys.version_info[:2])
    try:
        _, minimum = inspect(zip_path)
    except (KeyError, ValueError, zipfile.BadZipFile) as error:
        return False, "not a yt-dlp archive ({})".format(error)
    if minimum > python:
        return False, "needs Python {}.{}, this is {}.{}".format(*minimum, *python)
    started = time.monotonic()
    parsed = 0
    with zipfile.ZipFile(zip_path) as archive:
        sources = [name for name in archive.namelist() if name.endswith(".py")]
        for index, name in enumerate(sources):
            _check(should_stop)
            if index % 20 == 0:
                _report(progress, "validate", index / len(sources))
            try:
                ast.parse(archive.read(name), filename=name, feature_version=python)
            except SyntaxError as error:
                return False, "{} does not parse on Python {}.{} (line {})".format(
                    name, python[0], python[1], error.lineno)
            parsed += 1
    log.info("validated %s: needs Python %d.%d (have %d.%d), %d source files parse, %.1f s",
             os.path.basename(zip_path), minimum[0], minimum[1], python[0], python[1], parsed,
             time.monotonic() - started)
    return True, None


# ---------------------------------------------------------------------------
# \\N{} escapes (see the module docstring for why)
# ---------------------------------------------------------------------------

def neutralize_named_escapes(source):
    """(source, count): source bytes with every \\N{NAME} in a str literal as \\uXXXX.

    Raw and bytes literals are left alone -- there \\N{} is not an escape. The
    code point comes from unicodedata.lookup(), which reads the module's own
    tables and never touches the process-wide pointer.
    """
    if b"\\N{" not in source:
        return source, 0
    text = source.decode("utf-8")
    line_starts = [0]
    for line in text.splitlines(keepends=True):
        line_starts.append(line_starts[-1] + len(line))
    edits = []
    fstring_prefixes = []  # Python 3.12+ splits f-strings into several tokens
    for token in tokenize.generate_tokens(io.StringIO(text).readline):
        kind = tokenize.tok_name.get(token.type)
        if kind == "FSTRING_START":
            fstring_prefixes.append(re.match(r"[A-Za-z]*", token.string).group(0))
            continue
        if kind == "FSTRING_END":
            fstring_prefixes.pop()
            continue
        if kind == "STRING":
            prefix = re.match(r"[A-Za-z]*", token.string).group(0)
        elif kind == "FSTRING_MIDDLE" and fstring_prefixes:
            prefix = fstring_prefixes[-1]
        else:
            continue
        if "r" in prefix.lower() or "b" in prefix.lower():
            continue
        start = line_starts[token.start[0] - 1] + token.start[1]
        end = line_starts[token.end[0] - 1] + token.end[1]
        segment = text[start:end]
        for match in _NAMED_ESCAPE.finditer(segment):
            backslashes = 0
            while match.start() - backslashes - 1 >= 0 and segment[match.start() - backslashes - 1] == "\\":
                backslashes += 1
            if backslashes % 2:
                continue  # "\\\\N{" -- an escaped backslash, then plain text
            try:
                code = ord(unicodedata.lookup(match.group(1)))
            except KeyError:
                continue  # not a character name; compiling would fail on it anyway
            replacement = "\\u{:04x}".format(code) if code <= 0xFFFF else "\\U{:08x}".format(code)
            edits.append((start + match.start(), start + match.end(), replacement))
    for begin, finish, replacement in sorted(edits, reverse=True):
        text = text[:begin] + replacement + text[finish:]
    return text.encode("utf-8"), len(edits)


def patch_archive(source_path, target_path):
    """Write target_path: the archive with \\N{} rewritten and PATCH_MARK set. Returns the count."""
    count = 0
    with zipfile.ZipFile(source_path) as source, \
            zipfile.ZipFile(target_path, "w", zipfile.ZIP_DEFLATED) as target:
        for info in source.infolist():
            data = source.read(info)
            if info.filename.endswith(".py"):
                data, changed = neutralize_named_escapes(data)
                if changed:
                    log.info("rewrote %d \\N{} escape(s) in %s", changed, info.filename)
                count += changed
            target.writestr(info, data, compress_type=zipfile.ZIP_DEFLATED)
        target.comment = PATCH_MARK
    return count


def is_patched(zip_path):
    try:
        with zipfile.ZipFile(zip_path) as archive:
            return archive.comment == PATCH_MARK
    except (OSError, zipfile.BadZipFile):
        return False


def ensure_patched(store_dir):
    """Rewrite the downloaded copy in use if an older add-on version stored it unpatched.

    Run by the service at start. The result gets a new name: zipimport caches
    an archive's directory per path, so a file is never rewritten in place.
    """
    state = _read_state(store_dir) if store_dir and os.path.isdir(store_dir) else {}
    name = state.get("current")
    path = os.path.join(store_dir, name) if name else None
    if not path or not os.path.isfile(path) or is_patched(path):
        return False
    patched = name[:-4] + ".p1.zip"
    fd, tmp = tempfile.mkstemp(dir=store_dir, prefix=".patch-")
    os.close(fd)
    try:
        count = patch_archive(path, tmp)
        os.replace(tmp, os.path.join(store_dir, patched))
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    state["current"] = patched
    _write_state(store_dir, state)
    _cleanup(store_dir, keep_first=patched)
    log.info("patched %s -> %s (%d \\N{} escape(s))", name, patched, count)
    return True


# ---------------------------------------------------------------------------
# State on disk
# ---------------------------------------------------------------------------

def _read_state(store_dir):
    try:
        with open(os.path.join(store_dir, STATE), encoding="utf-8") as handle:
            state = json.load(handle)
        return state if isinstance(state, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_state(store_dir, state):
    fileutil.write_json_atomic(os.path.join(store_dir, STATE), state, indent=1, sort_keys=True)


def current(store_dir):
    """(path, version) of the downloaded file in use, or (None, None)."""
    state = _read_state(store_dir)
    name = state.get("current")
    if not name:
        return None, None
    path = os.path.join(store_dir, name)
    if not os.path.isfile(path):
        return None, None
    return path, state.get("version")


def activate(store_dir, bundled_path):
    """Put the best available yt-dlp first on sys.path; return (version, source).

    source is "downloaded" or "bundled". Each comes from its compiled directory
    when the service has prepared one, else from its archive. Importing is left
    to the caller, which can fall back to activate_bundled() if it fails.
    """
    path, version = current(store_dir) if store_dir else (None, None)
    if path and not is_patched(path):
        # Downloaded by an older add-on version; the service patches it at start.
        # Until then its \\N{} escapes would be decoded on import -- see the docstring.
        log.warning("the downloaded yt-dlp %s is not patched yet; using the bundled copy", version)
        path = None
    if path:
        _use(path, compiled_dir(store_dir, os.path.basename(path)), "downloaded", version)
        return version, "downloaded"
    return activate_bundled(bundled_path, store_dir)


def activate_bundled(bundled_path, store_dir=None):
    version, _ = inspect(bundled_path)
    if not is_patched(bundled_path):
        log.error("BUG: the bundled yt-dlp %s was not patched at build time", version)
    _use(bundled_path, bundled_dir(store_dir, version) if store_dir else None, "bundled", version)
    return version, "bundled"


def _use(archive, directory, source, version):
    if directory and is_compiled(directory):
        _prepend(directory)
        log.info("using the %s %s, compiled (%s)", source, version, directory)
    else:
        _prepend(archive)
        log.info("using the %s %s from its archive, compiling on import (%s)", source, version, archive)


# ---------------------------------------------------------------------------
# Compiled once: an extracted, byte-compiled copy of each archive in use
# ---------------------------------------------------------------------------

READY = ".ytdlpcast-compiled"


def compiled_dir(store_dir, archive_name):
    """The directory an archive is extracted to: its name without ".zip"."""
    return os.path.join(store_dir, archive_name[:-len(".zip")])


def bundled_dir(store_dir, version):
    return os.path.join(store_dir, "bundled-" + _safe_version(version))


def _safe_version(version):
    """A version string as a file name: yt-dlp's are "2026.08.19" or "2026.08.19.232755"."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", version)


def is_compiled(directory):
    """Ready for this interpreter: extracted, and compiled by a Python with the same bytecode tag."""
    try:
        with open(os.path.join(directory, READY), encoding="utf-8") as handle:
            return handle.read().strip() == sys.implementation.cache_tag
    except OSError:
        return False


def build_compiled(archive, directory, should_stop=None, progress=None):
    """Extract archive into directory and byte-compile it; True when it is ready.

    Safe to interrupt (should_stop() is checked between files) and to resume:
    files already compiled are skipped. The directory appears in one rename, and
    READY is written last, so a plugin call never picks up half of it.
    """
    if is_compiled(directory):
        return True
    started = time.monotonic()
    if not os.path.isdir(directory):
        staging = tempfile.mkdtemp(dir=os.path.dirname(directory), prefix=".extract-")
        try:
            with zipfile.ZipFile(archive) as source:
                for name in source.namelist():
                    if name.startswith("/") or ".." in name.split("/"):
                        raise ValueError("unsafe path in {}: {}".format(os.path.basename(archive), name))
                source.extractall(staging)
            os.rename(staging, directory)
        except OSError:
            shutil.rmtree(staging, ignore_errors=True)
            if not os.path.isdir(directory):  # another builder may have won the race
                raise
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        log.info("extracted %s in %.1f s", os.path.basename(archive), time.monotonic() - started)
    compiled = skipped = 0
    sources = [os.path.join(root, name) for root, _, files in os.walk(directory)
               for name in files if name.endswith(".py")]
    for index, source_path in enumerate(sources):
        if index % 20 == 0:
            _report(progress, "compile", index / len(sources))
        if should_stop and should_stop():
            log.info("compiling %s interrupted after %d files; it resumes next time",
                     os.path.basename(directory), compiled)
            return False
        cache = importlib.util.cache_from_source(source_path)
        if os.path.exists(cache) and os.path.getmtime(cache) >= os.path.getmtime(source_path):
            skipped += 1
            continue
        try:
            py_compile.compile(source_path, cfile=cache, doraise=True)
            compiled += 1
        except py_compile.PyCompileError as error:
            log.warning("%s does not compile: %s", source_path, error.msg)
    with open(os.path.join(directory, READY), "w", encoding="utf-8") as handle:
        handle.write(sys.implementation.cache_tag)
    log.info("compiled %d files of %s in %.1f s (%d already done)", compiled, os.path.basename(directory),
             time.monotonic() - started, skipped)
    return True


def prepare(store_dir, bundled_path, should_stop=None, cleanup=False):
    """Build the compiled copies the plugin may use; [(version, seconds)] of those built now.

    The downloaded copy in use and the bundled one (the fallback). cleanup
    removes compiled copies of anything else -- done at service start, when no
    playback can still be importing from an old one.
    """
    os.makedirs(store_dir, exist_ok=True)
    wanted, built = set(), []
    jobs = []
    path, version = current(store_dir)
    if path and is_patched(path):
        jobs.append((path, compiled_dir(store_dir, os.path.basename(path)), version))
    try:
        bundled_version, _ = inspect(bundled_path)
        if is_patched(bundled_path):
            jobs.append((bundled_path, bundled_dir(store_dir, bundled_version), bundled_version))
    except (OSError, KeyError, ValueError, zipfile.BadZipFile):
        pass
    for archive, directory, label in jobs:
        wanted.add(os.path.basename(directory))
        if is_compiled(directory):
            continue
        started = time.monotonic()
        if not build_compiled(archive, directory, should_stop):
            break
        built.append((label, time.monotonic() - started))
    if cleanup:
        for name in os.listdir(store_dir):
            full = os.path.join(store_dir, name)
            if (os.path.isdir(full) and name not in wanted
                    and name.startswith(("yt-dlp-", "bundled-", ".extract-"))):
                shutil.rmtree(full, ignore_errors=True)
                log.info("removed the compiled copy %s", name)
    return built


def mark_broken(store_dir, broken):
    """Remember that the downloaded file does not import, for describe(); False clears it."""
    state = _read_state(store_dir)
    name = state.get("current") if broken else None
    if state.get("broken") != name:
        state["broken"] = name
        _write_state(store_dir, state)


def describe(store_dir, bundled_path):
    """What the add-on runs and how the last check went, for the settings screen.

    {"version", "source": downloaded|bundled|fallback, "channel", "updated_at",
     "checked_at", "last": {"status", "version", "reason", "at"} or None}
    """
    state = _read_state(store_dir) if store_dir and os.path.isdir(store_dir) else {}
    path, version = current(store_dir) if store_dir else (None, None)
    info = {"channel": state.get("channel"), "updated_at": state.get("updated_at"),
            "checked_at": state.get("checked_at"), "last": state.get("last_result")}
    if path and state.get("broken") != state.get("current"):
        # States from before 2.1 have no updated_at; the file's age says the same.
        info.update(version=version, source="downloaded",
                    updated_at=info["updated_at"] or int(os.path.getmtime(path)))
        return info
    try:
        bundled, _ = inspect(bundled_path)
    except (OSError, KeyError, ValueError, zipfile.BadZipFile):
        bundled = None
    # The bundled copy, either because nothing was downloaded yet or because
    # the download does not import; it is always built from the stable channel.
    info.update(version=bundled, source="fallback" if path else "bundled", channel="stable")
    return info


def _prepend(path):
    if path in sys.path:
        sys.path.remove(path)
    sys.path.insert(0, path)


# ---------------------------------------------------------------------------
# Updating
# ---------------------------------------------------------------------------

def _url(channel, asset):
    return "https://github.com/{}/releases/latest/download/{}".format(CHANNELS[channel], asset)


def _fetch(url, timeout, progress=None, should_stop=None):
    request = urllib.request.Request(url, headers={"User-Agent": "plugin.video.ytdlpcast"})
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            total = int(response.headers.get("Content-Length") or 0)
            chunks, done = [], 0
            while True:
                _check(should_stop)
                chunk = response.read(65536)
                if not chunk:
                    break
                chunks.append(chunk)
                done += len(chunk)
                if total:
                    _report(progress, "download", done / total)
            data = b"".join(chunks)
            diag.log_request("GET", url, started, response.status, len(data))
            return data
    except Exception as error:
        diag.log_request("GET", url, started, error=error)
        raise


def published_sha256(channel, timeout=20):
    """SHA-256 of the asset in the channel's latest release, from SHA2-256SUMS."""
    for line in _fetch(_url(channel, SUMS), timeout).decode("utf-8", "replace").splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].lstrip("*") == ASSET:
            return parts[0].lower()
    raise ValueError("{} has no line for {}".format(SUMS, ASSET))


def latest_version(channel, timeout=20):
    """The tag of the channel's latest release -- yt-dlp's version string.

    Read from where github.com/<repo>/releases/latest redirects (.../tag/<tag>):
    no API call, so no API rate limit.
    """
    url = "https://github.com/{}/releases/latest".format(CHANNELS[channel])
    request = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "plugin.video.ytdlpcast"})
    started = time.monotonic()
    with urllib.request.urlopen(request, timeout=timeout) as response:
        final = response.geturl()
        diag.log_request("HEAD", url, started, response.status, 0, note=" -> " + final)
    tag = final.rstrip("/").rsplit("/tag/", 1)[-1] if "/tag/" in final else None
    if not tag:
        raise ValueError("no release tag in {}".format(final))
    return urllib.parse.unquote(tag)


def check(store_dir, channel, bundled_path, timeout=20):
    """What runs and what the channel offers, without changing anything.

    {"version", "source", "latest", "available", "reason"}: available is False
    when the copy in use is the channel's latest, or when that release was
    rejected before (reason says why).
    """
    info = describe(store_dir, bundled_path)
    latest = latest_version(channel, timeout)
    sha = published_sha256(channel, timeout)
    state = _read_state(store_dir) if os.path.isdir(store_dir) else {}
    in_use = info["source"] == "downloaded" and state.get("sha256") == sha
    rejected = sha in state.get("rejected", [])
    return {"version": info.get("version"), "source": info["source"], "latest": latest,
            "available": not in_use and not rejected,
            "reason": "release {} was rejected before".format(latest) if rejected and not in_use else None}


def update(store_dir, channel, python=None, timeout=60, progress=None, should_stop=None):
    """Bring store_dir up to date with the channel. Never removes the file in use.

    Whatever the outcome, it is kept in the state as last_result -- the
    settings screen shows it.
    """
    started = time.monotonic()
    log.info("checking %s: %s", channel, _url(channel, ASSET) if channel in CHANNELS else "?")
    try:
        result = _update(store_dir, channel, python, timeout, progress, should_stop)
    except Cancelled:
        result = Result(CANCELLED, reason="cancelled")
    if os.path.isdir(store_dir):
        state = _read_state(store_dir)
        state["last_result"] = {"status": result.status, "version": result.version,
                                "reason": result.reason, "at": int(time.time())}
        if result.status == UPDATED:
            state["updated_at"] = state["last_result"]["at"]
            state["broken"] = None
        _write_state(store_dir, state)
    log.info("check finished in %.1f s: %s", time.monotonic() - started, result)
    return result


def _update(store_dir, channel, python, timeout, progress=None, should_stop=None):
    if channel not in CHANNELS:
        return Result(FAILED, reason="unknown channel {!r}".format(channel))
    os.makedirs(store_dir, exist_ok=True)
    state = _read_state(store_dir)
    state["checked_at"] = int(time.time())

    _report(progress, "check", 0)
    try:
        sha = published_sha256(channel, timeout)
    except Cancelled:
        raise
    except Exception as error:  # noqa: BLE001 - network trouble is an outcome, not a crash
        _write_state(store_dir, state)
        return Result(FAILED, reason="checksums unavailable: {}".format(error))

    in_use, _ = current(store_dir)
    log.info("published SHA-256 %s; in use: %s, SHA-256 %s", sha[:16],
             os.path.basename(in_use) if in_use else "the bundled copy", (state.get("sha256") or "-")[:16])
    if in_use and state.get("sha256") == sha:
        _write_state(store_dir, state)
        return Result(UP_TO_DATE, version=state.get("version"))
    if sha in state.get("rejected", []):
        _write_state(store_dir, state)
        return Result(SKIPPED, reason="release {} was rejected before".format(sha[:12]))

    fd, raw = tempfile.mkstemp(dir=store_dir, prefix=".download-")
    os.close(fd)
    fd, tmp = tempfile.mkstemp(dir=store_dir, prefix=".patched-")
    os.close(fd)
    try:
        fetched = time.monotonic()
        data = _fetch(_url(channel, ASSET), timeout, progress, should_stop)
        log.info("downloaded %d bytes in %.1f s", len(data), time.monotonic() - fetched)
        if hashlib.sha256(data).hexdigest() != sha:
            return Result(FAILED, reason="download does not match {}".format(SUMS))
        with open(raw, "wb") as handle:
            handle.write(data)
        if not zipfile.is_zipfile(raw):
            return _reject(store_dir, state, sha, "not a ZIP archive")
        # Before anything parses it: validation compiles every source file too.
        patch_archive(raw, tmp)
        ok, reason = compatible(tmp, python, progress, should_stop)
        if not ok:
            return _reject(store_dir, state, sha, reason)
        version, _ = inspect(tmp)
        name = "yt-dlp-{}.zip".format(_safe_version(version))
        target = os.path.join(store_dir, name)
        if not os.path.exists(target):  # versioned: never overwrite a file in place
            os.replace(tmp, target)
        state.update(current=name, version=version, channel=channel, sha256=sha)
        _write_state(store_dir, state)
        log.info("installed as %s", name)
        _cleanup(store_dir, keep_first=name)
        return Result(UPDATED, version=version)
    except Cancelled:
        raise
    except Exception as error:  # noqa: BLE001 - the file in use stays untouched
        return Result(FAILED, reason=str(error))
    finally:
        for leftover in (raw, tmp):
            if os.path.exists(leftover):
                os.remove(leftover)


def _reject(store_dir, state, sha, reason):
    rejected = state.setdefault("rejected", [])
    if sha not in rejected:
        rejected.append(sha)
    del rejected[:-20]  # bounded
    _write_state(store_dir, state)
    return Result(REJECTED, reason=reason)


def _cleanup(store_dir, keep_first):
    files = sorted(
        (name for name in os.listdir(store_dir) if name.startswith("yt-dlp-") and name.endswith(".zip")),
        key=lambda name: os.path.getmtime(os.path.join(store_dir, name)),
        reverse=True)
    keep = [keep_first] + [name for name in files if name != keep_first][:KEEP - 1]
    for name in files:
        if name not in keep:
            try:
                os.remove(os.path.join(store_dir, name))
                log.info("removed the older %s", name)
            except OSError:
                pass
