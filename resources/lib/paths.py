# -*- coding: utf-8 -*-
"""The one place that knows where rewritten manifests live.

Shared by the service that serves the directory and the plugin that writes
into it, so the two can never disagree about the path.
"""

import os

import xbmcaddon
import xbmcvfs


def manifest_directory():
    addon_id = xbmcaddon.Addon().getAddonInfo("id")
    path = os.path.join(xbmcvfs.translatePath("special://temp/"), addon_id)
    xbmcvfs.mkdirs(path)
    return path
