# -*- coding: utf-8 -*-
"""Local discovery: how the YouTube app on the same network finds Kodi.

The app multicasts an SSDP M-SEARCH for DIAL devices; we answer with the URL of
a device description, the app reads it, asks for the YouTube "application" and
launches it with a POST carrying a pairing code. That code is all the app hands
over -- the session itself runs through YouTube (cast_lounge).

Ported from TubeCast by enen92, whose SSDP part comes from Leapcast (both MIT,
see LICENSE.txt). The bottle web framework is gone in favour of http.server, the
DIAL server no longer wakes ten times a second to poll its socket, SSDP no
longer starts a thread for every datagram on the network, and its socket is
actually closed on shutdown.

No xbmc import: scripts/test-cast-discovery.py exercises both servers.
"""

import socket
import struct
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from xml.sax.saxutils import escape

from . import diag

ssdp_log = diag.logger("cast.ssdp")
dial_log = diag.logger("cast.dial")
# Every datagram and every header: for "the phone does not see Kodi".
detail = diag.logger("cast.discovery")

SSDP_ADDRESS = "239.255.255.250"
SSDP_PORT = 1900
DIAL_SERVICE = "urn:dial-multiscreen-org:service:dial:1"
DESCRIPTION_PATH = "/ssdp/device-desc.xml"
APP_PATH = "/apps/YouTube"
RUN_PATH = APP_PATH + "/run"

SSDP_RESPONSE = (
    "HTTP/1.1 200 OK\r\n"
    "LOCATION: http://{ip}:{port}" + DESCRIPTION_PATH + "\r\n"
    "CACHE-CONTROL: max-age=1800\r\n"
    "EXT: \r\n"
    "SERVER: UPnP/1.0\r\n"
    "BOOTID.UPNP.ORG: 1\r\n"
    "USN: uuid:{uuid}\r\n"
    "ST: " + DIAL_SERVICE + "\r\n"
    "\r\n"
)

DEVICE_DESCRIPTION = """<?xml version="1.0" encoding="utf-8"?>
<root xmlns="urn:schemas-upnp-org:device-1-0" xmlns:r="urn:restful-tv-org:schemas:upnp-dd">
    <specVersion>
        <major>1</major>
        <minor>0</minor>
    </specVersion>
    <URLBase>{base}</URLBase>
    <device>
        <deviceType>urn:dial-multiscreen-org:device:dial:1</deviceType>
        <friendlyName>{name}</friendlyName>
        <manufacturer>Kodi</manufacturer>
        <modelName>yt-dlp cast</modelName>
        <UDN>uuid:{uuid}</UDN>
    </device>
</root>"""

APP_STOPPED = """<service xmlns="urn:dial-multiscreen-org:schemas:dial">
    <name>YouTube</name>
    <options allowStop="true"/>
    <state>stopped</state>
</service>"""

APP_RUNNING = """<service xmlns="urn:dial-multiscreen-org:schemas:dial">
    <name>YouTube</name>
    <options allowStop="true"/>
    <servicedata xmlns="urn:chrome.google.com:cast">
        <connectionSvcURL></connectionSvcURL>
        <protocols>
            <protocol>ramp</protocol>
        </protocols>
    </servicedata>
    <state>running</state>
    <activity-status xmlns="urn:chrome.google.com:cast">
        <description>YouTube Receiver</description>
    </activity-status>
    <link rel="run" href="run"/>
</service>"""


def local_address_for(peer):
    """Our address on the route to peer -- the one the phone can reach us at."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.connect(peer)  # UDP: no packet is sent, only the route is chosen
        return probe.getsockname()[0]


class SsdpResponder:
    """Answers DIAL M-SEARCHes on the SSDP multicast group, in one thread.

    SO_REUSEADDR/SO_REUSEPORT let it share port 1900 with Kodi's own UPnP and
    with TubeCast, should both be installed.
    """

    LOG_EVERY = 60  # seconds between log lines per asking address

    def __init__(self, uuid, dial_port, port=SSDP_PORT):
        self.uuid = uuid
        self.dial_port = dial_port
        self.port = port
        self._sock = None
        self._thread = None
        self._stopping = threading.Event()
        self._logged = {}

    def start(self):
        """Bind and serve in a daemon thread. Raises OSError if binding fails."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if hasattr(socket, "SO_REUSEPORT"):
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            sock.bind(("", self.port))
            try:
                membership = struct.pack("4sI", socket.inet_aton(SSDP_ADDRESS), socket.INADDR_ANY)
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, membership)
            except OSError as error:
                # No multicast route (no network yet, odd interfaces): unicast
                # searches still work, and the TV code does not need SSDP at all.
                ssdp_log.warning("could not join %s (%s); only direct searches will be answered",
                                 SSDP_ADDRESS, error)
            sock.settimeout(1.0)
        except BaseException:
            sock.close()
            raise
        self._sock = sock
        self._thread = threading.Thread(target=self._serve, name="ytdlpcast-ssdp", daemon=True)
        self._thread.start()
        ssdp_log.info("answering DIAL searches on port %d, device uuid %s", self.port, self.uuid)

    def _serve(self):
        while not self._stopping.is_set():
            try:
                datagram, peer = self._sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                if not self._stopping.is_set():
                    ssdp_log.exception("receiving failed")
                break
            try:
                self._handle(datagram, peer)
            except Exception:  # noqa: BLE001 - one odd datagram must not end discovery
                ssdp_log.exception("answering %s failed", peer[0])

    def _handle(self, datagram, peer):
        if diag.enabled("cast.discovery"):
            lines = datagram.decode("utf-8", "replace").splitlines()
            target = next((line for line in lines if line.upper().startswith("ST:")), "no ST")
            agent = next((line for line in lines if line.upper().startswith(("USER-AGENT:", "SERVER:"))), "")
            detail.info("datagram from %s:%d: %s | %s %s", peer[0], peer[1], lines[0] if lines else "?",
                        target, agent)
        if not datagram.startswith(b"M-SEARCH") or DIAL_SERVICE.encode() not in datagram:
            return
        ip = local_address_for(peer)
        self._sock.sendto(SSDP_RESPONSE.format(ip=ip, port=self.dial_port, uuid=self.uuid).encode(), peer)
        now = time.monotonic()
        if now - self._logged.get(peer[0], -self.LOG_EVERY) >= self.LOG_EVERY:
            self._logged[peer[0]] = now
            ssdp_log.info("DIAL search from %s answered with http://%s:%d%s",
                          peer[0], ip, self.dial_port, DESCRIPTION_PATH)

    def stop(self):
        self._stopping.set()
        if self._sock is not None:
            try:
                membership = struct.pack("4sI", socket.inet_aton(SSDP_ADDRESS), socket.INADDR_ANY)
                self._sock.setsockopt(socket.IPPROTO_IP, socket.IP_DROP_MEMBERSHIP, membership)
            except OSError:
                pass
            self._sock.close()
        if self._thread is not None:
            self._thread.join(timeout=3)
        self._sock = self._thread = None
        ssdp_log.info("stopped")


class _DialHandler(BaseHTTPRequestHandler):
    server_version = "plugin.video.ytdlpcast"
    protocol_version = "HTTP/1.1"

    def _detail(self):
        if diag.enabled("cast.discovery"):
            detail.info("%s %s %s headers: %s", self.client_address[0], self.command, self.path,
                        dict(self.headers.items()))

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        dial_log.info("%s GET %s", self.client_address[0], path)
        self._detail()
        if path == DESCRIPTION_PATH:
            host = self.headers.get("Host") or "{}:{}".format(*self.server.server_address)
            body = DEVICE_DESCRIPTION.format(base=escape("http://" + host), name=escape(self.server.app.name),
                                             uuid=self.server.app.uuid)
            return self._respond(200, body, {"Application-URL": "http://{}/apps".format(host)})
        if path == APP_PATH:
            return self._respond(200, APP_RUNNING if self.server.app.is_running() else APP_STOPPED)
        self._respond(404, "not found", content_type="text/plain")

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode("utf-8", "replace") if length else ""
        form = dict(urllib.parse.parse_qsl(body))
        dial_log.info("%s POST %s %s", self.client_address[0], path,
                      {k: v for k, v in form.items() if k != "pairingCode"})
        self._detail()
        if path != APP_PATH:
            return self._respond(404, "not found", content_type="text/plain")
        if not self.server.app.launch(form):
            return self._respond(503, "not ready", content_type="text/plain")
        host = self.headers.get("Host") or "{}:{}".format(*self.server.server_address)
        self._respond(201, "", {"Location": "http://{}{}".format(host, RUN_PATH)}, content_type="text/plain")

    def do_DELETE(self):
        path = self.path.split("?", 1)[0]
        dial_log.info("%s DELETE %s", self.client_address[0], path)
        self._detail()
        if path != RUN_PATH:
            return self._respond(404, "not found", content_type="text/plain")
        self.server.app.stop()
        self._respond(200, "")

    def _respond(self, status, body, headers=None, content_type="application/xml"):
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache, must-revalidate, no-store")
        self.send_header("Access-Control-Allow-Method", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Expose-Headers", "Location")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format, *args):  # noqa: A002 - signature fixed by the base class
        pass  # requests are logged above, through the add-on's logger


class DialServer:
    """The DIAL HTTP endpoint, on all interfaces and a port the system picks.

    app supplies name, uuid, is_running(), launch(form) -> bool and stop().
    """

    def __init__(self, app, host="0.0.0.0", port=0):
        self.app = app
        self.host = host
        self.port = port
        self._httpd = None
        self._thread = None

    def start(self):
        """Bind and serve in a daemon thread; return the port. Raises OSError."""
        self._httpd = ThreadingHTTPServer((self.host, self.port), _DialHandler)
        self._httpd.daemon_threads = True
        self._httpd.app = self.app
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever, name="ytdlpcast-dial", daemon=True)
        self._thread.start()
        dial_log.info("DIAL server listening on %s:%d as \"%s\"", self.host, self.port, self.app.name)
        return self.port

    def stop(self):
        if self._httpd is None:
            return
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=3)
        self._httpd = self._thread = None
        dial_log.info("stopped")
