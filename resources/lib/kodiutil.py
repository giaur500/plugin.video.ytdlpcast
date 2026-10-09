# -*- coding: utf-8 -*-
"""Kodi plumbing shared by the plugin and the service.

The add-on's id, the home window -- its properties are what the plugin and the
service, separate interpreters, both see -- JSON-RPC, the box's address,
InputStream Adaptive's version, and a Player that only queues its callbacks.
"""

import json
import re

import xbmc
import xbmcaddon
import xbmcgui

ADDON_ID = xbmcaddon.Addon().getAddonInfo("id")
HOME = xbmcgui.Window(10000)
ISA_ID = "inputstream.adaptive"


def json_rpc(method, params=None):
    """The result of a JSON-RPC call; RuntimeError when Kodi answers with an error.

    xbmc.executeJSONRPC is safe from any thread.
    """
    reply = json.loads(xbmc.executeJSONRPC(json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}})))
    if "error" in reply:
        raise RuntimeError("{}: {}".format(method, reply["error"].get("message", reply["error"])))
    return reply.get("result")


def network_address():
    """The box's address on the network, or "" while it has none (booting, cable out)."""
    ip = xbmc.getIPAddress()
    return ip if ip and ip not in ("127.0.0.1", "0.0.0.0") else ""


def major(version):
    """20 from "20.3.18" or "20.2 (20.2.0) Git:…"; None when there is no number."""
    match = re.match(r"\s*(\d+)", version or "")
    return int(match.group(1)) if match else None


def inputstream_adaptive():
    """(version, enabled) of InputStream Adaptive; version "" when Kodi does not know it.

    A disabled one is skipped by Kodi without a word: it plays the manifest
    itself, through ffmpeg, in the lowest quality.
    """
    return (xbmc.getInfoLabel("System.AddonVersion({})".format(ISA_ID)),
            xbmc.getCondVisibility("System.AddonIsEnabled({})".format(ISA_ID)))


def isa_needs_manifest_type():
    """True for InputStream Adaptive 20 (Kodi 20 Nexus): it opens no manifest without
    inputstream.adaptive.manifest_type, where 21 detects the type itself.

    The ISA version decides; Kodi's own one when that cannot be read.
    """
    version = major(inputstream_adaptive()[0]) or major(xbmc.getInfoLabel("System.BuildVersion"))
    return version is not None and version < 21


class EventPlayer(xbmc.Player):
    """Only queues what happened, as ("started",), ("paused",)...; its owner decides what it means.

    Kodi delivers Player callbacks solely to the thread that created the
    Player, inside that thread's waitForAbort -- so the owner creates it on its
    own thread and drains the queue in the same loop.
    """

    def __init__(self, events):
        super().__init__()
        self.events = events

    def onAVStarted(self):
        self.events.put(("started",))

    def onPlayBackPaused(self):
        self.events.put(("paused",))

    def onPlayBackResumed(self):
        self.events.put(("resumed",))

    def onPlayBackSeek(self, time, seekOffset):  # noqa: A002, N803 - Kodi's signature
        self.events.put(("seeked",))

    def onPlayBackStopped(self):
        self.events.put(("stopped",))

    def onPlayBackEnded(self):
        self.events.put(("ended",))

    def onPlayBackError(self):
        self.events.put(("error",))
