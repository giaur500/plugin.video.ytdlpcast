# -*- coding: utf-8 -*-
"""The web interface's HTTP server: a page for any browser on the network.

Paste a link to play or queue, watch what plays, seek, pause, stop, read the
log. Kodi itself is behind a backend object (web_kodi.KodiBackend), so this
module has no xbmc import and scripts/test-web.py runs it on a desktop.

Security, as the user chose it: open to the local network by default, with an
optional PIN. Either way, a request that changes something must be JSON and,
when the browser says where it comes from (Origin), come from this page --
so a web page open in some browser on the network cannot make Kodi play
anything: a cross-site form cannot send JSON without a preflight, and no
preflight is ever answered.
"""

import hashlib
import hmac
import json
import os
import re
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler

from . import diag, httpserve

log = diag.logger("web")

STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
}
COOKIE = "ytdlpcast_session"
MAX_BODY = 16 * 1024
LOCKOUT_FAILURES = 5
LOCKOUT_SECONDS = 60


class LogTail:
    """kodi.log, read on from an offset, cut down to this add-on's lines and ISA's.

    Kodi starts every entry with a timestamp; a line without one (a traceback,
    a multi-line message) continues the entry above and is kept with it.
    Kodi starts a fresh file on every launch, so an offset beyond the end means
    the log was rotated: reading starts again near the end of the new one.
    """

    MARKERS = ("[plugin.video.ytdlpcast]", "AddOnLog: inputstream.adaptive:")
    ENTRY = re.compile(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d")
    START_BYTES = 128 * 1024
    STEP_BYTES = 512 * 1024

    def __init__(self, path, markers=MARKERS):
        self.path = path
        self.markers = markers

    def read(self, offset=None):
        """{"lines": [...], "offset": next offset, "reset": rotated or first read}."""
        try:
            size = os.path.getsize(self.path)
        except OSError:
            return {"lines": [], "offset": 0, "reset": True}
        reset = offset is None or offset < 0 or offset > size
        start = max(0, size - self.START_BYTES) if reset else offset
        with open(self.path, "rb") as handle:
            handle.seek(start)
            data = handle.read(min(size - start, self.STEP_BYTES))
        cut = data.rfind(b"\n")
        if cut < 0:
            return {"lines": [], "offset": start, "reset": reset}
        data = data[:cut + 1]
        lines = data.decode("utf-8", "replace").splitlines()
        if reset and start > 0:
            lines = lines[1:]  # began mid-line
        kept, keep = [], False
        for line in lines:
            if self.ENTRY.match(line):
                keep = any(marker in line for marker in self.markers)
            if keep:
                kept.append(line)
        return {"lines": kept, "offset": start + len(data), "reset": reset}


def _same(given, expected):
    """Constant-time comparison that also takes non-ASCII input (str would raise TypeError)."""
    return hmac.compare_digest(given.encode("utf-8"), expected.encode("utf-8"))


class Auth:
    """The optional PIN. A browser that entered it gets a cookie derived from a
    secret; a new PIN comes with a new secret, which signs every browser out."""

    def __init__(self, pin=None, secret=""):
        self.pin = pin or None
        self.secret = secret or ""
        self._failures = {}
        self._lock = threading.Lock()

    @property
    def required(self):
        return self.pin is not None

    def session(self):
        return hmac.new(self.secret.encode(), b"ytdlpcast-session", hashlib.sha256).hexdigest()

    def allows(self, cookie_header):
        if not self.required:
            return True
        for part in (cookie_header or "").split(";"):
            name, _, value = part.strip().partition("=")
            if name == COOKIE and _same(value, self.session()):
                return True
        return False

    def login(self, address, pin):
        """(ok, seconds to wait): five wrong PINs from one address lock it for a minute."""
        if not self.required:
            return True, 0
        now = time.monotonic()
        with self._lock:
            count, until = self._failures.get(address, (0, 0))
            if until > now:
                return False, int(until - now) + 1
            if _same(str(pin or ""), self.pin):
                self._failures.pop(address, None)
                return True, 0
            count += 1
            self._failures[address] = (0, now + LOCKOUT_SECONDS) if count >= LOCKOUT_FAILURES else (count, 0)
            return False, LOCKOUT_SECONDS if count >= LOCKOUT_FAILURES else 0


class _Handler(BaseHTTPRequestHandler):
    server_version = "plugin.video.ytdlpcast"
    protocol_version = "HTTP/1.1"

    # -- plumbing -------------------------------------------------------------

    def _send(self, status, body, content_type="application/json; charset=utf-8", headers=None):
        data = body if isinstance(body, bytes) else (
            json.dumps(body, ensure_ascii=False).encode("utf-8") if not isinstance(body, str) else body.encode("utf-8"))
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def log_message(self, format, *args):  # noqa: A002 - signature fixed by the base class
        if diag.enabled("web"):
            log.info("%s %s", self.address_string(), format % args)

    def _route(self):
        parts = urllib.parse.urlsplit(self.path)
        return parts.path, dict(urllib.parse.parse_qsl(parts.query))

    def _authorized(self):
        return self.server.auth.allows(self.headers.get("Cookie"))

    def _same_origin(self):
        origin = self.headers.get("Origin")
        return origin is None or origin == "http://" + (self.headers.get("Host") or "")

    def _json_body(self):
        """The request's JSON object, or None: not JSON, too large, malformed."""
        length = httpserve.content_length(self.headers)
        if (length is None or length > MAX_BODY
                or not (self.headers.get("Content-Type") or "").startswith("application/json")):
            self.close_connection = True  # the unread body must not be parsed as the next request
            return None
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return None
        return body if isinstance(body, dict) else None

    def _call(self, function, *args):
        try:
            return self._send(200, function(*args) or {})
        except ValueError as error:
            return self._send(400, {"error": str(error)})
        except Exception as error:  # noqa: BLE001 - a broken call must not kill the server
            log.warning("%s %s failed: %s: %s", self.command, self.path, type(error).__name__, error)
            return self._send(500, {"error": "{}: {}".format(type(error).__name__, error)})

    # -- methods ----------------------------------------------------------------

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path, query = self._route()
        if path in STATIC:
            name, content_type = STATIC[path]
            try:
                with open(os.path.join(self.server.root, name), "rb") as handle:
                    return self._send(200, handle.read(), content_type)
            except OSError:
                return self._send(404, {"error": "missing"})
        if path == "/api/auth":
            return self._send(200, {"pin_required": self.server.auth.required, "authorized": self._authorized()})
        if not path.startswith("/api/"):
            return self._send(404, {"error": "not found"})
        if not self._authorized():
            return self._send(401, {"error": "pin"})
        backend = self.server.backend
        if path == "/api/status":
            return self._call(backend.status, query.get("request"))
        if path == "/api/queue":
            return self._call(backend.queue)
        if path == "/api/log":
            offset = int(query["offset"]) if query.get("offset", "").lstrip("-").isdigit() else None
            return self._call(self.server.log_tail.read, offset)
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        path, _ = self._route()
        if not self._same_origin():
            log.warning("%s: POST %s from a foreign page (%s) refused", self.address_string(), path,
                        self.headers.get("Origin"))
            return self._send(403, {"error": "foreign origin"})
        body = self._json_body()
        if body is None:
            return self._send(415, {"error": "JSON expected"})
        if path == "/api/login":
            ok, wait = self.server.auth.login(self.client_address[0], body.get("pin"))
            if not ok:
                log.warning("%s: wrong PIN%s", self.address_string(), ", locked for {} s".format(wait) if wait else "")
                return self._send(403, {"error": "pin", "retry_after": wait})
            cookie = "{}={}; Path=/; HttpOnly; SameSite=Strict; Max-Age=31536000".format(
                COOKIE, self.server.auth.session())
            return self._send(200, {"ok": True}, headers={"Set-Cookie": cookie})
        if not self._authorized():
            return self._send(401, {"error": "pin"})
        backend = self.server.backend
        if path == "/api/play":
            return self._call(backend.play, body.get("url"), body.get("mode") or "now")
        if path == "/api/control":
            return self._call(backend.control, body.get("action"), body.get("seconds"))
        if path == "/api/queue/goto":
            return self._call(backend.goto, body.get("index"))
        return self._send(404, {"error": "not found"})


class WebServer(httpserve.ServerThread):
    """The page and its API on the network; the PIN can be swapped while serving."""

    def __init__(self, backend, root, log_path, port, host="0.0.0.0", auth=None):
        self.auth = auth or Auth()
        super().__init__(_Handler, host, port, "ytdlpcast-web", backend=backend, root=root,
                         log_tail=LogTail(log_path), auth=self.auth)

    def set_auth(self, auth):
        self.auth = auth
        self.set(auth=auth)
