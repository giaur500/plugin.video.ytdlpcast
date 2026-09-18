# -*- coding: utf-8 -*-
"""Kodi service: keeps the manifest HTTP server up for as long as Kodi runs.

The plugin script that resolves a video exits as soon as it has handed the
ListItem to Kodi, so it cannot host the server itself. This service starts it
at Kodi launch, restarts it when the port setting changes, and stops it on
shutdown. If the port cannot be bound the service logs why and ends; the plugin
notices the missing server and plays manifests as published instead.
"""

import glob
import os

import xbmc
import xbmcaddon

from resources.lib import manifest_server, paths

ADDON_ID = xbmcaddon.Addon().getAddonInfo("id")



def log(message, level=xbmc.LOGINFO):
    xbmc.log("[{}] {}".format(ADDON_ID, message), level)


def configured_port():
    # A fresh Addon() each time: the settings object caches values otherwise.
    return xbmcaddon.Addon().getSettingInt("http_port")


def start_server(root, port):
    server = manifest_server.ManifestServer(root, port)
    try:
        server.start()
    except OSError as error:
        log("manifest server could not bind 127.0.0.1:{} ({}); manifests will play as published"
            .format(port, error), xbmc.LOGERROR)
        return None
    log("manifest server listening on {}".format(server.base_url))
    return server



class Service(xbmc.Monitor):
    def __init__(self):
        super().__init__()
        self.root = paths.manifest_directory()
        for stale in glob.glob(os.path.join(self.root, "*.m3u8")):
            try:
                os.remove(stale)
            except OSError:
                pass
        self.port = configured_port()
        self.server = start_server(self.root, self.port)

    def onSettingsChanged(self):
        port = configured_port()
        if port == self.port:
            return
        log("manifest server port changed {} -> {}, restarting".format(self.port, port))
        if self.server:
            self.server.stop()
        self.port = port
        self.server = start_server(self.root, port)

    def run(self):
        self.waitForAbort()
        if self.server:
            self.server.stop()
            log("manifest server stopped")


if __name__ == "__main__":
    Service().run()
