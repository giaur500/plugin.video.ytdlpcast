# -*- coding: utf-8 -*-
"""Python 3.11 state that outlives Kodi's sub-interpreters, and how to live with it.

Kodi runs every plugin call in a sub-interpreter of its own and ends it when
the call returns. Python 3.11 keeps a few things process-wide that one
interpreter sets and the next ones still use after it has gone; 3.12 fixed
both known to hit yt-dlp:

- \\N{} escapes: the decoder's pointer dangles and Kodi crashes. yt-dlp's
  sources are rewritten without them (ytdlp_loader).
- datetime.datetime.strptime: Modules/_datetimemodule.c keeps the _strptime
  module of the first interpreter that called it in a static variable. Ending
  that interpreter clears the module (every name in it becomes None), and from
  then on every strptime anywhere in Kodi raises "TypeError: 'NoneType' object
  is not callable" -- until Kodi restarts. yt-dlp calls it for every video with
  an upload date (its date-range check), so the first resolve after a restart
  works and every later one fails. Unless a long-lived interpreter happened to
  call strptime first, which is why some boxes never see it.

The service pins strptime to its own interpreter, which lives as long as Kodi;
the plugin repairs it within its own interpreter when someone else's ended one
got there first. No xbmc import: scripts/test-subinterp.py runs this anywhere.
"""

import datetime


def strptime_works():
    try:
        datetime.datetime.strptime("2000", "%Y")
        return True
    except TypeError:
        return False


def pin_strptime():
    """Make this interpreter's _strptime the one datetime keeps, if nobody's is yet.

    For the service, whose interpreter lives as long as Kodi. False when it is
    already broken: an interpreter that has ended got there first.
    """
    return strptime_works()


def repair_strptime():
    """Give this interpreter a datetime.datetime whose strptime works; True when it was broken.

    The replacement is a subclass whose strptime does what the C one does,
    with this interpreter's own _strptime -- %f and %z included. The datetime
    module is per interpreter, so nothing outside this plugin call sees it.
    Call before importing what uses it, so that a "from datetime import
    datetime" gets the subclass too.
    """
    if strptime_works():
        return False
    import _strptime
    # Python 3.14 renamed it, when date and time got a strptime of their own.
    parse = getattr(_strptime, "_strptime_datetime", None) or _strptime._strptime_datetime_datetime

    class RepairedDatetime(datetime.datetime):
        __slots__ = ()

        @classmethod
        def strptime(cls, date_string, format):  # noqa: A002 - datetime's own signature
            return parse(cls, date_string, format)

    RepairedDatetime.__name__ = RepairedDatetime.__qualname__ = "datetime"
    RepairedDatetime.__module__ = "datetime"
    datetime.datetime = RepairedDatetime
    return True
