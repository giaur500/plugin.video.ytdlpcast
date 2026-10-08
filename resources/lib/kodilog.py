# -*- coding: utf-8 -*-
"""Kodi side of the add-on's logging: the handler and the Diagnostics switches.

Every module logs through diag's per-area loggers; this writes them to kodi.log
as "[plugin.video.ytdlpcast] <area>: …" -- "cast/lounge", "manifest", "player"
-- and applies the switches. Called once per process: by the service at start
and on every settings change, by the plugin at the start of each run.
"""

import logging

import xbmc
import xbmcaddon

from . import diag

ADDON_ID = xbmcaddon.Addon().getAddonInfo("id")

_LEVELS = {
    logging.DEBUG: xbmc.LOGDEBUG,
    logging.INFO: xbmc.LOGINFO,
    diag.NOTICE: xbmc.LOGINFO,
    logging.WARNING: xbmc.LOGWARNING,
    logging.ERROR: xbmc.LOGERROR,
    logging.CRITICAL: xbmc.LOGFATAL,
}


class KodiLogHandler(logging.Handler):

    def emit(self, record):
        try:
            message = self.format(record)
        except Exception:  # noqa: BLE001 - a bad log call must not break anything
            message = str(record.msg)
        area = record.name[len(diag.ROOT) + 1:] if record.name.startswith(diag.ROOT + ".") else record.name
        xbmc.log("[{}] {}: {}".format(ADDON_ID, area.replace(".", "/"), message),
                 _LEVELS.get(record.levelno, xbmc.LOGINFO))


def switches(addon=None):
    """{setting id: bool} for every Diagnostics switch, "Enable all" included."""
    addon = addon or xbmcaddon.Addon()  # fresh: the settings object caches values
    return {switch: addon.getSettingBool(switch) for switch in (diag.ALL, *diag.SWITCHES)}


def apply(addon=None):
    """Read the switches and set each area's level; return the switches."""
    current = switches(addon)
    diag.configure(current, KodiLogHandler())
    return current
