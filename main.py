# -*- coding: utf-8 -*-
"""Entry point.

Called as plugin://plugin.video.ytdlpcast/?video_id=ID&seek=SECONDS -- the same
shape TubeCast already uses for the YouTube add-on, so the fork only has to
change the plugin id in the URL it builds.
"""

import sys
import urllib.parse

import xbmc
import xbmcaddon
import xbmcgui
import xbmcplugin

from resources.lib import resolver

ADDON = xbmcaddon.Addon()
ADDON_ID = ADDON.getAddonInfo("id")
ADDON_NAME = ADDON.getAddonInfo("name")
HANDLE = int(sys.argv[1])


def log(message, level=xbmc.LOGINFO):
    xbmc.log("[{}] {}".format(ADDON_ID, message), level)


def fail(message_id):
    """Tell Kodi the item is unplayable, and say why on screen.

    Resolving to False matters: TubeCast waits on the player and would otherwise
    sit there with the phone showing a video that never starts.
    """
    log(ADDON.getLocalizedString(message_id), xbmc.LOGERROR)
    xbmcgui.Dialog().notification(
        ADDON_NAME, ADDON.getLocalizedString(message_id), xbmcgui.NOTIFICATION_ERROR
    )
    xbmcplugin.setResolvedUrl(HANDLE, False, xbmcgui.ListItem())


def as_seconds(value):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def main():
    params = dict(urllib.parse.parse_qsl(sys.argv[2].lstrip("?")))
    log("called with {}".format(params))

    video_id = params.get("video_id")
    url = params.get("url") or (resolver.watch_url(video_id) if video_id else None)
    if not url:
        return fail(30010)

    # TubeCast sends "seek"; Tubed calls the same thing "start_offset".
    seek = as_seconds(params.get("seek") or params.get("start_offset"))

    try:
        info = resolver.extract(url)
    except Exception as error:  # noqa: BLE001 - any extractor failure is the same to us
        log("extraction failed: {}".format(error), xbmc.LOGERROR)
        return fail(30011)

    stream, headers = resolver.pick_hls(info)
    adaptive = stream is not None

    if stream is None and ADDON.getSettingBool("allow_progressive"):
        log("no HLS manifest, falling back to a progressive format")
        stream, headers = resolver.pick_progressive(info)

    if stream is None:
        return fail(30012)

    item = xbmcgui.ListItem(path=stream)
    item.setContentLookup(False)

    if adaptive:
        item.setMimeType(resolver.HLS_MIME)
        item.setProperty("inputstream", "inputstream.adaptive")
        encoded = resolver.encode_headers(headers)
        if encoded:
            item.setProperty("inputstream.adaptive.manifest_headers", encoded)
            item.setProperty("inputstream.adaptive.stream_headers", encoded)
        # ISA 21 detects the type from the mime type; the explicit property is
        # only there for older builds that still want to be told.
        if ADDON.getSettingBool("legacy_manifest_type"):
            item.setProperty("inputstream.adaptive.manifest_type", "hls")

    tag = item.getVideoInfoTag()
    tag.setTitle(info.get("title") or "")
    tag.setPlot(info.get("description") or "")
    duration = as_seconds(info.get("duration"))
    tag.setDuration(duration)

    thumbnail = info.get("thumbnail")
    if thumbnail:
        item.setArt({"thumb": thumbnail, "icon": thumbnail})

    # Resuming needs both halves; Kodi ignores ResumeTime on its own.
    if seek > 0 and duration > 0:
        item.setProperty("ResumeTime", str(seek))
        item.setProperty("TotalTime", str(duration))

    log("playing {} stream, seek={}s".format("adaptive" if adaptive else "progressive", seek))
    xbmcplugin.setResolvedUrl(HANDLE, True, item)


if __name__ == "__main__":
    main()
