# -*- coding: utf-8 -*-
"""YouTube Lounge API: the screen's identity, its session and the long poll.

Ported from TubeCast by enen92 (MIT, see LICENSE.txt), reworked after
yt-cast-receiver: the screen is persistent instead of being created anew for
every phone, its lounge token is refreshed before it expires, and every new
session starts from a clean slate -- TubeCast kept the previous session's SID
and command counter, so a re-paired phone's commands were dropped as already
seen, and its token refresh never ran at all.

No xbmc import: scripts/test-cast-protocol.py drives this through a fake
transport, scripts/cast-harness.py against YouTube itself.
"""

import codecs
import http.client
import json
import logging
import os
import random
import socket
import ssl
import tempfile
import threading
import time
import urllib.parse
import uuid

from . import cast_protocol

log = logging.getLogger("cast.lounge")

HOST = "www.youtube.com"
GENERATE_SCREEN_ID = "/api/lounge/pairing/generate_screen_id"
GET_LOUNGE_TOKEN = "/api/lounge/pairing/get_lounge_token_batch"
REGISTER_PAIRING_CODE = "/api/lounge/pairing/register_pairing_code"
GET_PAIRING_CODE = "/api/lounge/pairing/get_pairing_code"
BIND = "/api/lounge/bc/bind"

SCREEN_APP = "kodi-ytdlpcast"
USER_AGENT = "plugin.video.ytdlpcast"

# YouTube answers with refreshIntervalInMillis (13 days, the token lives 14).
FALLBACK_REFRESH_MS = 13 * 24 * 3600 * 1000


class LoungeError(Exception):
    """YouTube answered, but not with what we asked for."""

    def __init__(self, what, status=None, body=""):
        super().__init__("{} failed: HTTP {} {}".format(what, status, (body or "").strip()[:200]))
        self.status = status

    @property
    def session_gone(self):
        """4xx: the session or token is no longer valid -- start a new one."""
        return self.status is not None and 400 <= self.status < 500


def mask(secret):
    """Enough of an id or token to tell two apart in a log, not to use it."""
    return (secret[:6] + "…") if secret else "-"


def ssl_context():
    # Kodi on Android has no system CA store Python can read; certifi (a
    # dependency of the add-on) carries one. A desktop without it uses the system's.
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


class Identity:
    """Who this screen is, kept in the add-on profile across restarts.

    device_id is ours (a random UUID made once); screen_id and the lounge token
    come from YouTube. TubeCast used one hard-coded device id for every install
    in the world and a fresh screen per connection, so a phone paired with a TV
    code was forgotten at the next restart.
    """

    FIELDS = ("device_id", "screen_id", "lounge_token", "expiration", "refresh_at", "tubecast_warned")

    def __init__(self, path):
        self.path = path
        self.device_id = None
        self.screen_id = None
        self.lounge_token = None
        self.expiration = 0
        self.refresh_at = 0
        self.tubecast_warned = False

    @classmethod
    def load(cls, path):
        identity = cls(path)
        try:
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
            for field in cls.FIELDS:
                if field in data:
                    setattr(identity, field, data[field])
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError) as error:
            log.warning("identity file %s unreadable (%s), starting a new one", path, error)
        if not identity.device_id:
            identity.device_id = str(uuid.uuid4())
            log.info("new device id %s", identity.device_id)
            identity.save()
        return identity

    def save(self):
        # Write-then-rename, as ytdlp_loader does: the plugin reads this file
        # (TV code) while the service may be writing it.
        directory = os.path.dirname(self.path) or "."
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".cast-")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({field: getattr(self, field) for field in self.FIELDS}, handle, indent=1, sort_keys=True)
        os.replace(tmp, self.path)

    def token_due(self, now_ms=None):
        now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
        return not self.lounge_token or now_ms >= self.refresh_at


class Stream:
    """One open long poll. abort() may be called from any thread."""

    def __init__(self, connection, response):
        self._connection = connection
        self._response = response

    def chunks(self):
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        while True:
            data = self._response.read1(65536)
            if not data:
                return
            yield decoder.decode(data)

    def abort(self):
        sock = self._connection.sock
        if sock is not None:
            try:
                # Wakes the reading thread at once; closing alone would not.
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def close(self):
        self._connection.close()


class HttpTransport:
    """HTTPS to www.youtube.com: one kept-alive connection for requests, one per long poll."""

    def __init__(self, timeout=15, stream_timeout=300):
        self.timeout = timeout
        self.stream_timeout = stream_timeout
        self._context = ssl_context()
        self._connection = None
        self._lock = threading.Lock()

    def _connect(self):
        return http.client.HTTPSConnection(HOST, timeout=self.timeout, context=self._context)

    @staticmethod
    def _target(path, query):
        return path + ("?" + urllib.parse.urlencode(query) if query else "")

    def request(self, method, path, query=None, form=None):
        """(status, text). Retries once: a kept-alive connection may have gone stale."""
        body = urllib.parse.urlencode(form).encode("utf-8") if form is not None else None
        headers = {"User-Agent": USER_AGENT}
        if body is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        with self._lock:
            for attempt in (1, 2):
                connection = self._connection or self._connect()
                try:
                    connection.request(method, self._target(path, query), body=body, headers=headers)
                    response = connection.getresponse()
                    data = response.read()
                except (http.client.HTTPException, OSError):
                    connection.close()
                    self._connection = None
                    if attempt == 2:
                        raise
                    continue
                self._connection = connection
                return response.status, data.decode("utf-8", "replace")

    def open_stream(self, path, query):
        connection = self._connect()
        try:
            connection.request("GET", self._target(path, query), headers={"User-Agent": USER_AGENT})
            response = connection.getresponse()
        except BaseException:
            connection.close()
            raise
        if response.status != 200:
            body = response.read(500).decode("utf-8", "replace")
            connection.close()
            raise LoungeError("long poll", response.status, body)
        # The server holds the poll open and sends a noop now and then; only a
        # silence well beyond that means the connection is dead.
        connection.sock.settimeout(self.stream_timeout)
        return Stream(connection, response)

    def close(self):
        with self._lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None


def generate_screen_id(transport):
    status, text = transport.request("GET", GENERATE_SCREEN_ID)
    if status != 200 or not text.strip():
        raise LoungeError("generate_screen_id", status, text)
    return text.strip()


def get_lounge_token(transport, screen_id):
    status, text = transport.request("POST", GET_LOUNGE_TOKEN, form={"screen_ids": screen_id})
    if status != 200:
        raise LoungeError("get_lounge_token_batch", status, text)
    try:
        token = json.loads(text)["screens"][0]
        token["loungeToken"]  # noqa: B018 - must be there
    except (ValueError, KeyError, IndexError, TypeError):
        raise LoungeError("get_lounge_token_batch", status, text) from None
    return token


def get_pairing_code(transport, identity, name):
    """A TV code for the phone ("Watch on TV" -> "Enter TV code"), as 123-456-789-012."""
    status, text = transport.request("POST", GET_PAIRING_CODE, query={"ctx": "pair"}, form={
        "access_type": "permanent",
        "app": SCREEN_APP,
        "lounge_token": identity.lounge_token,
        "screen_id": identity.screen_id,
        "screen_name": name,
        "device_id": identity.device_id,
    })
    code = text.strip()
    if status != 200 or not code.isdigit():
        raise LoungeError("get_pairing_code", status, text)
    return "-".join(code[i:i + 3] for i in range(0, len(code), 3))


class LoungeSession:
    """The screen's connection to YouTube's lounge: one session at a time.

    establish() and listen() run on the listener thread, send() on the cast
    controller's and register_pairing_code() on the DIAL server's -- the bind
    parameters they share are guarded by one lock.
    """

    def __init__(self, identity, name, transport=None):
        self.identity = identity
        self.name = name
        self.transport = transport or HttpTransport()
        self._lock = threading.Lock()
        self._stream = None
        self._reset()

    def _reset(self):
        with self._lock:
            self.sid = None
            self.gsessionid = None
            self.last_code = -1
            self.aid = 3
            self.ofs = 0
            self.rid = random.randint(41000, 49999)

    @property
    def online(self):
        return self.sid is not None

    def _common(self):
        return {
            "device": "LOUNGE_SCREEN",
            "id": self.identity.device_id,
            "name": self.name,
            "app": SCREEN_APP,
            "theme": "cl",
            "capabilities": "",
            "mdx-version": "2",
            "loungeIdToken": self.identity.lounge_token,
            "VER": "8",
            "v": "2",
            "zx": uuid.uuid4().hex[:12],
            "t": "1",
        }

    def ensure_token(self, force=False):
        identity = self.identity
        if not force and not identity.token_due() and identity.screen_id:
            return
        stored = identity.screen_id
        if not stored:
            identity.screen_id = generate_screen_id(self.transport)
            log.info("new screen %s", mask(identity.screen_id))
        try:
            token = get_lounge_token(self.transport, identity.screen_id)
        except LoungeError as error:
            if not stored:
                raise
            # A screen YouTube no longer knows: start over with a new one.
            log.warning("stored screen %s rejected (%s), creating a new one", mask(stored), error)
            identity.screen_id = generate_screen_id(self.transport)
            log.info("new screen %s", mask(identity.screen_id))
            token = get_lounge_token(self.transport, identity.screen_id)
        now_ms = int(time.time() * 1000)
        identity.lounge_token = token["loungeToken"]
        identity.expiration = int(token.get("expiration") or 0)
        identity.refresh_at = now_ms + int(token.get("refreshIntervalInMillis") or FALLBACK_REFRESH_MS)
        if identity.expiration:
            # The suggested interval can land on the expiry itself; renew a day ahead.
            identity.refresh_at = min(identity.refresh_at, identity.expiration - 24 * 3600 * 1000)
        identity.save()
        log.info("lounge token %s for screen %s, valid until %s, refresh at %s",
                 mask(identity.lounge_token), mask(identity.screen_id),
                 time.strftime("%Y-%m-%d %H:%M", time.localtime(identity.expiration / 1000)),
                 time.strftime("%Y-%m-%d %H:%M", time.localtime(identity.refresh_at / 1000)))

    def establish(self, force_token=False):
        """Open a new session; return its opening commands (loungeStatus etc.)."""
        self._reset()
        self.ensure_token(force=force_token)
        status, text = self._bind()
        if status != 200 and 400 <= status < 500 and not force_token:
            # Typically a token YouTube has already retired: get a new one once.
            log.warning("bind refused (HTTP %s), renewing the lounge token", status)
            self.ensure_token(force=True)
            status, text = self._bind()
        if status != 200:
            raise LoungeError("bind", status, text)
        commands = self.accept(cast_protocol.CommandParser().feed(text))
        if not self.online or not self.gsessionid:
            raise LoungeError("bind (no session id in the answer)", status, text)
        log.info("session established (SID %s) as \"%s\"", mask(self.sid), self.name)
        return commands

    def _bind(self):
        with self._lock:
            query = dict(self._common(), RID=str(self.rid), CVER="1")
            self.rid += 1
        return self.transport.request("POST", BIND, query=query, form={"count": "0"})

    def accept(self, commands):
        """Session bookkeeping for received commands; return the ones to act on.

        Takes the SID ("c") and gsessionid ("S"), acknowledges what arrived
        (AID) and drops anything already seen within this session.
        """
        fresh = []
        with self._lock:
            for command in commands:
                if command.code <= self.last_code:
                    log.debug("command %s already handled, ignored", command.code)
                    continue
                self.last_code = command.code
                self.aid = max(self.aid, command.code)
                if command.name == "c":
                    data = command.data if isinstance(command.data, (list, tuple)) else (command.data,)
                    self.sid = data[0]
                elif command.name == "S":
                    self.gsessionid = command.data
                else:
                    fresh.append(command)
        return fresh

    def listen(self, on_commands):
        """Hold one long poll open until the server ends it; feed it to on_commands.

        Raises LoungeError when the server refuses the poll (session_gone tells
        whether a new session is needed), OSError and http.client exceptions on
        network trouble.
        """
        with self._lock:
            query = dict(self._common(), RID="rpc", SID=self.sid, CI="0", AID=str(self.aid),
                         gsessionid=self.gsessionid, TYPE="xmlhttp")
        self._stream = stream = self.transport.open_stream(BIND, query)
        parser = cast_protocol.CommandParser()
        try:
            for text in stream.chunks():
                log.debug("received %r", text)
                fresh = self.accept(parser.feed(text))
                if fresh:
                    on_commands(fresh)
        finally:
            self._stream = None
            stream.close()

    def abort(self):
        """Break a listen() in progress, from another thread."""
        stream = self._stream
        if stream is not None:
            stream.abort()

    def send(self, sc, payload=None):
        """Post one message (onStateChange, nowPlaying...) to the phones."""
        with self._lock:
            if self.sid is None:
                raise LoungeError("send {} (no session)".format(sc))
            query = dict(self._common(), SID=self.sid, RID=str(self.rid), AID=str(self.aid),
                         gsessionid=self.gsessionid)
            form = {"count": "1", "ofs": str(self.ofs), "req0__sc": sc}
            self.rid += 1
            self.ofs += 1
        for key, value in (payload or {}).items():
            form["req0_" + key] = value
        log.debug("POST %s %r", sc, form)
        status, text = self.transport.request("POST", BIND, query=query, form=form)
        if status != 200:
            raise LoungeError("send {}".format(sc), status, text)

    def register_pairing_code(self, code):
        """Link a phone that found us by DIAL: it hands over a code, we tell YouTube."""
        status, text = self.transport.request("POST", REGISTER_PAIRING_CODE, form={
            "access_type": "permanent",
            "app": SCREEN_APP,
            "pairing_code": code,
            "screen_id": self.identity.screen_id,
            "screen_name": self.name,
            "device_id": self.identity.device_id,
        })
        if status != 200:
            raise LoungeError("register_pairing_code", status, text)
        log.info("pairing code %s registered for screen %s", code, mask(self.identity.screen_id))

    def close(self):
        self.abort()
        self.transport.close()


class LoungeWorker(threading.Thread):
    """Keeps the screen online: session, long polls, recovery, token renewal.

    A long poll ending is normal -- the server closes it every few minutes and
    it is reopened at once. A refused poll (4xx) means the session is gone and a
    new one is opened; network trouble is retried with a growing delay. Nothing
    here ever gives up while the worker runs: a box that was offline for an hour
    reconnects on its own.
    """

    MAX_DELAY = 60

    def __init__(self, session, on_commands, on_online):
        super().__init__(name="ytdlpcast-lounge", daemon=True)
        self.session = session
        self.on_commands = on_commands
        self.on_online = on_online
        self._stopping = threading.Event()

    def stop(self):
        self._stopping.set()
        self.session.abort()

    def run(self):
        failures = 0
        need_session = True
        online = False
        while not self._stopping.is_set():
            try:
                token_due = self.session.identity.token_due()
                if need_session or token_due:
                    if token_due and not need_session:
                        log.info("lounge token due for renewal, starting a new session")
                    commands = self.session.establish(force_token=token_due)
                    need_session = False
                    if not online:
                        online = True
                        self.on_online(True)
                    if commands:
                        self.on_commands(commands)
                started = time.monotonic()
                self.session.listen(self.on_commands)
                if time.monotonic() - started > 1:
                    failures = 0
                    log.debug("long poll ended, reopening")
                    continue
                # Ended at once: something is off; do not spin.
                failures += 1
                log.warning("long poll ended immediately")
            except LoungeError as error:
                if self._stopping.is_set():
                    break
                failures += 1
                need_session = need_session or error.session_gone
                log.warning("%s%s", error, "; starting a new session" if error.session_gone else "")
            except (OSError, http.client.HTTPException) as error:
                if self._stopping.is_set():
                    break
                failures += 1
                log.warning("network error: %s: %s", type(error).__name__, error)
            except Exception:  # noqa: BLE001 - the screen must stay online whatever happens
                if self._stopping.is_set():
                    break
                failures += 1
                need_session = True
                log.exception("unexpected error in the lounge listener")
            if failures:
                if failures >= 3:
                    # Long enough for the session to have lapsed: reopen it
                    # rather than poll a SID YouTube may have forgotten.
                    need_session = True
                    if online:
                        online = False
                        self.on_online(False)
                delay = min(2 ** min(failures, 6), self.MAX_DELAY)
                log.warning("retrying in %ds (attempt %d)", delay, failures)
                if self._stopping.wait(delay):
                    break
        if online:
            self.on_online(False)
        self.session.close()
        log.info("lounge listener stopped")
