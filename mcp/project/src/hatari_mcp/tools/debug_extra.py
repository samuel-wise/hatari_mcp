"""Debugger register, breakpoint-wait, memory-find and exception helpers."""

from __future__ import annotations

import asyncio
import hashlib
import re
import time
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from ..app import App
from ..errors import HatariError, error
from ._common import guarded

REGISTER_NAMES = frozenset(
    {f"d{index}" for index in range(8)}
    | {f"a{index}" for index in range(8)}
    | {f"v{index}" for index in range(8)}
    | {"pc", "sr", "usp", "isp", "caar", "cacr", "dfc", "sfc", "msp", "vbr"}
)
COMPARISON_RE = re.compile(r"==|!=|<>|<=|>=|=|<|>")
BREAKPOINT_ID_RE = re.compile(r"condition breakpoint\s+(\d+)\s+with", re.IGNORECASE)
FIND_ADDRESS_RE = re.compile(r"^\s*([0-9A-Fa-f]{8}):\s")
FIND_COUNT_RE = re.compile(r"(\d+)\s+matches?\.", re.IGNORECASE)
PC_RE = re.compile(r"Next PC:\s*([0-9A-Fa-f]+)", re.IGNORECASE)
HEX_RE = re.compile(r"[0-9A-Fa-f]+")
MAX_FIND_SCAN = 16 * 1024 * 1024
MAX_SAVE_LENGTH = 512 * 1024 * 1024


def _address(value: int, label: str = "address") -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise error("bad_option", f"{label} must be an integer.", "Pass a decimal or 0x-prefixed integer.")
    if value < 0 or value > 0xFFFFFFFF:
        raise error("bad_option", f"{label} must be between 0 and 0xffffffff.", "Use a 32-bit address.")
    return value


def normalize_register(register: str) -> str:
    if not isinstance(register, str):
        raise error("bad_option", "register must be a string.", "Use a CPU register name such as d0 or pc.")
    name = register.strip().lower()
    if name not in REGISTER_NAMES:
        raise error(
            "bad_option",
            f"Unsupported CPU register: {register!r}",
            "Use d0-d7, a0-a7, pc, sr, usp, isp, caar, cacr, dfc, sfc, msp, vbr or v0-v7.",
        )
    return name


def format_register_value(value: int | str) -> str:
    if isinstance(value, bool):
        raise error("bad_option", "Boolean values are not valid register values.", "Pass an integer or expression.")
    if isinstance(value, int):
        value = _address(value, "value")
        return f"${value:x}"
    if not isinstance(value, str):
        raise error("bad_option", "value must be an integer or string.", "Pass an integer or debugger expression.")
    text = value.strip()
    if not text:
        raise error("bad_option", "value must not be empty.", "Pass an integer or debugger expression.")
    if any(char in text for char in ("\x00", "\n", "\r", "=")) or any(ord(char) < 32 for char in text):
        raise error(
            "bad_option",
            "Register value contains a forbidden character.",
            "Pass a single-line integer or debugger expression without '='.",
        )
    if re.fullmatch(r"[+-]?\d+", text):
        number = int(text, 10)
        if number < 0 or number > 0xFFFFFFFF:
            raise error("bad_option", "Register value must fit in 32 bits.", "Use 0..0xffffffff.")
        return f"${number:x}"
    lowered = text.lower()
    if lowered.startswith("0x"):
        body = text[2:]
        if not body or not HEX_RE.fullmatch(body):
            raise error("bad_option", f"Invalid hexadecimal value: {value!r}", "Use 0x followed by hex digits.")
        number = int(body, 16)
        if number > 0xFFFFFFFF:
            raise error("bad_option", "Register value must fit in 32 bits.", "Use 0..0xffffffff.")
        return f"${number:x}"
    if text.startswith("$"):
        body = text[1:]
        if not body or not HEX_RE.fullmatch(body):
            raise error("bad_option", f"Invalid hexadecimal value: {value!r}", "Use $ followed by hex digits.")
        if int(body, 16) > 0xFFFFFFFF:
            raise error("bad_option", "Register value must fit in 32 bits.", "Use 0..0xffffffff.")
        return "$" + body.lower()
    if re.search(r"\s", text):
        raise error(
            "bad_option",
            "Register value must be one token.",
            "Use an integer or a single debugger expression without spaces.",
        )
    return text


def register_assignment(register: str, value: int | str) -> tuple[str, str, str]:
    name = normalize_register(register)
    formatted = format_register_value(value)
    return f"cpureg {name}={formatted}", name, formatted


def validate_breakpoint_condition(condition: str) -> str:
    if not isinstance(condition, str):
        raise error("bad_option", "condition must be a string.", "Pass a conditional breakpoint expression.")
    text = condition.strip()
    if not text:
        raise error("bad_option", "Breakpoint condition must not be empty.", "Example: pc = $1234.")
    if any(char in text for char in ("\x00", "\n", "\r")) or any(ord(char) < 32 for char in text):
        raise error(
            "bad_option",
            "Breakpoint condition must be one line.",
            "Pass a single conditional expression.",
        )
    if not COMPARISON_RE.search(text):
        raise error(
            "bad_option",
            "Breakpoint condition has no comparison operator.",
            "Use a condition such as 'pc = $1234' or '(0x1234).w = 0xffff'.",
        )
    return text


def parse_breakpoint_id(raw: str) -> int | None:
    match = BREAKPOINT_ID_RE.search(raw)
    return int(match.group(1)) if match else None


def parse_pc(raw: str) -> int | None:
    match = PC_RE.search(raw)
    return int(match.group(1), 16) if match else None


def parse_find_pattern(pattern: str) -> list[int]:
    if not isinstance(pattern, str):
        raise error("bad_option", "pattern must be a string.", "Pass hex bytes such as '48 65' or '4865'.")
    text = pattern.strip()
    if not text:
        raise error("bad_option", "pattern must not be empty.", "Pass hex bytes such as '48 65' or '4865'.")
    if any(char in text for char in ("\x00", "\n", "\r")) or any(ord(char) < 32 for char in text):
        raise error("bad_option", "pattern must be one line.", "Pass hex bytes such as '48 65'.")
    if any(char.isspace() for char in text):
        parts = text.split()
    else:
        body = text[2:] if text.lower().startswith("0x") else text[1:] if text.startswith("$") else text
        if not HEX_RE.fullmatch(body) or len(body) % 2:
            raise error(
                "bad_option",
                f"Invalid byte pattern: {pattern!r}",
                "Use whitespace-separated hex bytes or an even-length hex string.",
            )
        parts = [body[index : index + 2] for index in range(0, len(body), 2)]
    values: list[int] = []
    for part in parts:
        body = part[2:] if part.lower().startswith("0x") else part[1:] if part.startswith("$") else part
        if not body or not re.fullmatch(r"[0-9A-Fa-f]{1,2}", body):
            raise error(
                "bad_option",
                f"Invalid byte pattern token: {part!r}",
                "Use hex bytes between 0x00 and 0xff.",
            )
        values.append(int(body, 16))
    if not values:
        raise error("bad_option", "pattern produced no bytes.", "Pass at least one hex byte.")
    return values


def format_find_pattern(pattern: str) -> list[str]:
    return [f"${value:02x}" for value in parse_find_pattern(pattern)]


def format_find_range(start: int | None, length: int | None) -> str:
    start_value = 0 if start is None else _address(start, "start")
    if length is None:
        return f"${start_value:x}"
    if isinstance(length, bool) or not isinstance(length, int):
        raise error("bad_option", "length must be an integer.", "Pass a positive byte count.")
    if length < 1 or length > MAX_FIND_SCAN:
        raise error(
            "bad_option",
            f"length must be between 1 and {MAX_FIND_SCAN}.",
            "Narrow the search range.",
        )
    end = start_value + length
    if end > 0xFFFFFFFF:
        raise error("bad_option", "address range exceeds the 32-bit address space.", "Narrow the range.")
    return f"${start_value:x}-${end:x}"


def parse_find_output(raw: str, max_hits: int = 20) -> dict[str, Any]:
    limit = max(1, min(int(max_hits), 1000))
    addresses: list[int] = []
    for line in raw.splitlines():
        match = FIND_ADDRESS_RE.match(line)
        if match:
            addresses.append(int(match.group(1), 16))
    count_match = FIND_COUNT_RE.search(raw)
    count = int(count_match.group(1)) if count_match else len(addresses)
    matches = addresses[:limit]
    return {
        "matches": matches,
        "addresses": [f"0x{address:08x}" for address in matches],
        "count": count,
        "parsed": len(addresses),
        "truncated": count > limit or len(addresses) > limit,
    }


def _debugger_error(raw: str) -> str | None:
    prefixes = (
        "ERROR",
        "Error,",
        "Illegal",
        "Invalid",
        "Bad ",
        "Cannot open",
        "Unknown",
    )
    for line in raw.splitlines():
        text = line.strip()
        if text.startswith(prefixes):
            return text
    return None


def _require_prompt(app: App, action: str) -> None:
    state = app.supervisor.state.value
    if state != "at_prompt":
        raise error(
            "not_stopped",
            f"Hatari is {state}; {action} requires the debugger prompt.",
            "Set a breakpoint and continue to it, or use dbg_breakpoint_wait.",
        )


async def _remove_breakpoint(app: App, breakpoint_id: int) -> bool:
    try:
        result = await app.supervisor.debug(f"breakpoint {breakpoint_id}", timeout_ms=3000)
    except HatariError:
        return False
    lowered = result["raw"].lower()
    if "no such" in lowered or "no (more)" in lowered:
        return True
    return _debugger_error(result["raw"]) is None


async def wait_for_breakpoint(app: App, condition: str, timeout_ms: int) -> dict[str, Any]:
    text = validate_breakpoint_condition(condition)
    if isinstance(timeout_ms, bool) or not isinstance(timeout_ms, int):
        raise error("bad_option", "timeout_ms must be an integer.", "Pass a positive timeout in milliseconds.")
    timeout_ms = max(1, min(timeout_ms, 30 * 60 * 1000))
    state = app.supervisor.state.value
    if state != "at_prompt":
        raise error(
            "not_stopped",
            f"Hatari is {state}; a temporary breakpoint needs the debugger prompt.",
            "Hit a breakpoint or exception first; remote-paused mode is not a reliable prompt.",
        )

    start = time.monotonic()
    stderr_start = app.supervisor.stderr_ring._next - 1
    before_exception = app.supervisor.exception
    set_result = await app.supervisor.debug(f"breakpoint {text} :once", timeout_ms=5000)
    breakpoint_id = parse_breakpoint_id(set_result["raw"])
    set_error = _debugger_error(set_result["raw"])
    if set_error or breakpoint_id is None:
        raise error(
            "breakpoint_failed",
            f"Hatari did not add the temporary breakpoint: {set_error or 'no breakpoint id in output'}",
            "Check the condition syntax with dbg_breakpoint_set.",
            raw=set_result["raw"],
        )

    await app.supervisor.debug("cont", channel="auto")

    timed_out = False
    deadline = start + timeout_ms / 1000.0
    state_now = app.supervisor.state.value
    while True:
        state_now = app.supervisor.state.value
        if state_now == "at_prompt":
            break
        if state_now in {"stopped", "stopping", "exited", "crashed", "failed"}:
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            timed_out = True
            break
        await asyncio.sleep(min(0.02, remaining))

    if timed_out:
        reason = "timeout"
    elif state_now in {"stopped", "stopping", "exited", "crashed", "failed"}:
        reason = "exited"
    else:
        exception = app.supervisor.exception
        if exception is not None and exception != before_exception:
            reason = "exception"
        else:
            reason = "breakpoint"

    pc: int | None = None
    if reason in {"breakpoint", "exception"}:
        try:
            registers = await app.supervisor.debug("cpureg", timeout_ms=3000)
            pc = parse_pc(registers["raw"])
        except HatariError:
            pc = None

    entries, _ = app.supervisor.stderr_ring.read(since=stderr_start, limit=200)
    removed = await _remove_breakpoint(app, breakpoint_id) if timed_out else True
    warnings = []
    if timed_out and not removed:
        warnings.append(
            f"Temporary breakpoint {breakpoint_id} could not be removed; "
            "delete it with dbg_breakpoint_delete after returning to the prompt."
        )
    return {
        "ok": True,
        "condition": text,
        "breakpoint_id": breakpoint_id,
        "reason": reason,
        "timed_out": timed_out,
        "breakpoint_removed": removed,
        "pc": pc,
        "exception": app.supervisor.exception,
        "state": app.supervisor.state.value,
        "duration_ms": int((time.monotonic() - start) * 1000),
        "lines": [entry.text for entry in entries],
        "warnings": warnings,
    }


def register(server: MCPServer, app: App) -> None:
    @server.tool(
        name="dbg_registers_set",
        description=(
            "Set one M68k CPU register with the stock debugger 'cpureg REG=value' command. "
            "Requires the debugger prompt; destructive to emulated CPU state."
        ),
        annotations=ToolAnnotations(destructiveHint=True, openWorldHint=False),
    )
    @guarded
    async def dbg_registers_set(register: str, value: int | str) -> dict[str, Any]:
        command, name, formatted = register_assignment(register, value)
        _require_prompt(app, "setting registers")
        result = await app.supervisor.debug(command, channel="stdin", timeout_ms=5000)
        debugger_error = _debugger_error(result["raw"])
        if debugger_error:
            code = "unsupported_register" if "usage" in debugger_error.lower() else "debugger_error"
            raise error(
                code,
                f"Hatari rejected register assignment: {debugger_error}",
                "Check the register name and whether the CPU level supports it.",
                raw=result["raw"],
            )
        return {**result, "register": name, "value": formatted}

    @server.tool(
        name="dbg_breakpoint_wait",
        description=(
            "Set a temporary ':once' breakpoint, continue, and wait for the debugger prompt, "
            "an exception, process exit, or timeout. The condition must contain a comparison."
        ),
        annotations=ToolAnnotations(destructiveHint=False, openWorldHint=False),
    )
    @guarded
    async def dbg_breakpoint_wait(condition: str, timeout_ms: int = 30000) -> dict[str, Any]:
        return await wait_for_breakpoint(app, condition, timeout_ms)

    @server.tool(
        name="dbg_memory_find",
        description=(
            "Find a contiguous byte pattern with the debugger 'find' command. "
            "Pattern is hex bytes ('48 65' or '4865'). Requires the debugger prompt."
        ),
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False),
    )
    @guarded
    async def dbg_memory_find(
        pattern: str,
        start: int | None = None,
        length: int | None = None,
        max_hits: int = 20,
    ) -> dict[str, Any]:
        _require_prompt(app, "memory search")
        tokens = format_find_pattern(pattern)
        range_text = format_find_range(start, length)
        command = f"find {range_text} {' '.join(tokens)}"
        result = await app.supervisor.debug(command, channel="stdin", timeout_ms=30000)
        debugger_error = _debugger_error(result["raw"])
        if debugger_error:
            raise error(
                "bad_option",
                f"Hatari rejected the memory search: {debugger_error}",
                "Check the address range and hex byte pattern.",
                raw=result["raw"],
            )
        return {
            **result,
            "command": command,
            "pattern": tokens,
            "range": range_text,
            **parse_find_output(result["raw"], max_hits),
        }

    @server.tool(
        name="dbg_memory_save",
        description="Save a guest memory region to a host file with the debugger savebin command.",
        annotations=ToolAnnotations(destructiveHint=False, openWorldHint=False),
    )
    @guarded
    async def dbg_memory_save(path: str, address: int, length: int) -> dict[str, Any]:
        target = app.guard_path(path)
        start = _address(address)
        if isinstance(length, bool) or not isinstance(length, int):
            raise error("bad_option", "length must be an integer.", "Pass a positive byte count.")
        if length < 1 or length > MAX_SAVE_LENGTH:
            raise error(
                "bad_option",
                f"length must be between 1 and {MAX_SAVE_LENGTH}.",
                "Save the region in smaller chunks.",
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        result = await app.supervisor.debug(
            f'savebin "{target}" ${start:x} {length}',
            timeout_ms=30000,
        )
        debugger_error = _debugger_error(result["raw"])
        if debugger_error:
            raise error(
                "save_failed",
                f"Hatari rejected savebin: {debugger_error}",
                "Check the target path and address.",
                raw=result["raw"],
            )
        if not target.is_file():
            raise error(
                "save_failed",
                "Hatari did not create the requested file.",
                "Check the target path and address.",
                raw=result["raw"],
            )
        digest = hashlib.sha256()
        with target.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        size = target.stat().st_size
        warnings = [] if size == length else [f"Saved {size} bytes, expected {length}."]
        return {
            **result,
            "path": str(target),
            "address": start,
            "length": length,
            "size": size,
            "size_matches": size == length,
            "sha256": digest.hexdigest(),
            "warnings": warnings,
        }

    @server.tool(
        name="dbg_exception_get",
        description=(
            "Return the last exception recorded by the supervisor without running a debugger "
            "command. Includes the current state and prompt flag."
        ),
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False),
    )
    @guarded
    async def dbg_exception_get() -> dict[str, Any]:
        status = app.supervisor.status()
        exception = status.get("exception")
        return {
            "ok": True,
            "exception": exception,
            "has_exception": exception is not None,
            "state": status.get("state"),
            "at_prompt": status.get("at_prompt"),
            "last_error": status.get("last_error"),
        }
