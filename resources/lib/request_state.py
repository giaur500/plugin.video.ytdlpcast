# -*- coding: utf-8 -*-
"""How a playback request went, passed from the plugin back to whoever asked.

The cast receiver and the web interface both start playback through the
plugin, which runs in a separate interpreter and exits. Each request carries a
nonce (?req=…); the plugin leaves its outcome in a window property named after
that nonce, so two requests in flight never overwrite each other:

    "queued: N"   the web's play_url action put N items in Kodi's playlist
    "ok"          the item resolved and Kodi was handed a stream
    "failed: …"   it did not, with the reason (yt-dlp's message where there is one)
"""

import uuid

from .kodiutil import ADDON_ID, HOME


def new_nonce():
    return uuid.uuid4().hex[:12]


def _key(nonce):
    return "{}.request.{}".format(ADDON_ID, nonce)


def report(nonce, result):
    if nonce:
        HOME.setProperty(_key(nonce), result)


def peek(nonce):
    """The outcome so far, or "" while nothing is known."""
    return HOME.getProperty(_key(nonce)) if nonce else ""


def take(nonce):
    """The outcome, removed: for a requester that acts on it once."""
    value = peek(nonce)
    if value:
        HOME.clearProperty(_key(nonce))
    return value
