"""Minimal, deliberate HTTP/JSON-RPC + SSE transport (partial MCP transport).

This is not a complete MCP HTTP transport. It provides the security-critical
pieces that can be built with the standard library:

* loopback bind by default; a non-loopback bind requires an explicit opt-in,
  a token, and an explicit ``allowed_hosts`` list;
* bearer token on every POST/SSE request, compared with ``hmac.compare_digest``;
* Host and Origin checks, ``application/json`` for POST, and an optional
  anti-CSRF header for browser callers;
* bounded request bodies, socket timeouts, bounded per-session SSE replay
  rings with ``Last-Event-ID``, and one session id per emulator context;
* JSON-RPC 2.0 POST dispatch to a caller-supplied handler.

It does not implement the MCP initialize/session state machine, streamable
HTTP resumption semantics, TLS, or multi-tenant isolation; those remain
explicitly out of scope. TLS must be terminated by a reverse proxy.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import urlparse

from .auth import (
    AuthDecision,
    authorize,
    is_loopback,
    load_token,
    redact_text,
    validate_session_id,
)

logger = logging.getLogger("hatari_mcp.http")

MAX_BODY = 1024 * 1024
MAX_SESSIONS = 64
REPLAY_LIMIT = 256
SESSION_QUEUE_LIMIT = 256
SESSION_TTL_SECONDS = 1800.0


@dataclass
class Session:
    session_id: str
    queue: queue.Queue = field(default_factory=lambda: queue.Queue(maxsize=SESSION_QUEUE_LIMIT))
    replay: deque = field(default_factory=lambda: deque(maxlen=REPLAY_LIMIT))
    next_event_id: int = 1
    lock: threading.Lock = field(default_factory=threading.Lock)
    created_at: float = field(default_factory=time.monotonic)
    last_seen: float = field(default_factory=time.monotonic)
    dropped: int = 0

    def enqueue(self, entry: Any) -> bool:
        """Enqueue without blocking; on overflow drop the oldest event.

        The replay ring still holds recent events, so a reconnecting client can
        recover them with ``Last-Event-ID`` even after a queue drop.
        """
        try:
            self.queue.put_nowait(entry)
            return True
        except queue.Full:
            pass
        try:
            self.queue.get_nowait()
            self.dropped += 1
        except queue.Empty:
            pass
        try:
            self.queue.put_nowait(entry)
            return True
        except queue.Full:
            self.dropped += 1
            return False


class HttpSseServer:
    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 0,
        token: str | None = None,
        token_file: str | None = None,
        allow_non_loopback: bool = False,
        allowed_origins: set[str] | frozenset[str] | None = None,
        allowed_hosts: set[str] | frozenset[str] | None = None,
        require_custom_header: bool = False,
        max_body: int = MAX_BODY,
        max_sessions: int = MAX_SESSIONS,
        max_queue: int = SESSION_QUEUE_LIMIT,
        session_ttl: float = SESSION_TTL_SECONDS,
        request_timeout: float = 10.0,
        sse_heartbeat: float = 15.0,
        rpc_handler: Callable[[dict[str, Any], str], Any] | None = None,
    ):
        if not is_loopback(host) and not allow_non_loopback:
            raise ValueError("Refusing a non-loopback bind without allow_non_loopback=True.")
        self.host = host
        self.port = int(port)
        self.allow_non_loopback = allow_non_loopback
        self.token = load_token(token=token, token_file=token_file, generate=True)
        if not is_loopback(host) and not self.token:
            raise ValueError("A non-loopback bind requires an HTTP bearer token.")
        self.allowed_origins = frozenset(allowed_origins or ())
        self.allowed_hosts = frozenset(allowed_hosts or ())
        if not is_loopback(host) and not self.allowed_hosts:
            # Without an allowlist every non-loopback Host header is rejected,
            # which makes the bind useless; refuse it at construction instead.
            raise ValueError(
                "A non-loopback bind requires allowed_hosts (the Host header names clients use)."
            )
        self.require_custom_header = require_custom_header
        self.max_body = max(1, int(max_body))
        self.max_sessions = max(1, int(max_sessions))
        self.max_queue = max(1, int(max_queue))
        self.session_ttl = max(0.1, float(session_ttl))
        self.request_timeout = max(0.1, float(request_timeout))
        self.sse_heartbeat = max(0.1, float(sse_heartbeat))
        self.rpc_handler = rpc_handler
        self._sessions: dict[str, Session] = {}
        self._sessions_lock = threading.Lock()
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._stopping = threading.Event()

    @property
    def bound_port(self) -> int:
        assert self._httpd is not None
        return int(self._httpd.server_address[1])

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.bound_port}"

    def start(self) -> str:
        if self._httpd is not None:
            return self.url
        handler = _make_handler(self)
        self._httpd = ThreadingHTTPServer((self.host, self.port), handler)
        self._httpd.daemon_threads = True
        self._thread = threading.Thread(target=self._httpd.serve_forever, name="hatari-mcp-http", daemon=True)
        self._thread.start()
        logger.info("HTTP/SSE listening on %s (partial MCP transport)", self.url)
        return self.url

    def stop(self) -> None:
        self._stopping.set()
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None
        with self._sessions_lock:
            for session in self._sessions.values():
                session.enqueue(None)
            self._sessions.clear()

    @property
    def session_count(self) -> int:
        with self._sessions_lock:
            return len(self._sessions)

    def _prune_sessions_locked(self, now: float | None = None) -> None:
        """Expire idle sessions so a long-lived server cannot grow forever."""
        if not self._sessions:
            return
        now = time.monotonic() if now is None else now
        expired = [
            session_id
            for session_id, session in self._sessions.items()
            if now - session.last_seen > self.session_ttl
        ]
        for session_id in expired:
            session = self._sessions.pop(session_id, None)
            if session is not None:
                session.enqueue(None)

    def get_session(self, session_id: str | None) -> tuple[Session, bool]:
        requested = validate_session_id(session_id)
        with self._sessions_lock:
            now = time.monotonic()
            self._prune_sessions_locked(now)
            if requested and requested in self._sessions:
                session = self._sessions[requested]
                session.last_seen = now
                return session, False
            if len(self._sessions) >= self.max_sessions:
                raise ValueError("too many active HTTP sessions")
            new_id = requested or uuid.uuid4().hex
            session = Session(new_id, queue=queue.Queue(maxsize=self.max_queue))
            session.last_seen = now
            self._sessions[new_id] = session
            return session, True

    def publish(self, event: str, data: Any, *, session_id: str | None = None) -> int | None:
        """Publish one SSE event; returns the event id or None if no session."""
        payload = data if isinstance(data, str) else json.dumps(data, separators=(",", ":"))
        with self._sessions_lock:
            self._prune_sessions_locked()
            if session_id is not None:
                session = self._sessions.get(session_id)
                sessions = [session] if session else []
            else:
                sessions = list(self._sessions.values())
        event_id = None
        for session in sessions:
            with session.lock:
                event_id = session.next_event_id
                session.next_event_id += 1
                entry = (event_id, event, payload)
                session.replay.append(entry)
            session.enqueue(entry)
        return event_id

    def dispatch(self, request: dict[str, Any], session_id: str) -> Any:
        if self.rpc_handler is None:
            return {"jsonrpc": "2.0", "id": request.get("id"), "error": {"code": -32601, "message": "No handler"}}
        result = self.rpc_handler(request, session_id)
        if isinstance(result, dict) and result.get("jsonrpc") == "2.0":
            return result
        return {"jsonrpc": "2.0", "id": request.get("id"), "result": result}


def _make_handler(server: HttpSseServer):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "hatari-mcp-http"
        sys_version = ""

        def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
            message = redact_text(fmt % args if args else fmt, (server.token or "",))
            logger.info("http %s", message)

        def _headers(self) -> dict[str, str]:
            return {key: value for key, value in self.headers.items()}

        def _auth(self) -> AuthDecision:
            headers = self._headers()
            return authorize(
                expected_token=server.token,
                authorization=headers.get("Authorization"),
                origin=headers.get("Origin"),
                host=headers.get("Host"),
                allowed_origins=server.allowed_origins or None,
                allowed_hosts=server.allowed_hosts or None,
                require_custom_header=server.require_custom_header,
                custom_header=headers.get("X-Hatari-MCP"),
            )

        def _send_json(self, status: int, payload: Any, *, extra: dict[str, str] | None = None) -> None:
            body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for key, value in (extra or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

        def _deny(self, decision: AuthDecision) -> None:
            extra = {"WWW-Authenticate": 'Bearer realm="hatari-mcp"'} if decision.status == 401 else None
            self._reject(
                decision.status,
                {"jsonrpc": "2.0", "id": None, "error": {"code": decision.status, "message": decision.reason}},
                extra=extra,
            )

        def _reject(self, status: int, payload: Any, *, extra: dict[str, str] | None = None) -> None:
            """Send an error and close the connection so an unread body cannot
            desynchronise a keep-alive stream."""
            self.close_connection = True
            self._send_json(status, payload, extra=extra)

        def _path(self) -> str:
            return urlparse(self.path).path

        def do_POST(self) -> None:  # noqa: N802
            if self._path() not in {"/", "/mcp"}:
                self._reject(HTTPStatus.NOT_FOUND, {"error": "not found"})
                return
            decision = self._auth()
            if not decision.ok:
                self._deny(decision)
                return
            content_type = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
            if content_type != "application/json":
                self._reject(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "application/json required"})
                return
            # Reject ambiguous framing outright rather than trusting one header.
            if self.headers.get("Transfer-Encoding") is not None:
                self._reject(HTTPStatus.BAD_REQUEST, {"error": "Transfer-Encoding is not supported"})
                return
            lengths = self.headers.get_all("Content-Length") or []
            if len(lengths) != 1:
                self._reject(HTTPStatus.LENGTH_REQUIRED, {"error": "one Content-Length required"})
                return
            try:
                length = int(lengths[0])
            except ValueError:
                self._reject(HTTPStatus.BAD_REQUEST, {"error": "invalid Content-Length"})
                return
            if length < 0 or length > server.max_body:
                self._reject(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "body too large"})
                return
            try:
                self.connection.settimeout(server.request_timeout)
                raw = self.rfile.read(length)
            except TimeoutError:
                self._reject(HTTPStatus.REQUEST_TIMEOUT, {"error": "request timeout"})
                return
            if len(raw) != length:
                self._reject(HTTPStatus.BAD_REQUEST, {"error": "short request body"})
                return
            try:
                request = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._reject(HTTPStatus.BAD_REQUEST, {"error": "invalid JSON"})
                return
            if not isinstance(request, dict) or request.get("jsonrpc") != "2.0" or not isinstance(request.get("method"), str):
                self._reject(HTTPStatus.BAD_REQUEST, {"error": "invalid JSON-RPC request"})
                return

            try:
                session, created = server.get_session(self.headers.get("Mcp-Session-Id"))
            except ValueError as exc:
                self._reject(HTTPStatus.SERVICE_UNAVAILABLE, {"error": str(exc)})
                return
            extra = {"Mcp-Session-Id": session.session_id} if created else None
            request_id = request.get("id")
            if "id" not in request:
                self.send_response(HTTPStatus.ACCEPTED)
                self.send_header("Content-Length", "0")
                if created:
                    self.send_header("Mcp-Session-Id", session.session_id)
                self.end_headers()
                return
            try:
                response = server.dispatch(request, session.session_id)
            except ValueError as exc:
                response = {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32602, "message": str(exc)},
                }
            except Exception as exc:  # pragma: no cover - defensive
                logger.exception("HTTP RPC handler failed")
                response = {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32603, "message": "internal error"},
                }
            self._send_json(HTTPStatus.OK, response, extra=extra)

        def do_GET(self) -> None:  # noqa: N802
            if self._path() not in {"/mcp/events", "/events"}:
                self._reject(HTTPStatus.NOT_FOUND, {"error": "not found"})
                return
            decision = self._auth()
            if not decision.ok:
                self._deny(decision)
                return
            try:
                session, created = server.get_session(self.headers.get("Mcp-Session-Id"))
            except ValueError as exc:
                self._reject(HTTPStatus.SERVICE_UNAVAILABLE, {"error": str(exc)})
                return
            self.connection.settimeout(server.sse_heartbeat + 5.0)
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            if created:
                self.send_header("Mcp-Session-Id", session.session_id)
            self.end_headers()
            last_event = self.headers.get("Last-Event-ID")
            try:
                last_id = int(last_event) if last_event is not None else 0
            except ValueError:
                last_id = 0
            try:
                with session.lock:
                    replay = list(session.replay)
                for event_id, event, payload in replay:
                    if event_id <= last_id:
                        continue
                    self._write_event(event_id, event, payload)
                    last_id = event_id
                while not server._stopping.is_set():
                    session.last_seen = time.monotonic()
                    try:
                        item = session.queue.get(timeout=server.sse_heartbeat)
                    except queue.Empty:
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
                        continue
                    if item is None:
                        break
                    # The queue may still contain events already replayed from
                    # the ring; skip them so a reconnect never duplicates.
                    if item[0] <= last_id:
                        continue
                    self._write_event(*item)
                    last_id = item[0]
                    session.last_seen = time.monotonic()
            except (BrokenPipeError, ConnectionResetError, TimeoutError):
                pass
            finally:
                try:
                    self.close_connection = True
                except Exception:
                    pass

        def _write_event(self, event_id: int, event: str, payload: str) -> None:
            data = f"id: {event_id}\nevent: {event}\ndata: {payload}\n\n".encode("utf-8")
            self.wfile.write(data)
            self.wfile.flush()

        def do_OPTIONS(self) -> None:  # noqa: N802
            self.send_response(HTTPStatus.METHOD_NOT_ALLOWED)
            self.send_header("Allow", "GET, POST")
            self.send_header("Content-Length", "0")
            self.end_headers()

    return Handler
