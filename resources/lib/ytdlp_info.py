# -*- coding: utf-8 -*-
"""Settings -> yt-dlp: the two read-only lines and the "Check for updates now" dialog.

Kodi settings cannot show live data, so the lines -- what runs, and how the
last check went -- are ordinary string settings, disabled in settings.xml so
nobody edits them, and written here whenever what they describe may have
changed: by the service at start and after each check, by the plugin after
"Check now", after falling back to the bundled copy, and just before it opens
the settings itself.
"""

import os
import time

import xbmc
import xbmcaddon
import xbmcgui

from . import diag, kodilog, paths, resolver, ytdlp_loader

VERSION_SETTING = "ytdlp_info_version"
CHECK_SETTING = "ytdlp_info_check"

log = diag.logger("updates")


def _date(seconds, with_time=True):
    return time.strftime("%Y-%m-%d %H:%M" if with_time else "%Y-%m-%d", time.localtime(seconds))


def outcome_text(string, status, version=None, reason=None):
    """An update outcome -- a ytdlp_loader status -- in the user's language."""
    if status == ytdlp_loader.UP_TO_DATE:
        return string(30170).format(version)
    if status == ytdlp_loader.UPDATED:
        return string(30171).format(version)
    if status in (ytdlp_loader.REJECTED, ytdlp_loader.SKIPPED):
        return string(30172).format(reason)
    if status == ytdlp_loader.CANCELLED:
        return string(30250)
    return string(30173).format(reason)


def texts(addon):
    """(version line, last check line), in the user's language."""
    string = addon.getLocalizedString
    info = ytdlp_loader.describe(paths.ytdlp_directory(), paths.bundled_ytdlp())
    version = info.get("version") or "?"
    if info["source"] == "downloaded":
        line = "{} · {} · {} {}".format(version, info.get("channel") or "?", string(30177),
                                        _date(info["updated_at"], with_time=False)).strip()
    elif info["source"] == "fallback":
        line = "{} · {} · {}".format(version, string(30178), string(30169))
    else:
        line = "{} · {} (stable)".format(version, string(30178))

    last = info.get("last")
    if last:
        outcome = outcome_text(string, last.get("status"), last.get("version") or version, last.get("reason"))
        check = "{} · {}".format(_date(last.get("at") or 0), outcome)
    elif info.get("checked_at"):
        check = _date(info["checked_at"])  # a state from before 2.1: date only
    else:
        check = string(30179)
    return line, check


def refresh(addon=None):
    """Write both lines, only where they changed -- every write is a settings change."""
    addon = addon or xbmcaddon.Addon()
    try:
        line, check = texts(addon)
    except Exception:  # noqa: BLE001 - an info line must never break playback or the service
        log.exception("could not describe the yt-dlp in use")
        return
    for setting, value in ((VERSION_SETTING, line), (CHECK_SETTING, check)):
        if addon.getSettingString(setting) != value:
            addon.setSettingString(setting, value)


# Manual update: (start %, end %, message) per stage of the progress dialog.
UPDATE_STAGES = {
    "check": (0, 5, 30245),
    "download": (5, 50, 30246),
    "validate": (50, 75, 30247),
    "compile": (75, 100, 30248),
}


def update_dialog(addon):
    """Settings button: show what runs and what the channel offers; update on request.

    Three of Kodi's own dialogs in a row: the two versions (with "Update" when
    there is something newer), the progress through every step -- download,
    validation against Kodi's Python, the one-time compile -- and the outcome.
    When it says ready, the next playback already runs the new version at full
    speed. Cancel stops before anything is replaced; cancelled during the
    compile, the new version is in place and the service finishes compiling it
    at the next start.
    """
    string = addon.getLocalizedString
    name = addon.getAddonInfo("name")
    channel = ytdlp_loader.SETTING_CHANNELS[addon.getSettingInt("ytdlp_channel")]
    store = paths.ytdlp_directory()
    dialog = xbmcgui.Dialog()

    busy = xbmcgui.DialogProgress()
    busy.create(name, string(30245).format(channel))
    try:
        ytdlp_loader.ensure_patched(store)  # a copy an older version left unpatched
        found = ytdlp_loader.check(store, channel, paths.bundled_ytdlp())
    except Exception as error:  # noqa: BLE001 - network trouble is an answer here, not a crash
        busy.close()
        kodilog.log("manual yt-dlp check on {} failed: {}".format(channel, error), xbmc.LOGWARNING)
        dialog.ok(name, string(30173).format(resolver.short_error(error)))
        return
    busy.close()

    source = string(30177 if found["source"] == "downloaded" else 30178)
    versions = "{}: [B]{}[/B] ({})[CR]{}: [B]{}[/B]".format(
        string(30175), found["version"] or "?", source, string(30252).format(channel), found["latest"])
    kodilog.log("manual yt-dlp check on {}: in use {} ({}), latest {}, update {}".format(
        channel, found["version"], found["source"], found["latest"],
        "available" if found["available"] else "not needed"))
    if not found["available"]:
        dialog.ok(name, "{}[CR][CR]{}".format(versions, found["reason"] or string(30253)))
        return
    if not dialog.yesno(name, "{}[CR][CR]{}".format(versions, string(30254)),
                        nolabel=string(30255), yeslabel=string(30256)):
        return

    progress_dialog = xbmcgui.DialogProgress()
    progress_dialog.create(name, string(30245).format(channel))

    def progress(stage, fraction):
        low, high, message = UPDATE_STAGES[stage]
        progress_dialog.update(int(low + (high - low) * fraction),
                               string(message).format(found["latest"] if stage == "download" else channel))

    ready = False
    try:
        result = ytdlp_loader.update(store, channel, progress=progress, should_stop=progress_dialog.iscanceled)
        kodilog.log("manual yt-dlp update on {}: {}".format(channel, result))
        if result.status in (ytdlp_loader.UPDATED, ytdlp_loader.UP_TO_DATE):
            path, _ = ytdlp_loader.current(store)
            if path and ytdlp_loader.is_patched(path):
                try:
                    ready = ytdlp_loader.build_compiled(
                        path, ytdlp_loader.compiled_dir(store, os.path.basename(path)),
                        should_stop=progress_dialog.iscanceled, progress=progress)
                except (OSError, ValueError) as error:
                    # The archive is installed and imports as it is; the service retries the compile.
                    kodilog.log("compiling yt-dlp {} failed: {}".format(result.version, error), xbmc.LOGWARNING)
    finally:
        progress_dialog.close()
    refresh(addon)

    if result.status in (ytdlp_loader.UPDATED, ytdlp_loader.UP_TO_DATE):
        outcome = string(30249 if ready else 30251).format(result.version)
    else:
        outcome = outcome_text(string, result.status, result.version, result.reason)
    dialog.ok(name, outcome)
