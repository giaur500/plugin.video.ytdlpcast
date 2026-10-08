# -*- coding: utf-8 -*-
"""Kodi service: the cast receiver, the manifest HTTP server and the yt-dlp updater.

The plugin script that resolves a video exits as soon as it has handed the
ListItem to Kodi, so it cannot host anything long-lived. This service does:

- the cast receiver (resources/lib/cast_kodi.py), so the YouTube app on a phone
  can find Kodi and send videos to it; restarted when its settings change;
- the manifest server, started at Kodi launch, restarted when the port setting
  changes. If the port cannot be bound the service logs why; the plugin notices
  the missing server and plays manifests as published instead;
- the yt-dlp updater: a first check shortly after Kodi starts, then one a day.
  Updating happens here, never during playback, so a download or the validation
  of a new release cannot delay a cast.
"""

import glob
import os
import time

import xbmc
import xbmcaddon

from resources.lib import cast_kodi, manifest_server, paths, ytdlp_loader

ADDON_ID = xbmcaddon.Addon().getAddonInfo("id")

# Give Kodi time to finish starting before the first network request.
FIRST_CHECK_DELAY = 60
CHECK_INTERVAL = 24 * 60 * 60


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
        # Rewritten manifests and downloaded subtitles are per-playback scratch;
        # anything left from a previous run is stale.
        for pattern in ("*.m3u8", "*.srt"):
            for stale in glob.glob(os.path.join(self.root, pattern)):
                try:
                    os.remove(stale)
                except OSError:
                    pass
        self.port = configured_port()
        self.server = start_server(self.root, self.port)
        self.cast_settings = cast_kodi.read_settings()
        self.cast = cast_kodi.start(self.cast_settings)

    def onSettingsChanged(self):
        port = configured_port()
        if port != self.port:
            log("manifest server port changed {} -> {}, restarting".format(self.port, port))
            if self.server:
                self.server.stop()
            self.port = port
            self.server = start_server(self.root, port)

        settings = cast_kodi.read_settings()
        if settings == self.cast_settings:
            return
        restart = any(settings[key] != self.cast_settings[key] for key in ("enabled", "discovery", "name"))
        self.cast_settings = settings
        if restart:
            log("cast settings changed, restarting the receiver")
            cast_kodi.stop(self.cast)
            self.cast = cast_kodi.start(settings)
        else:
            cast_kodi.setup_logging(settings["verbose"])

    def check_ytdlp(self):
        addon = xbmcaddon.Addon()  # fresh: settings may have changed since start
        if not addon.getSettingBool("ytdlp_auto_update"):
            return
        channel = ytdlp_loader.SETTING_CHANNELS[addon.getSettingInt("ytdlp_channel")]
        started = time.time()
        try:
            result = ytdlp_loader.update(paths.ytdlp_directory(), channel)
        except Exception as error:  # noqa: BLE001 - the service must outlive any update
            log("yt-dlp update crashed ({}: {})".format(type(error).__name__, error), xbmc.LOGERROR)
            return
        took = time.time() - started
        if result.status == ytdlp_loader.REJECTED:
            # Typically a release that needs a newer Python than Kodi ships.
            log("yt-dlp {} release rejected, staying on the current version: {}".format(
                channel, result.reason), xbmc.LOGWARNING)
        elif result.status == ytdlp_loader.FAILED:
            log("yt-dlp update on {} failed: {}".format(channel, result.reason), xbmc.LOGWARNING)
        else:
            log("yt-dlp update on {} took {:.1f}s: {}".format(channel, took, result))

    def run(self):
        wait = FIRST_CHECK_DELAY
        while not self.waitForAbort(wait):
            self.check_ytdlp()
            wait = CHECK_INTERVAL
        cast_kodi.stop(self.cast)
        if self.server:
            self.server.stop()
            log("manifest server stopped")


if __name__ == "__main__":
    Service().run()
