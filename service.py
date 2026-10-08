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
import platform
import sys
import threading
import time

import xbmc
import xbmcaddon

from resources.lib import (cast_kodi, diag, kodilog, manifest_server, paths, playback_diag, web_kodi,
                           ytdlp_info, ytdlp_loader)

ADDON_ID = xbmcaddon.Addon().getAddonInfo("id")

# Give Kodi time to finish starting before the first network request.
FIRST_CHECK_DELAY = 60
CHECK_INTERVAL = 24 * 60 * 60

service_log = diag.logger("service")


def log(message, level=xbmc.LOGINFO):
    """The lines logged whatever the Diagnostics switches say: summaries, warnings, errors."""
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
    service_log.info("manifest server listening on %s", server.base_url)
    return server


def player_diagnostics_wanted(switches):
    return bool(switches.get(diag.ALL) or switches.get("diag_player"))


class Service(xbmc.Monitor):
    def __init__(self):
        super().__init__()
        started = time.monotonic()
        self.switches = kodilog.apply()
        described = ytdlp_loader.describe(paths.ytdlp_directory(), paths.bundled_ytdlp())
        log("service started: {} {}, Kodi {}, Python {}, {}, yt-dlp {} ({})".format(
            ADDON_ID, xbmcaddon.Addon().getAddonInfo("version"), xbmc.getInfoLabel("System.BuildVersion"),
            platform.python_version(), sys.platform, described.get("version"), described.get("source")))
        ytdlp_info.refresh()
        self.root = paths.manifest_directory()
        # Rewritten manifests, kept originals and downloaded subtitles are
        # per-playback scratch; anything left from a previous run is stale.
        removed = 0
        for pattern in ("*.m3u8", "*.srt", "*.original.txt", os.path.join("cache", "*.json")):
            for stale in glob.glob(os.path.join(self.root, pattern)):
                try:
                    os.remove(stale)
                    removed += 1
                except OSError:
                    pass
        service_log.info("removed %d stale file(s) from %s", removed, self.root)
        self.port = configured_port()
        self.server = start_server(self.root, self.port)
        self.cast_settings = cast_kodi.read_settings()
        self.cast = cast_kodi.start(self.cast_settings)
        self.player_diag = playback_diag.start(player_diagnostics_wanted(self.switches))
        self.web_settings = web_kodi.read_settings()
        self.web = web_kodi.start(self.web_settings)
        service_log.info("started in %.1f s; first yt-dlp check in %d s", time.monotonic() - started,
                         FIRST_CHECK_DELAY)

    def onSettingsChanged(self):
        switches = kodilog.apply()
        if switches != self.switches:
            service_log.info("diagnostics now: %s",
                             ", ".join(name for name, on in sorted(switches.items()) if on) or "all off")
            if player_diagnostics_wanted(switches) != player_diagnostics_wanted(self.switches):
                playback_diag.stop(self.player_diag)
                self.player_diag = playback_diag.start(player_diagnostics_wanted(switches))
            self.switches = switches

        port = configured_port()
        if port != self.port:
            log("manifest server port changed {} -> {}, restarting".format(self.port, port))
            if self.server:
                self.server.stop()
            self.port = port
            self.server = start_server(self.root, port)

        settings = cast_kodi.read_settings()
        if settings != self.cast_settings:
            log("cast settings changed, restarting the receiver")
            self.cast_settings = settings
            cast_kodi.stop(self.cast)
            self.cast = cast_kodi.start(settings)

        web = web_kodi.read_settings()
        if (web["enabled"], web["port"]) != (self.web_settings["enabled"], self.web_settings["port"]) or (
                web["enabled"] and not self.web):
            if web != self.web_settings:
                log("web interface settings changed, restarting it")
            web_kodi.stop(self.web)
            self.web = web_kodi.start(web)
        elif self.web and self.web.auth_outdated(web):
            self.web.reload_auth(web)
        self.web_settings = web

    def check_ytdlp(self):
        addon = xbmcaddon.Addon()  # fresh: settings may have changed since start
        if not addon.getSettingBool("ytdlp_auto_update"):
            service_log.info("automatic yt-dlp updates are switched off")
            return
        channel = ytdlp_loader.SETTING_CHANNELS[addon.getSettingInt("ytdlp_channel")]
        started = time.time()
        try:
            result = ytdlp_loader.update(paths.ytdlp_directory(), channel)
        except Exception as error:  # noqa: BLE001 - the service must outlive any update
            log("yt-dlp update crashed ({}: {})".format(type(error).__name__, error), xbmc.LOGERROR)
            return
        finally:
            ytdlp_info.refresh()
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
            service_log.info("next yt-dlp check in %d h", CHECK_INTERVAL // 3600)
        stopping = time.monotonic()
        cast_kodi.stop(self.cast)
        web_kodi.stop(self.web)
        playback_diag.stop(self.player_diag)
        if self.server:
            self.server.stop()
            service_log.info("manifest server stopped")
        service_log.info("stopped in %.1f s; threads still alive: %s", time.monotonic() - stopping,
                         ", ".join(t.name for t in threading.enumerate() if t is not threading.current_thread())
                         or "none")


if __name__ == "__main__":
    Service().run()
