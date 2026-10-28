"""Minimal HTTP/SSE transport security and session behavior."""

from __future__ import annotations

import http.client
import json
import time

import pytest

from hatari_mcp.transport.http_sse import HttpSseServer

TOKEN = "z" * 32


def make_server(**kwargs) -> HttpSseServer:
    def handler(request, session_id):
        return {"sid": session_id, "echo": request.get("params")}

    server = HttpSseServer(token=TOKEN, rpc_handler=handler, request_timeout=2, sse_heartbeat=2, **kwargs)
    server.start()
    return server


def post(server: HttpSseServer, headers: dict, body: dict | bytes) -> tuple[int, dict, bytes]:
    raw = body if isinstance(body, bytes) else json.dumps(body).encode()
    connection = http.client.HTTPConnection("127.0.0.1", server.bound_port, timeout=3)
    try:
        connection.request("POST", "/mcp", body=raw, headers=headers)
        response = connection.getresponse()
        data = response.read()
        return response.status, {key: response.getheader(key) for key in response.headers}, data
    finally:
        connection.close()


def rpc(method: str = "ping", *, request_id: object = 1) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "method": method, "params": {"a": 1}}


def test_requires_bearer_and_never_accepts_query_tokens() -> None:
    server = make_server()
    try:
        status, _, body = post(server, {"Content-Type": "application/json"}, rpc())
        assert status == 401
        assert b"bearer" in body.lower()
        status, _, _ = post(
            server,
            {"Content-Type": "application/json", "Authorization": "Bearer wrong"},
            rpc(),
        )
        assert status == 401
        status, headers, body = post(
            server,
            {"Content-Type": "application/json", "Authorization": "Bearer " + TOKEN},
            rpc(),
        )
        assert status == 200
        assert json.loads(body)["result"]["echo"] == {"a": 1}
        assert headers["Mcp-Session-Id"]
    finally:
        server.stop()


def test_rejects_bad_origin_host_and_content_type() -> None:
    server = make_server()
    try:
        common = {"Content-Type": "application/json", "Authorization": "Bearer " + TOKEN}
        status, _, _ = post(server, {**common, "Origin": "https://evil.example"}, rpc())
        assert status == 403
        status, _, _ = post(server, {**common, "Host": "evil.example"}, rpc())
        assert status == 403
        status, _, _ = post(server, {"Authorization": "Bearer " + TOKEN}, rpc())
        assert status == 415
    finally:
        server.stop()


def test_body_cap_invalid_json_and_notification() -> None:
    server = make_server(max_body=128)
    try:
        headers = {"Content-Type": "application/json", "Authorization": "Bearer " + TOKEN}
        status, _, _ = post(server, headers, b"x" * 256)
        assert status == 413
        status, _, _ = post(server, headers, b"not json")
        assert status == 400
        notification = {"jsonrpc": "2.0", "method": "notify", "params": {}}
        status, _, body = post(server, headers, notification)
        assert status == 202
        assert body == b""
    finally:
        server.stop()


def test_session_id_is_reused() -> None:
    server = make_server()
    try:
        headers = {"Content-Type": "application/json", "Authorization": "Bearer " + TOKEN}
        status, response_headers, body = post(server, headers, rpc(method="first"))
        assert status == 200
        session_id = response_headers["Mcp-Session-Id"]
        first_sid = json.loads(body)["result"]["sid"]
        status, _, body = post(server, {**headers, "Mcp-Session-Id": session_id}, rpc(method="second"))
        assert status == 200
        assert json.loads(body)["result"]["sid"] == first_sid
    finally:
        server.stop()


def test_sse_auth_and_replay_ring() -> None:
    server = make_server()
    try:
        unauthorized = http.client.HTTPConnection("127.0.0.1", server.bound_port, timeout=3)
        unauthorized.request("GET", "/mcp/events")
        response = unauthorized.getresponse()
        assert response.status == 401
        response.read()
        unauthorized.close()

        connection = http.client.HTTPConnection("127.0.0.1", server.bound_port, timeout=3)
        connection.request("GET", "/mcp/events", headers={"Authorization": "Bearer " + TOKEN})
        response = connection.getresponse()
        assert response.status == 200
        assert response.getheader("Content-Type") == "text/event-stream"
        session_id = response.getheader("Mcp-Session-Id")
        event_id = server.publish("breakpoint.hit", {"pc": 0x1234}, session_id=session_id)
        assert event_id == 1
        lines = b""
        while not lines.endswith(b"\n\n"):
            lines += response.read(1)
        assert b"event: breakpoint.hit" in lines
        assert b'"pc":4660' in lines
        connection.close()

        replay = http.client.HTTPConnection("127.0.0.1", server.bound_port, timeout=3)
        replay.request(
            "GET",
            "/mcp/events",
            headers={
                "Authorization": "Bearer " + TOKEN,
                "Mcp-Session-Id": session_id,
                "Last-Event-ID": "0",
            },
        )
        response = replay.getresponse()
        assert response.status == 200
        lines = b""
        while not lines.endswith(b"\n\n"):
            lines += response.read(1)
        assert b"id: 1" in lines
        replay.close()
    finally:
        server.stop()


def test_non_loopback_bind_requires_opt_in() -> None:
    with pytest.raises(ValueError):
        HttpSseServer(host="0.0.0.0", token=TOKEN)


def test_ambiguous_framing_and_session_limit_are_rejected() -> None:
    server = make_server(max_sessions=1)
    try:
        headers = {"Content-Type": "application/json", "Authorization": "Bearer " + TOKEN}
        status, _, _ = post(server, {**headers, "Transfer-Encoding": "chunked"}, rpc())
        assert status == 400

        status, response_headers, _ = post(server, headers, rpc(method="first"))
        assert status == 200
        session_id = response_headers["Mcp-Session-Id"]
        assert session_id
        status, _, _ = post(server, headers, rpc(method="second"))
        assert status == 503
        status, _, _ = post(
            server, {**headers, "Mcp-Session-Id": session_id}, rpc(method="third")
        )
        assert status == 200
    finally:
        server.stop()


def test_non_loopback_bind_requires_allowed_hosts() -> None:
    with pytest.raises(ValueError):
        HttpSseServer(host="0.0.0.0", token=TOKEN, allow_non_loopback=True)

    server = HttpSseServer(
        host="0.0.0.0",
        token=TOKEN,
        allow_non_loopback=True,
        allowed_hosts={"example.test:8443"},
        request_timeout=2,
        sse_heartbeat=2,
        rpc_handler=lambda request, session_id: {
            "sid": session_id,
            "echo": request.get("params"),
        },
    )
    server.start()
    try:
        headers = {
            "Content-Type": "application/json",
            "Authorization": "Bearer " + TOKEN,
            "Host": "example.test:8443",
        }
        status, _, body = post(server, headers, rpc())
        assert status == 200
        assert json.loads(body)["result"]["echo"] == {"a": 1}
        status, _, _ = post(server, {**headers, "Host": "evil.example"}, rpc())
        assert status == 403
    finally:
        server.stop()


def test_session_queue_is_bounded_and_drops_oldest() -> None:
    server = make_server(max_queue=1)
    try:
        session, _ = server.get_session(None)
        assert server.publish("first", {"n": 1}, session_id=session.session_id) == 1
        assert server.publish("second", {"n": 2}, session_id=session.session_id) == 2
        assert session.queue.qsize() <= 1
        assert session.dropped >= 1
        assert len(session.replay) == 2
    finally:
        server.stop()


def test_idle_sessions_expire_and_are_pruned() -> None:
    server = make_server(session_ttl=0.05)
    try:
        session, created = server.get_session(None)
        assert created is True
        session_id = session.session_id
        assert server.session_count == 1
        time.sleep(0.12)
        assert server.publish("late", {"n": 1}, session_id=session_id) is None
        assert server.session_count == 0
        assert session_id not in server._sessions
    finally:
        server.stop()
