"""Legacy sentinel/stdin adapter with the framed client's request interface.

Stock Hatari has no framed transport. This adapter exposes ``request``,
``ping`` and ``close`` so tool code can depend on one interface; it maps a
small, explicit operation set onto the existing supervisor debugger commands.
Unsupported framed-only operations return a typed ``unsupported`` error with
the stock fallback named in the hint instead of pretending to work.
"""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable

from .capabilities import describe
from .errors import UNSUPPORTED, make_protocol_error

DebugFn = Callable[..., Awaitable[dict[str, Any]]]
ProbeFn = Callable[[], Awaitable[bool]]


class LegacyAdapter:
    def __init__(
        self,
        debug: DebugFn,
        *,
        probe_prompt: ProbeFn | None = None,
        requested_mode: str = "legacy",
    ):
        self._debug = debug
        self._probe_prompt = probe_prompt
        self.requested_mode = requested_mode

    @property
    def capabilities(self) -> dict[str, Any]:
        return describe(requested_mode=self.requested_mode)

    async def close(self) -> None:
        return None

    async def ping(self, *, timeout: float = 2.0) -> dict[str, Any]:
        if self._probe_prompt is None:
            return {"pong": False, "protocol": "legacy", "reason": "no probe configured"}
        try:
            alive = await asyncio.wait_for(self._probe_prompt(), timeout)
        except asyncio.TimeoutError:
            alive = False
        return {"pong": bool(alive), "protocol": "legacy"}

    async def request(
        self,
        op: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> Any:
        params = params or {}
        if not isinstance(params, dict):
            raise make_protocol_error("bad_request", "Legacy request params must be an object.")
        if op in {"dbg.command", "debugger.command"}:
            command = params.get("command")
            if not isinstance(command, str) or not command:
                raise make_protocol_error("bad_request", "dbg.command requires a command.")
            channel = params.get("channel", "auto")
            timeout_ms = params.get("timeout_ms")
            if timeout is not None and timeout_ms is None:
                timeout_ms = int(timeout * 1000)
            return await self._debug(command, channel=channel, timeout_ms=timeout_ms)
        if op in {"emulator.status", "status"}:
            return {"protocol": "legacy", "capabilities": self.capabilities}
        fallback = {
            "mem.read": "Use the memory tool (savebin/memdump) instead.",
            "fb.read": "Use screen_screenshot and decode it server-side.",
            "clock.step": "Use an _vbl breakpoint or --run-vbls; it is approximate.",
            "input.inject": "Use emu_input events; they are host-timed, not frame-locked.",
        }
        raise make_protocol_error(
            UNSUPPORTED,
            f"Operation {op!r} needs the framed protocol.",
            fallback.get(op, "Use the matching legacy tool or a patched Hatari build."),
        )
