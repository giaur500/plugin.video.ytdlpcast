# -*- coding: utf-8 -*-
"""The two read-only lines under Settings -> yt-dlp: what runs, and how the last check went.

Kodi settings cannot show live data, so these are ordinary string settings,
disabled in settings.xml so nobody edits them, and written here whenever what
they describe may have changed: by the service at start and after each check,
by the plugin after "Check now", after falling back to the bundled copy, and
just before it opens the settings itself.
"""

import time

import xbmcaddon

from . import diag, paths, ytdlp_loader

VERSION_SETTING = "ytdlp_info_version"
CHECK_SETTING = "ytdlp_info_check"

log = diag.logger("updates")


def _date(seconds, with_time=True):
    return time.strftime("%Y-%m-%d %H:%M" if with_time else "%Y-%m-%d", time.localtime(seconds))


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
        status = last.get("status")
        if status == ytdlp_loader.UP_TO_DATE:
            outcome = string(30170).format(last.get("version") or version)
        elif status == ytdlp_loader.UPDATED:
            outcome = string(30171).format(last.get("version"))
        elif status in (ytdlp_loader.REJECTED, ytdlp_loader.SKIPPED):
            outcome = string(30172).format(last.get("reason"))
        else:
            outcome = string(30173).format(last.get("reason"))
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
