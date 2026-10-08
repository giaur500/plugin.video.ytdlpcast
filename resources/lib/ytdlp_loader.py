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

No xbmc import here: scripts/test-loader.py exercises all of it on a desktop.
"""

import ast
import hashlib
import json
import os
import re
import sys
import tempfile
import time
import urllib.request
import zipfile

from . import diag

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

_MIN_SUPPORTED = re.compile(r"MIN_SUPPORTED\s*,\s*MIN_RECOMMENDED\s*=\s*\(\s*(\d+)\s*,\s*(\d+)\s*\)")
_VERSION = re.compile(r"""^__version__\s*=\s*['"]([^'"]+)['"]""", re.M)


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


def compatible(zip_path, python=None):
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
        for name in archive.namelist():
            if not name.endswith(".py"):
                continue
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
    # Write-then-rename: a reader never sees a half-written file.
    fd, tmp = tempfile.mkstemp(dir=store_dir, prefix=".state-")
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=1, sort_keys=True)
    os.replace(tmp, os.path.join(store_dir, STATE))


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

    source is "downloaded" or "bundled". Importing is left to the caller, which
    can fall back to activate_bundled() if the import itself fails.
    """
    path, version = current(store_dir) if store_dir else (None, None)
    if path:
        _prepend(path)
        log.info("using the downloaded %s (%s)", version, path)
        return version, "downloaded"
    return activate_bundled(bundled_path)


def activate_bundled(bundled_path):
    version, _ = inspect(bundled_path)
    _prepend(bundled_path)
    log.info("using the bundled %s (%s)", version, bundled_path)
    return version, "bundled"


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


def _fetch(url, timeout):
    request = urllib.request.Request(url, headers={"User-Agent": "plugin.video.ytdlpcast"})
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = response.read()
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


def update(store_dir, channel, python=None, timeout=60):
    """Bring store_dir up to date with the channel. Never removes the file in use.

    Whatever the outcome, it is kept in the state as last_result -- the
    settings screen shows it.
    """
    started = time.monotonic()
    log.info("checking %s: %s", channel, _url(channel, ASSET) if channel in CHANNELS else "?")
    result = _update(store_dir, channel, python, timeout)
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


def _update(store_dir, channel, python, timeout):
    if channel not in CHANNELS:
        return Result(FAILED, reason="unknown channel {!r}".format(channel))
    os.makedirs(store_dir, exist_ok=True)
    state = _read_state(store_dir)
    state["checked_at"] = int(time.time())

    try:
        sha = published_sha256(channel)
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

    fd, tmp = tempfile.mkstemp(dir=store_dir, prefix=".download-")
    os.close(fd)
    try:
        fetched = time.monotonic()
        data = _fetch(_url(channel, ASSET), timeout)
        log.info("downloaded %d bytes in %.1f s", len(data), time.monotonic() - fetched)
        if hashlib.sha256(data).hexdigest() != sha:
            return Result(FAILED, reason="download does not match {}".format(SUMS))
        with open(tmp, "wb") as handle:
            handle.write(data)
        if not zipfile.is_zipfile(tmp):
            return _reject(store_dir, state, sha, "not a ZIP archive")
        ok, reason = compatible(tmp, python)
        if not ok:
            return _reject(store_dir, state, sha, reason)
        version, _ = inspect(tmp)
        name = "yt-dlp-{}.zip".format(re.sub(r"[^A-Za-z0-9._-]", "_", version))
        target = os.path.join(store_dir, name)
        if not os.path.exists(target):  # versioned: never overwrite a file in place
            os.replace(tmp, target)
        state.update(current=name, version=version, channel=channel, sha256=sha)
        _write_state(store_dir, state)
        log.info("installed as %s", name)
        _cleanup(store_dir, keep_first=name)
        return Result(UPDATED, version=version)
    except Exception as error:  # noqa: BLE001 - the file in use stays untouched
        return Result(FAILED, reason=str(error))
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


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
