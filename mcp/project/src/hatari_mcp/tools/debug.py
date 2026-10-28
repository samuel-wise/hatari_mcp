"""Debugger inspection, symbols, breakpoints and execution control."""

from __future__ import annotations

import re
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from ..app import App
from ..errors import error
from ._common import guarded

REG_RE = re.compile(r"\b([DA][0-7])\s+([0-9A-Fa-f]{8})\b")
USP_RE = re.compile(r"\bUSP\s+([0-9A-Fa-f]{8})\b")
ISP_RE = re.compile(r"\bISP\s+([0-9A-Fa-f]{8})\b")
SR_RE = re.compile(r"\bSR=([0-9A-Fa-f]{4})\b")
PC_RE = re.compile(r"Next PC:\s*([0-9A-Fa-f]+)", re.IGNORECASE)
VALUE_RE = re.compile(r"(?:=|\$)\s*([0-9A-Fa-f]+)")


def parse_registers(text: str) -> dict[str, Any]:
    registers: dict[str, Any] = {}
    for match in REG_RE.finditer(text):
        registers[match.group(1).lower()] = int(match.group(2), 16)
    usp = USP_RE.search(text)
    isp = ISP_RE.search(text)
    sr = SR_RE.search(text)
    pc = PC_RE.search(text)
    if usp:
        registers["usp"] = int(usp.group(1), 16)
    if isp:
        registers["isp"] = int(isp.group(1), 16)
    if sr:
        registers["sr"] = int(sr.group(1), 16)
    if pc:
        registers["pc"] = int(pc.group(1), 16)
    return registers


def parse_value(text: str) -> int | None:
    matches = VALUE_RE.findall(text)
    if not matches:
        return None
    try:
        return int(matches[-1], 16)
    except ValueError:
        return None


def register(server: MCPServer, app: App) -> None:
    @server.tool(
        name="dbg_command",
        description=(
            "Send one raw Hatari debugger command. channel=auto uses stdin at the debugger prompt "
            "and the control socket while running. This is privileged: debugger commands can write "
            "memory, run scripts, and control the emulator."
        ),
        annotations=ToolAnnotations(destructiveHint=True, openWorldHint=False),
    )
    @guarded
    async def dbg_command(
        command: str,
        channel: str = "auto",
        timeout_ms: int | None = None,
    ) -> dict[str, Any]:
        return await app.supervisor.debug(command, channel=channel, timeout_ms=timeout_ms)

    @server.tool(
        name="dbg_registers_get",
        description="Read the M68k register file with Hatari's cpureg command.",
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False),
    )
    @guarded
    async def dbg_registers_get() -> dict[str, Any]:
        result = await app.supervisor.debug("cpureg")
        return {**result, "registers": parse_registers(result["raw"])}

    @server.tool(
        name="dbg_evaluate",
        description="Evaluate a Hatari debugger expression (read-only).",
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False),
    )
    @guarded
    async def dbg_evaluate(expression: str) -> dict[str, Any]:
        if "\n" in expression or "\r" in expression:
            raise error("bad_option", "Expression must be one line.", "Send a single expression.")
        result = await app.supervisor.debug(f"evaluate {expression}")
        value = parse_value(result["raw"])
        return {
            **result,
            "expression": expression,
            "value": value,
            "hex": f"0x{value:x}" if value is not None else None,
            "dec": value,
        }

    @server.tool(
        name="dbg_disassemble",
        description="Disassemble a range starting at address. Hatari takes a range, not a count.",
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False),
    )
    @guarded
    async def dbg_disassemble(address: str, count: int = 10) -> dict[str, Any]:
        count = max(1, min(int(count), 200))
        try:
            start = int(address, 0)
        except ValueError:
            start = None
        if start is None:
            result = await app.supervisor.debug(f"disasm {address}")
        else:
            end = start + count * 16
            result = await app.supervisor.debug(f"disasm ${start:x}-${end:x}")
        lines = result["lines"][:count]
        return {**result, "address": address, "count": count, "instructions": lines}

    @server.tool(
        name="dbg_symbols_load",
        description=(
            "Load symbols with Hatari's symbols command. Use prg=True to load the running "
            "GEMDOS program's symbols, or pass an ELF/a.out/DRI/GST/nm .sym file."
        ),
        annotations=ToolAnnotations(destructiveHint=False),
    )
    @guarded
    async def dbg_symbols_load(
        path: str | None = None,
        prg: bool = False,
        offset: str | None = None,
    ) -> dict[str, Any]:
        if prg:
            command = "symbols prg"
        elif path:
            command = f'symbols "{path}"'
        else:
            raise error("bad_option", "Pass path=... or prg=True.", "Choose a symbol source.")
        if offset:
            command += f" {offset}"
        result = await app.supervisor.debug(command)
        return {**result, "loaded": "error" not in result["raw"].lower()}

    @server.tool(
        name="dbg_symbols_list",
        description="List loaded symbols. kind is code, data or name; filter is an optional find string.",
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False),
    )
    @guarded
    async def dbg_symbols_list(kind: str = "code", filter: str = "") -> dict[str, Any]:
        if kind not in {"code", "data", "name"}:
            raise error("bad_option", f"Invalid symbol kind: {kind}", "Use code, data or name.")
        command = f"symbols {kind}"
        if filter:
            command += f" {filter}"
        result = await app.supervisor.debug(command)
        return {**result, "kind": kind, "filter": filter}

    @server.tool(
        name="dbg_breakpoint_set",
        description=(
            "Set a conditional breakpoint. type=exec maps address to pc = $address. "
            "Hatari has no enable/disable: delete and re-add instead."
        ),
        annotations=ToolAnnotations(destructiveHint=True),
    )
    @guarded
    async def dbg_breakpoint_set(
        address: str,
        type: str = "exec",
        condition: str | None = None,
        once: bool = True,
        quiet: bool = False,
        count: int | None = None,
    ) -> dict[str, Any]:
        if type not in {"exec", "mem", "reg"}:
            raise error("bad_option", f"Invalid breakpoint type: {type}", "Use exec, mem or reg.")
        if condition:
            expr = condition
        elif type == "exec":
            value = address if not address.startswith("0x") else "$" + address[2:]
            if re.fullmatch(r"\d+", value):
                value = "$" + value
            expr = f"pc = {value}"
        else:
            raise error(
                "bad_option",
                "mem/reg breakpoints require an explicit condition.",
                "Example: condition='(0x1234).w = 0xffff'.",
            )
        suffix = ""
        if count:
            suffix += f" :{int(count)}"
        if once:
            suffix += " :once"
        if quiet:
            suffix += " :quiet"
        result = await app.supervisor.debug(f"breakpoint {expr}{suffix}")
        return {**result, "expression": expr, "type": type}

    @server.tool(
        name="dbg_breakpoint_list",
        description="List active breakpoints.",
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False),
    )
    @guarded
    async def dbg_breakpoint_list() -> dict[str, Any]:
        return await app.supervisor.debug("breakpoint")

    @server.tool(
        name="dbg_breakpoint_delete",
        description="Delete a breakpoint by 1-based index, or all with index=0.",
        annotations=ToolAnnotations(destructiveHint=True),
    )
    @guarded
    async def dbg_breakpoint_delete(index: int) -> dict[str, Any]:
        if index < 0:
            raise error("bad_option", "Breakpoint index must be >= 0.", "Use 0 for all or a 1-based index.")
        return await app.supervisor.debug("breakpoint all" if index == 0 else f"breakpoint {index}")

    @server.tool(
        name="dbg_continue",
        description="Continue emulation. At the debugger prompt this uses stdin and leaves the prompt.",
        annotations=ToolAnnotations(destructiveHint=False),
    )
    @guarded
    async def dbg_continue() -> dict[str, Any]:
        return await app.supervisor.debug("cont", channel="auto")

    @server.tool(
        name="dbg_step",
        description="Step one CPU instruction. Requires the debugger prompt in this build.",
        annotations=ToolAnnotations(destructiveHint=False),
    )
    @guarded
    async def dbg_step() -> dict[str, Any]:
        if app.supervisor.state.value != "at_prompt":
            raise error(
                "not_stopped",
                "Stepping requires the debugger prompt.",
                "Set a breakpoint, then use dbg_continue to stop at it.",
            )
        return await app.supervisor.debug("step", channel="stdin")

    @server.tool(
        name="dbg_step_over",
        description="Step over one CPU instruction. Requires the debugger prompt in this build.",
        annotations=ToolAnnotations(destructiveHint=False),
    )
    @guarded
    async def dbg_step_over() -> dict[str, Any]:
        if app.supervisor.state.value != "at_prompt":
            raise error(
                "not_stopped",
                "Stepping requires the debugger prompt.",
                "Set a breakpoint, then use dbg_continue to stop at it.",
            )
        return await app.supervisor.debug("next", channel="stdin")
