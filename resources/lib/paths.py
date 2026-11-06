# -*- coding: utf-8 -*-
"""The one place that knows where the add-on keeps its files.

Shared by the service and the plugin, so the two can never disagree about a
path.
"""

import os
import tempfile

import xbmcaddon
import xbmcvfs


def manifest_directory():
    """Per-playback scratch: rewritten manifests and subtitles. Kodi may wipe it."""
    addon_id = xbmcaddon.Addon().getAddonInfo("id")
    path = os.path.join(xbmcvfs.translatePath("special://temp/"), addon_id)
    xbmcvfs.mkdirs(path)
    return path


def temp_directory():
    """Where Python's tempfile writes for this add-on. The service empties it at start."""
    path = os.path.join(manifest_directory(), "tmp")
    xbmcvfs.mkdirs(path)
    return path


def use_own_tempdir():
    """Send Python's tempfile to the add-on's own directory; return that directory.

    Left alone, tempfile looks for a writable /tmp, /var/tmp, /usr/tmp or working
    directory. Android gives Kodi none of them ("No usable temporary directory
    found"), and yt-dlp updates itself, so what asks for a temporary file can
    change at any time. Set on every platform: Python then tries none of those
    places, and a box with a /tmp runs the same code as one without. tempfile is
    a Python module, one per interpreter, so the service and every plugin call
    each have to set it.
    """
    tempfile.tempdir = temp_directory()
    return tempfile.tempdir


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
