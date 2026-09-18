# -*- coding: utf-8 -*-
"""A loopback HTTP server that hands rewritten manifests to InputStream Adaptive.

ISA fetches its manifest over HTTP. A local file path is not enough -- Kodi then
falls back to its own demuxer, which takes the first variant of a master
playlist, loses the audio groups and cannot seek. So the rewritten playlist is
served from 127.0.0.1 instead, the same way plugin.video.youtube serves the MPDs
it generates.

No xbmc import here on purpose: scripts/test-server.py exercises this on a
desktop. service.py is the thin Kodi wrapper around it.
"""

import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MANIFEST_MIME = "application/vnd.apple.mpegurl"
MANIFEST_EXTENSION = ".m3u8"
HEALTH_PATH = "/health"


class _Handler(BaseHTTPRequestHandler):
    """Serves exactly one directory of .m3u8 files by basename, nothing else."""

    server_version = "plugin.video.ytdlpcast/1.0"
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        self._serve(send_body=True)

    def do_HEAD(self):
        self._serve(send_body=False)

    def _serve(self, send_body):
        if self.path == HEALTH_PATH:
            return self._respond(200, b"ok", "text/plain", send_body)

        body = self._read_manifest()
        if body is None:
            return self._respond(404, b"not found", "text/plain", send_body)
        self._respond(200, body, MANIFEST_MIME, send_body)

    def _read_manifest(self):
        # Only the last path component counts, so "../" and subdirectories can
        # never reach outside the manifest directory.
        name = os.path.basename(self.path.split("?", 1)[0])
        if not name.endswith(MANIFEST_EXTENSION) or name in ("", MANIFEST_EXTENSION):
            return None
        path = os.path.join(self.server.root, name)
        try:
            with open(path, "rb") as handle:
                return handle.read()
        except OSError:
            return None

    def _respond(self, status, body, content_type, send_body):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        # The same file name is reused for the same video; a cached copy would
        # hand ISA a manifest whose segment URLs have already expired.
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if send_body:
            self.wfile.write(body)

    def log_message(self, format, *args):  # noqa: A002 - signature fixed by the base class
        pass  # Kodi has its own log; the default would write to stderr.


class ManifestServer:
    """Owns the listening socket and the thread that serves it."""

    def __init__(self, root, port, host="127.0.0.1"):
        self.root = root
        self.port = port
        self.host = host
        self._httpd = None
        self._thread = None

    @property
    def base_url(self):
        return "http://{}:{}".format(self.host, self.port)

    def start(self):
        """Bind and serve in a daemon thread. Raises OSError if the port is taken."""
        self._httpd = ThreadingHTTPServer((self.host, self.port), _Handler)
        self._httpd.root = self.root
        self._httpd.daemon_threads = True
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, name="ytdlpcast-manifests", daemon=True)
        self._thread.start()

    def stop(self):
        if self._httpd is None:
            return
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5)
        self._httpd = None
        self._thread = None
