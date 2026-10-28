"""Legacy adapter keeps one interface and names stock fallbacks."""

from __future__ import annotations

import asyncio

import pytest

from hatari_mcp.protocol.errors import ProtocolError
from hatari_mcp.protocol.legacy import LegacyAdapter


def test_debug_command_maps_to_supervisor() -> None:
    calls = []

    async def debug(command, *, channel="auto", timeout_ms=None):
        calls.append((command, channel, timeout_ms))
        return {"ok": True, "raw": "done"}

    adapter = LegacyAdapter(debug)
    result = asyncio.run(adapter.request("dbg.command", {"command": "cpureg"}, timeout=1.5))
    assert result["ok"] is True
    assert calls == [("cpureg", "auto", 1500)]


def test_unsupported_framed_operation_has_fallback_hint() -> None:
    async def debug(command, **kwargs):
        return {}

    adapter = LegacyAdapter(debug)
    with pytest.raises(ProtocolError) as excinfo:
        asyncio.run(adapter.request("mem.read", {"address": 0, "length": 4}))
    assert excinfo.value.code == "unsupported"
    assert "savebin" in excinfo.value.hint or "memory tool" in excinfo.value.hint


def test_ping_and_capabilities() -> None:
    async def debug(command, **kwargs):
        return {}

    async def probe() -> bool:
        return True

    adapter = LegacyAdapter(debug, probe_prompt=probe)
    assert asyncio.run(adapter.ping())["pong"] is True
    assert adapter.capabilities["protocol_mode"] == "legacy"
    assert adapter.capabilities["states"]["framed"] == "unsupported"
    asyncio.run(adapter.close())
