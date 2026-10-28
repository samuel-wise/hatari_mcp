"""Memory access and state save/load."""

from __future__ import annotations

import base64
import binascii
import uuid
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from ..app import App
from ..errors import error
from ._common import guarded


def _addr(value: str | int) -> str:
    if isinstance(value, int):
        return f"${value:x}"
    text = str(value)
    if text.startswith("0x"):
        return "$" + text[2:]
    if text.startswith("$"):
        return text
    try:
        return f"${int(text, 10):x}"
    except ValueError:
        return text


def register(server: MCPServer, app: App) -> None:
    @server.tool(
        name="dbg_memory_read",
        description=(
            "Read exact guest memory bytes with savebin. Returns base64 and hex. "
            "Maximum 64 KiB per call."
        ),
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False),
    )
    @guarded
    async def dbg_memory_read(address: str | int, length: int) -> dict[str, Any]:
        length = int(length)
        if length < 1 or length > 65536:
            raise error("bad_option", "length must be between 1 and 65536.", "Read in smaller chunks.")
        runtime = app.supervisor.runtime
        if not runtime:
            raise error("not_running", "Hatari has no runtime directory.", "Call emu_start first.")
        path = runtime.path / f"mem-{uuid.uuid4().hex}.bin"
        await app.supervisor.debug(f"savebin {path} {_addr(address)} 0x{length:x}")
        if not path.is_file():
            raise error("parse_failed", "Hatari did not create the memory dump file.", "Check the address.")
        data = path.read_bytes()
        path.unlink(missing_ok=True)
        return {
            "ok": True,
            "address": _addr(address),
            "length": len(data),
            "hex": data.hex(),
            "base64": base64.b64encode(data).decode("ascii"),
        }

    @server.tool(
        name="dbg_memory_write",
        description="Write exact bytes to guest memory with loadbin. data_hex must be an even-length hex string.",
        annotations=ToolAnnotations(destructiveHint=True),
    )
    @guarded
    async def dbg_memory_write(address: str | int, data_hex: str) -> dict[str, Any]:
        try:
            data = binascii.unhexlify(data_hex.strip())
        except (binascii.Error, ValueError) as exc:
            raise error("bad_option", f"Invalid hex data: {exc}", "Pass an even-length hexadecimal string.") from exc
        if len(data) > 65536:
            raise error("bad_option", "Write is larger than 64 KiB.", "Write in smaller chunks.")
        runtime = app.supervisor.runtime
        if not runtime:
            raise error("not_running", "Hatari has no runtime directory.", "Call emu_start first.")
        path = runtime.path / f"mem-{uuid.uuid4().hex}.bin"
        path.write_bytes(data)
        try:
            result = await app.supervisor.debug(f"loadbin {path} {_addr(address)}")
        finally:
            path.unlink(missing_ok=True)
        return {**result, "address": _addr(address), "length": len(data)}

    @server.tool(
        name="dbg_memory_dump",
        description="Text memory dump with Hatari's memdump command.",
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False),
    )
    @guarded
    async def dbg_memory_dump(address: str | int, count: int = 64, width: str = "b") -> dict[str, Any]:
        if width not in {"b", "w", "l"}:
            raise error("bad_option", f"Invalid width: {width}", "Use b, w or l.")
        count = max(1, min(int(count), 4096))
        return await app.supervisor.debug(f"memdump {_addr(address)} 0x{count:x} {width}")

    @server.tool(
        name="dbg_state_save",
        description="Save an emulator state file with the debugger statesave command.",
        annotations=ToolAnnotations(destructiveHint=False),
    )
    @guarded
    async def dbg_state_save(path: str) -> dict[str, Any]:
        target = app.guard_path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        result = await app.supervisor.debug(f'statesave "{target}"', timeout_ms=10000)
        return {**result, "path": str(target)}

    @server.tool(
        name="dbg_state_load",
        description="Load an emulator state file with the debugger stateload command.",
        annotations=ToolAnnotations(destructiveHint=True),
    )
    @guarded
    async def dbg_state_load(path: str) -> dict[str, Any]:
        target = app.guard_path(path, must_exist=True)
        result = await app.supervisor.debug(f'stateload "{target}"', timeout_ms=10000)
        return {**result, "path": str(target)}
