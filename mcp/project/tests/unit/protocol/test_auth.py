"""Bearer token, origin and redaction helpers."""

from __future__ import annotations

import os
import stat

import pytest

from hatari_mcp.transport import auth

TOKEN = "t" * 32


def test_token_loading_and_generation(monkeypatch) -> None:
    assert auth.load_token(token=TOKEN) == TOKEN
    assert auth.load_token(env={"HATARI_MCP_HTTP_TOKEN": TOKEN}) == TOKEN
    generated = auth.load_token(env={})
    assert generated and len(generated) >= 32
    assert auth.load_token(env={}, generate=False) is None
    with pytest.raises(ValueError):
        auth.load_token(token="short")


def test_token_file_permissions(tmp_path) -> None:
    good = tmp_path / "token"
    good.write_text(TOKEN)
    os.chmod(good, 0o600)
    assert auth.load_token(token_file=good) == TOKEN
    os.chmod(good, 0o644)
    with pytest.raises(ValueError):
        auth.load_token(token_file=good)


def test_bearer_parsing_and_constant_time_compare() -> None:
    assert auth.parse_bearer("Bearer abc") == "abc"
    assert auth.parse_bearer("bearer abc") is None
    assert auth.parse_bearer("Basic abc") is None
    assert auth.parse_bearer(None) is None
    assert auth.token_matches(TOKEN, TOKEN) is True
    assert auth.token_matches(TOKEN, TOKEN[:-1] + "x") is False
    assert auth.token_matches(None, TOKEN) is False


def test_origin_and_host_checks() -> None:
    assert auth.origin_allowed(None) is True
    assert auth.origin_allowed("http://127.0.0.1:1234") is True
    assert auth.origin_allowed("http://localhost") is True
    assert auth.origin_allowed("https://evil.example") is False
    assert auth.origin_allowed("null") is False
    assert auth.origin_allowed("https://evil.example", frozenset({"https://evil.example"})) is True

    assert auth.host_allowed("127.0.0.1:8000") is True
    assert auth.host_allowed("localhost") is True
    assert auth.host_allowed("evil.example") is False
    assert auth.host_allowed("evil.example:80", frozenset({"evil.example:80"})) is True
    assert auth.host_allowed(None) is False


def test_authorize_statuses() -> None:
    decision = auth.authorize(expected_token=TOKEN, authorization=None, host="127.0.0.1")
    assert (decision.ok, decision.status) == (False, 401)
    decision = auth.authorize(expected_token=TOKEN, authorization="Bearer " + TOKEN, host="evil")
    assert (decision.ok, decision.status) == (False, 403)
    decision = auth.authorize(
        expected_token=TOKEN,
        authorization="Bearer " + TOKEN,
        host="127.0.0.1",
        origin="https://evil.example",
    )
    assert (decision.ok, decision.status) == (False, 403)
    decision = auth.authorize(
        expected_token=TOKEN,
        authorization="Bearer " + TOKEN,
        host="127.0.0.1",
        require_custom_header=True,
    )
    assert (decision.ok, decision.status) == (False, 403)
    decision = auth.authorize(expected_token=TOKEN, authorization="Bearer " + TOKEN, host="127.0.0.1")
    assert decision.ok is True


def test_redaction_never_keeps_authorization_or_token() -> None:
    headers = auth.redact_headers({"Authorization": "Bearer " + TOKEN, "Accept": "text/plain"})
    assert headers["Authorization"] == "<redacted>"
    assert headers["Accept"] == "text/plain"
    assert TOKEN not in auth.redact_text("token=" + TOKEN, (TOKEN,))
    assert auth.is_loopback("::1") is True
    assert auth.is_loopback("10.0.0.1") is False
