# -*- coding: utf-8 -*-
"""The one place that knows where the add-on keeps its files.

Shared by the service and the plugin, so the two can never disagree about a
path.
"""

import os

import xbmcaddon
import xbmcvfs


def manifest_directory():
    """Per-playback scratch: rewritten manifests and subtitles. Kodi may wipe it."""
    addon_id = xbmcaddon.Addon().getAddonInfo("id")
    path = os.path.join(xbmcvfs.translatePath("special://temp/"), addon_id)
    xbmcvfs.mkdirs(path)
    return path


def ytdlp_directory():
    """Downloaded yt-dlp releases. Must survive restarts, hence addon_data, not temp."""
    profile = xbmcvfs.translatePath(xbmcaddon.Addon().getAddonInfo("profile"))
    path = os.path.join(profile, "ytdlp")
    xbmcvfs.mkdirs(path)
    return path


def bundled_ytdlp():
    """The copy shipped inside the add-on, used until a download succeeds."""
    addon_path = xbmcvfs.translatePath(xbmcaddon.Addon().getAddonInfo("path"))
    return os.path.join(addon_path, "resources", "vendor", "yt-dlp.zip")


def cast_state_file():
    """The cast identity (device id, screen, lounge token). Must survive restarts."""
    profile = xbmcvfs.translatePath(xbmcaddon.Addon().getAddonInfo("profile"))
    xbmcvfs.mkdirs(profile)
    return os.path.join(profile, "cast.json")
