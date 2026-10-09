# -*- coding: utf-8 -*-
"""Small file helpers. No xbmc import."""

import json
import os
import tempfile


def write_json_atomic(path, data, **dump_options):
    """Write data to path as JSON, through a temporary file and one rename.

    A reader -- the plugin while the service writes, another thread -- sees the
    old file or the new one, never half of one; a failed write leaves no
    temporary file behind.
    """
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".", prefix="." + os.path.basename(path) + "-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, **dump_options)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise
