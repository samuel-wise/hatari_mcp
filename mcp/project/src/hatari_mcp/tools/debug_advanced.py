"""Advanced debugger tools: backtrace, DSP, profiling, tracing and diffs."""

from __future__ import annotations

import os
import re
import struct
import tempfile
import uuid
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from ..app import App
from ..errors import error
from ..state import State
from ._common import guarded
from .debug import parse_registers

TRACE_CATEGORIES = frozenset(
    {
        "acia",
        "aes",
        "bios",
        "blitter",
        "cpu_all",
        "cpu_disasm",
        "cpu_exception",
        "cpu_pairing",
        "cpu_regs",
        "cpu_symbols",
        "cpu_video_cycles",
        "crossbar",
        "dmasound",
        "dsp_all",
        "dsp_disasm",
        "dsp_disasm_mem",
        "dsp_disasm_reg",
        "dsp_host_command",
        "dsp_host_interface",
        "dsp_host_ssi",
        "dsp_interrupt",
        "dsp_state",
        "dsp_symbols",
        "event",
        "fdc",
        "gemdos",
        "ide",
        "ikbd_acia",
        "ikbd_all",
        "ikbd_cmds",
        "ikbd_exec",
        "int",
        "io_all",
        "io_read",
        "io_write",
        "keymap",
        "mem",
        "mfp_all",
        "mfp_exception",
        "mfp_read",
        "mfp_start",
        "mfp_write",
        "midi",
        "midi_raw",
        "natfeats",
        "nvram",
        "os_all",
        "os_base",
        "psg_all",
        "psg_read",
        "psg_write",
        "scc",
        "scsi_cmd",
        "scsidrv",
        "scu",
        "vdi",
        "videl",
        "video_addr",
        "video_all",
        "video_border_h",
        "video_border_v",
        "video_color",
        "video_hbl",
        "video_res",
        "video_ste",
        "video_sync",
        "video_vbl",
        "xbios",
    }
)

INFO_ACTIONS = frozenset(
    {
        "acia",
        "aes",
        "basepage",
        "bios",
        "blitter",
        "cookiejar",
        "crossbar",
        "default",
        "disasm",
        "dmasnd",
        "dsp",
        "dspdisasm",
        "dspmemdump",
        "dspregs",
        "dta",
        "gemdos",
        "history",
        "ikbd",
        "memdump",
        "mfp",
        "mmu",
        "nvram",
        "osheader",
        "regaddr",
        "registers",
        "rtc",
        "scc",
        "scu",
        "vdi",
        "videl",
        "video",
        "xbios",
        "ym",
    }
)

DSP_REGISTERS = frozenset(
    {
        "a0",
        "a1",
        "a2",
        "b0",
        "b1",
        "b2",
        "la",
        "lc",
        "m0",
        "m1",
        "m2",
        "m3",
        "m4",
        "m5",
        "m6",
        "m7",
        "n0",
        "n1",
        "n2",
        "n3",
        "n4",
        "n5",
        "n6",
        "n7",
        "omr",
        "pc",
        "r0",
        "r1",
        "r2",
        "r3",
        "r4",
        "r5",
        "r6",
        "r7",
        "sp",
        "sr",
        "ssh",
        "ssl",
        "x0",
        "x1",
        "y0",
        "y1",
    }
)

DSP_UNAVAILABLE_MARKERS = (
    "dsp isn't present",
    "dsp not enabled",
    "dsp isn't available",
)

_SNAPSHOTS: dict[str, Path] = {}
_SNAPSHOT_LIMIT = 16

CPU_HISTORY_RE = re.compile(r"^\$([0-9A-Fa-f]+)\s+(.*)$")
DSP_HISTORY_RE = re.compile(r"^p:([0-9A-Fa-f]{4})\s+(.*)$")
BACKTRACE_RE = re.compile(
    r"^\s*#(\d+)\s+0x([0-9A-Fa-f]+):\s*(.*)$"
)
DSP_REGISTER_RE = re.compile(r"\b([A-Za-z][A-Za-z0-9]{0,2})\s*:\s*([0-9A-Fa-f]{1,8})\b")
DSP_MEMORY_RE = re.compile(
    r"^(?P<space>[A-Za-z][A-Za-z ]*?):(?P<address>[0-9A-Fa-f]{4})"
    r"(?:\s+\(P:(?P<p_address>[0-9A-Fa-f]{4})\))?(?::)?\s+"
    r"(?P<value>[0-9A-Fa-f]{6})\b"
)
DSP_DISASM_RE = re.compile(
    r"^p:([0-9A-Fa-f]{4})\s+([0-9A-Fa-f]{6})"
    r"(?:\s+([0-9A-Fa-f]{6}))?\s+\((\d+) cyc\)\s+(.*)$"
)


def _addr(value: int) -> str:
    return f"${value:x}"


def _validate_int_address(value: int, name: str = "address", maximum: int = 0xFFFFFFFF) -> int:
    value = int(value)
    if value < 0 or value > maximum:
        raise error(
            "bad_option",
            f"{name} must be between 0 and 0x{maximum:x}.",
            "Pass a guest address in the configured address space.",
        )
    return value


def _validate_condition(text: str, *, name: str = "condition", maximum: int = 512) -> str:
    text = text.strip()
    if not text:
        raise error("bad_option", f"{name} is empty.", "Pass a Hatari debugger expression.")
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in text):
        raise error(
            "bad_option",
            f"{name} contains control characters.",
            "Send a single-line expression without control characters.",
        )
    if len(text) > maximum:
        raise error(
            "bad_option",
            f"{name} is longer than {maximum} characters.",
            "Shorten the expression or split the operation.",
        )
    return text


def parse_trace_selection(what: str) -> dict[str, Any]:
    raw = what.strip()
    if not raw:
        raise ValueError("trace selection is empty")
    tokens = [token.strip() for token in raw.split(",") if token.strip()]
    if not tokens:
        raise ValueError("trace selection is empty")
    if len(tokens) > 16:
        raise ValueError("too many trace categories requested")
    categories: list[str] = []
    for token in tokens:
        prefix = token[0] if token[0] in "+-" else ""
        name = token[len(prefix) :].lower()
        if name == "help":
            raise ValueError("'help' is not a trace category")
        if name == "none":
            raise ValueError("use dbg_trace_stop() to disable tracing")
        if name == "all":
            raise ValueError("'all' is refused because it enables every trace category")
        if name not in TRACE_CATEGORIES:
            raise ValueError(f"unknown trace category: {name}")
        categories.append(f"{prefix}{name}")
    mode = "modify" if tokens[0][0] in "+-" else "replace"
    return {
        "expression": ",".join(categories),
        "categories": categories,
        "mode": mode,
    }


def parse_backtrace(text: str) -> dict[str, Any]:
    frames: list[dict[str, Any]] = []
    errors: list[str] = []
    truncated = False
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        match = BACKTRACE_RE.match(line)
        if match:
            index = int(match.group(1))
            address = int(match.group(2), 16)
            tail = match.group(3).strip()
            symbol: str | None = None
            offset: int | None = None
            target = tail
            if not tail.lower().startswith("0x"):
                split = tail.rsplit(" +0x", 1)
                symbol = split[0] if split else tail
                if len(split) == 2:
                    try:
                        offset = int(split[1], 16)
                    except ValueError:
                        offset = None
                target = symbol
            frames.append(
                {
                    "index": index,
                    "pc": address,
                    "address": address,
                    "hex": f"0x{address:x}",
                    "symbol": symbol,
                    "offset": offset,
                    "text": target,
                }
            )
            continue
        if stripped.startswith("ERROR:"):
            errors.append(stripped)
            continue
        if stripped.startswith("(stack trace truncated"):
            truncated = True
    return {
        "frames": frames,
        "errors": errors,
        "truncated": truncated,
        "frame_count": len(frames),
        "source": "frame_pointer",
        "confidence": "best_effort",
    }


def parse_dsp_registers(text: str) -> dict[str, int]:
    registers: dict[str, int] = {}
    for match in DSP_REGISTER_RE.finditer(text):
        name = match.group(1).lower()
        if name in DSP_REGISTERS:
            registers[name] = int(match.group(2), 16)
    return registers


def parse_dsp_memory(text: str) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    for line in text.splitlines():
        match = DSP_MEMORY_RE.match(line.strip())
        if not match:
            continue
        entry: dict[str, Any] = {
            "space": match.group("space").strip(),
            "address": int(match.group("address"), 16),
            "value": int(match.group("value"), 16),
        }
        if match.group("p_address"):
            entry["p_address"] = int(match.group("p_address"), 16)
        values.append(entry)
    return values


def parse_dsp_disassembly(text: str) -> list[dict[str, Any]]:
    instructions: list[dict[str, Any]] = []
    for line in text.splitlines():
        match = DSP_DISASM_RE.match(line.strip())
        if not match:
            continue
        words = [int(match.group(2), 16)]
        if match.group(3):
            words.append(int(match.group(3), 16))
        instructions.append(
            {
                "address": int(match.group(1), 16),
                "hex": f"0x{int(match.group(1), 16):04x}",
                "words": words,
                "cycles": int(match.group(4)),
                "text": match.group(5).strip(),
            }
        )
    return instructions


def parse_history(text: str) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    repeats = 0
    empty = False
    tracking: str | None = None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        cpu = CPU_HISTORY_RE.match(stripped)
        if cpu:
            items.append(
                {
                    "processor": "cpu",
                    "pc": int(cpu.group(1), 16),
                    "hex": f"0x{int(cpu.group(1), 16):x}",
                    "text": cpu.group(2),
                }
            )
            continue
        dsp = DSP_HISTORY_RE.match(stripped)
        if dsp:
            items.append(
                {
                    "processor": "dsp",
                    "pc": int(dsp.group(1), 16),
                    "hex": f"0x{int(dsp.group(1), 16):04x}",
                    "text": dsp.group(2),
                }
            )
            continue
        if stripped.startswith("Debugger:"):
            reason = stripped[len("Debugger:") :].strip().strip("*")
            if items:
                items[-1]["reason"] = reason
            continue
        if stripped.startswith("Last item repeated"):
            match = re.search(r"(\d+)", stripped)
            if match:
                repeats = int(match.group(1))
            continue
        if stripped.startswith("No history items"):
            empty = True
            continue
        if stripped.startswith("History tracking"):
            tracking = stripped
    return {
        "items": items,
        "count": len(items),
        "repeats": repeats,
        "empty": empty,
        "tracking": tracking,
        "completeness": "unknown",
    }


def profile_enabled(text: str) -> bool | None:
    lowered = text.lower()
    if "profiling enabled" in lowered:
        return True
    if "profiling disabled" in lowered:
        return False
    return None


def diff_memory(
    before: bytes,
    after: bytes,
    *,
    base: int = 0,
    max_changes: int = 4096,
    max_ranges: int = 1024,
) -> dict[str, Any]:
    common = min(len(before), len(after))
    changed: list[dict[str, Any]] = []
    ranges: list[dict[str, Any]] = []
    total = 0
    run_start: int | None = None
    for offset in range(common):
        old = before[offset]
        new = after[offset]
        if old == new:
            if run_start is not None:
                ranges.append(
                    {"offset": run_start, "address": base + run_start, "length": offset - run_start}
                )
                run_start = None
            continue
        total += 1
        if run_start is None:
            run_start = offset
        if len(changed) < max_changes:
            changed.append(
                {
                    "offset": offset,
                    "address": base + offset,
                    "before": f"0x{old:02x}",
                    "after": f"0x{new:02x}",
                }
            )
    if run_start is not None:
        ranges.append({"offset": run_start, "address": base + run_start, "length": common - run_start})
    ranges_truncated = len(ranges) > max_ranges
    return {
        "changed_count": total,
        "changed": changed,
        "ranges": ranges[:max_ranges],
        "truncated": total > len(changed),
        "ranges_truncated": ranges_truncated,
        "identical": total == 0,
        "before_length": len(before),
        "after_length": len(after),
        "length_mismatch": len(before) != len(after),
        "compared_length": common,
    }


def _dsp_unavailable(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in DSP_UNAVAILABLE_MARKERS)


def _dsp_error(text: str) -> None:
    if _dsp_unavailable(text):
        raise error(
            "dsp_unavailable",
            "DSP emulation is not available in this Hatari session.",
            "Use a Falcon profile built with DSP emulation (--dsp emu), or omit DSP tools.",
            raw=text,
        )


async def _require_dsp(app: App) -> None:
    profile = (app.active_profile or "").lower()
    if profile and profile != "falcon":
        raise error(
            "dsp_unavailable",
            "DSP emulation is only enabled on Falcon machines.",
            "Start Hatari with a Falcon profile and --dsp emu.",
        )
    result = await app.supervisor.debug("dspreg")
    _dsp_error(result["raw"])


def _tmp_path(prefix: str) -> Path:
    handle, name = tempfile.mkstemp(prefix=prefix, suffix=".bin", dir="/tmp")
    os.close(handle)
    return Path(name)


async def _save_memory(app: App, address: int, length: int) -> tuple[Path, bytes]:
    if not app.supervisor.runtime:
        raise error("not_running", "Hatari has no runtime directory.", "Call emu_start first.")
    path = _tmp_path("hatari-mcp-mem-")
    try:
        result = await app.supervisor.debug(f"savebin {path} {_addr(address)} 0x{length:x}")
    except Exception:
        path.unlink(missing_ok=True)
        raise
    if not path.is_file():
        raise error(
            "parse_failed",
            "Hatari did not create the memory dump file.",
            "Check the address.",
            raw=result.get("raw"),
        )
    data = path.read_bytes()
    if len(data) != length:
        path.unlink(missing_ok=True)
        raise error(
            "parse_failed",
            f"savebin returned {len(data)} bytes, expected {length}.",
            "Check that the address range is readable guest memory.",
        )
    return path, data


def _store_snapshot(path: Path) -> str:
    token = uuid.uuid4().hex
    _SNAPSHOTS[token] = path
    while len(_SNAPSHOTS) > _SNAPSHOT_LIMIT:
        oldest = next(iter(_SNAPSHOTS))
        old_path = _SNAPSHOTS.pop(oldest)
        old_path.unlink(missing_ok=True)
    return token


def _snapshot_at(token: str) -> Path:
    path = _SNAPSHOTS.get(token)
    if path is not None and path.is_file():
        return path
    candidate = Path(token)
    if (
        candidate.is_absolute()
        and candidate.parent == Path("/tmp")
        and candidate.name.startswith("hatari-mcp-mem-")
        and candidate.is_file()
    ):
        return candidate
    raise error(
        "unknown_snapshot",
        "The previous snapshot does not exist.",
        "Run dbg_state_diff without before to create a baseline snapshot.",
    )


async def _dsp_continue(app: App) -> dict[str, Any]:
    supervisor = app.supervisor
    if supervisor.state.value != "at_prompt":
        raise error(
            "not_stopped",
            "DSP continue requires the debugger prompt.",
            "Stop at a breakpoint before resuming the DSP.",
        )
    terminal = getattr(supervisor, "_terminal_command", None)
    if callable(terminal):
        await terminal("dspcont", use_stdin=True)
    else:
        write = getattr(supervisor, "_write_stdin", None)
        if not callable(write):
            raise error("internal_error", "No stdin channel is available.", "Call emu_start first.")
        await write("dspcont\n")
        supervisor.state = State.RUNNING
    return {
        "ok": True,
        "raw": "",
        "lines": [],
        "channel": "stdin",
        "state": supervisor.state.value,
        "timed_out": False,
        "command": "dspcont",
        "processor": "dsp",
    }


def _breakpoint_expression(address: str, type: str, condition: str | None) -> str:
    if type not in {"exec", "mem", "reg"}:
        raise error("bad_option", f"Invalid breakpoint type: {type}", "Use exec, mem or reg.")
    if condition:
        return _validate_condition(condition)
    if type != "exec":
        raise error(
            "bad_option",
            "mem/reg breakpoints require an explicit condition.",
            "Example: condition='(0x1234).w = 0xffff'.",
        )
    address = address.strip()
    if not address:
        raise error("bad_option", "address is empty.", "Pass an address or use condition=...")
    value = address if not address.startswith("0x") else "$" + address[2:]
    if re.fullmatch(r"\d+", value):
        value = "$" + value
    return f"pc = {value}"


def register(server: MCPServer, app: App) -> None:
    @server.tool(
        name="dbg_backtrace",
        description=(
            "Walk Hatari's A6 frame-pointer chain. This is a best-effort backtrace, not a "
            "profiler reconstruction, and needs a valid frame chain plus symbols for names."
        ),
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False),
    )
    @guarded
    async def dbg_backtrace(max_frames: int = 16, frame_pointer: int | None = None) -> dict[str, Any]:
        max_frames = max(1, min(int(max_frames), 256))
        command = f"backtrace {max_frames}"
        if frame_pointer is not None:
            fp = _validate_int_address(frame_pointer, "frame_pointer")
            command += f" {_addr(fp)}"
        result = await app.supervisor.debug(command)
        parsed = parse_backtrace(result["raw"])
        return {**result, **parsed, "max_frames": max_frames, "frame_pointer": frame_pointer}

    @server.tool(
        name="dbg_stack_dump",
        description=(
            "Dump 32-bit big-endian words from guest memory. When address is omitted, A7 from "
            "cpureg is used. Bytes come from savebin, not from formatted memdump text."
        ),
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False),
    )
    @guarded
    async def dbg_stack_dump(address: int | None = None, words: int = 32) -> dict[str, Any]:
        words = int(words)
        if words < 1 or words > 4096:
            raise error("bad_option", "words must be between 1 and 4096.", "Use a smaller dump.")
        source = "argument"
        if address is None:
            result = await app.supervisor.debug("cpureg")
            registers = parse_registers(result["raw"])
            if "a7" not in registers:
                raise error(
                    "parse_failed",
                    "Could not read A7 from cpureg output.",
                    "Pass address=... explicitly.",
                    raw=result["raw"],
                )
            address = registers["a7"]
            source = "cpureg"
        address = _validate_int_address(address)
        length = words * 4
        path = None
        try:
            path, data = await _save_memory(app, address, length)
            values = list(struct.unpack(f">{words}I", data))
        finally:
            if path is not None:
                path.unlink(missing_ok=True)
        return {
            "ok": True,
            "address": address,
            "hex_address": f"0x{address:x}",
            "address_source": source,
            "words": values,
            "hex_words": [f"0x{value:08x}" for value in values],
            "count": len(values),
        }

    @server.tool(
        name="dbg_profile_start",
        description=(
            "Enable CPU profiling with 'profile on', or DSP profiling with 'dspprofile on'. "
            "Hatari does not expose a profiling-state query; the result reflects the command."
        ),
        annotations=ToolAnnotations(destructiveHint=False),
    )
    @guarded
    async def dbg_profile_start(cpu: bool = True) -> dict[str, Any]:
        if cpu:
            command = "profile on"
            processor = "cpu"
        else:
            await _require_dsp(app)
            command = "dspprofile on"
            processor = "dsp"
        result = await app.supervisor.debug(command)
        enabled = profile_enabled(result["raw"])
        return {
            **result,
            "processor": processor,
            "enabled": enabled,
            "command": command,
            "status_note": "Stock Hatari has no profiling-state query.",
        }

    @server.tool(
        name="dbg_profile_stop",
        description="Disable CPU profiling with 'profile off', or DSP profiling with 'dspprofile off'.",
        annotations=ToolAnnotations(destructiveHint=False),
    )
    @guarded
    async def dbg_profile_stop(cpu: bool = True) -> dict[str, Any]:
        if cpu:
            command = "profile off"
            processor = "cpu"
        else:
            await _require_dsp(app)
            command = "dspprofile off"
            processor = "dsp"
        result = await app.supervisor.debug(command)
        enabled = profile_enabled(result["raw"])
        return {
            **result,
            "processor": processor,
            "enabled": enabled,
            "command": command,
            "status_note": "Stock Hatari has no profiling-state query.",
        }

    @server.tool(
        name="dbg_trace_start",
        description=(
            "Enable selected Hatari trace categories with the debugger trace command. "
            "The stock trace command has no file argument; --trace-file is launch-time only."
        ),
        annotations=ToolAnnotations(destructiveHint=False),
    )
    @guarded
    async def dbg_trace_start(what: str, file: str | None = None) -> dict[str, Any]:
        try:
            selection = parse_trace_selection(what)
        except ValueError as exc:
            raise error(
                "bad_option",
                str(exc),
                "Use comma-separated trace categories from Hatari's 'trace help'.",
            ) from exc
        if file is not None:
            target = app.guard_path(file)
            raise error(
                "trace_file_unsupported",
                "The stock debugger's trace command cannot redirect trace output to a file.",
                "Restart Hatari with --trace-file for the desired path; runtime trace stays on stderr.",
                extra={"file": str(target)},
            )
        result = await app.supervisor.debug(f"trace {selection['expression']}")
        return {
            **result,
            "requested": what,
            "categories": selection["categories"],
            "mode": selection["mode"],
            "file": None,
        }

    @server.tool(
        name="dbg_trace_stop",
        description="Disable all Hatari trace categories with 'trace none'.",
        annotations=ToolAnnotations(destructiveHint=False),
    )
    @guarded
    async def dbg_trace_stop() -> dict[str, Any]:
        result = await app.supervisor.debug("trace none")
        return {**result, "categories": [], "command": "trace none"}

    @server.tool(
        name="dbg_history",
        description=(
            "Read the last N debugger PC history entries. History must have been enabled before "
            "the event. Stock Hatari only prints entries not shown by an earlier history read."
        ),
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False),
    )
    @guarded
    async def dbg_history(
        limit: int = 20,
        enable: bool = False,
        processor: str = "both",
    ) -> dict[str, Any]:
        limit = max(1, min(int(limit), 1024))
        processor = processor.lower()
        if processor not in {"cpu", "dsp", "both"}:
            raise error("bad_option", f"Invalid processor: {processor}", "Use cpu, dsp or both.")
        enable_raw = None
        if enable:
            if processor == "dsp":
                await _require_dsp(app)
            selection = {"cpu": "cpu", "dsp": "dsp", "both": "on"}[processor]
            ring = max(64, limit)
            enable_result = await app.supervisor.debug(f"history {selection} {ring}")
            enable_raw = enable_result.get("raw")
        result = await app.supervisor.debug(f"history {limit}")
        parsed = parse_history(result["raw"])
        return {
            **result,
            **parsed,
            "limit": limit,
            "enable_raw": enable_raw,
            "note": "Hatari prints only history entries that were not shown before.",
        }

    @server.tool(
        name="dbg_command_history_clear",
        description="Report that stock Hatari has no command-history clear operation.",
        annotations=ToolAnnotations(destructiveHint=False),
    )
    @guarded
    async def dbg_command_history_clear() -> dict[str, Any]:
        raise error(
            "unsupported",
            "Stock Hatari has no debugger command-history clear command.",
            "Re-enable history with a different type or limit; History_Enable reallocates and "
            "zeroes the buffer, but there is no dedicated clear operation.",
        )

    @server.tool(
        name="dbg_dsp_registers",
        description="Read DSP registers with dspreg. Requires a Falcon build with DSP emulation.",
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False),
    )
    @guarded
    async def dbg_dsp_registers() -> dict[str, Any]:
        result = await app.supervisor.debug("dspreg")
        _dsp_error(result["raw"])
        return {**result, "registers": parse_dsp_registers(result["raw"]), "processor": "dsp"}

    @server.tool(
        name="dbg_dsp_memory",
        description=(
            "Dump DSP memory with dspmemdump. DSP memory is 24-bit words in P, X or Y space; "
            "the size argument is accepted for API symmetry and does not change the element width."
        ),
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False),
    )
    @guarded
    async def dbg_dsp_memory(address: int, count: int = 32, size: str = "l", space: str = "P") -> dict[str, Any]:
        address = _validate_int_address(address, maximum=0xFFFF)
        count = int(count)
        if count < 1 or count > 4096:
            raise error("bad_option", "count must be between 1 and 4096.", "Use a smaller range.")
        size = size.lower()
        if size not in {"b", "w", "l"}:
            raise error("bad_option", f"Invalid size: {size}", "Use b, w or l.")
        space = space.upper()
        if space not in {"P", "X", "Y"}:
            raise error("bad_option", f"Invalid DSP space: {space}", "Use P, X or Y.")
        end = min(0xFFFF, address + count - 1)
        result = await app.supervisor.debug(f"dspmemdump {space} {_addr(address)}-{_addr(end)}")
        _dsp_error(result["raw"])
        values = parse_dsp_memory(result["raw"])
        return {
            **result,
            "space": space,
            "address": address,
            "requested_count": count,
            "count": len(values),
            "size": size,
            "element_bits": 24,
            "size_note": "DSP dumps are fixed 24-bit words; size does not change element width.",
            "values": values,
        }

    @server.tool(
        name="dbg_dsp_disassemble",
        description="Disassemble DSP instructions with dspdisasm. Requires Falcon DSP emulation.",
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False),
    )
    @guarded
    async def dbg_dsp_disassemble(address: int, count: int = 10) -> dict[str, Any]:
        address = _validate_int_address(address, maximum=0xFFFF)
        count = int(count)
        if count < 1 or count > 200:
            raise error("bad_option", "count must be between 1 and 200.", "Use a smaller count.")
        end = min(0xFFFF, address + count * 3 - 1)
        result = await app.supervisor.debug(f"dspdisasm {_addr(address)}-{_addr(end)}")
        _dsp_error(result["raw"])
        instructions = parse_dsp_disassembly(result["raw"])[:count]
        return {
            **result,
            "address": address,
            "requested_count": count,
            "count": len(instructions),
            "instructions": instructions,
            "range_end": end,
        }

    @server.tool(
        name="dbg_dsp_breakpoint_set",
        description=(
            "Set a conditional DSP breakpoint with dspbreak. DSP memory addresses need a P, X "
            "or Y space; only R0-R7 can be used for indirect memory addressing."
        ),
        annotations=ToolAnnotations(destructiveHint=True),
    )
    @guarded
    async def dbg_dsp_breakpoint_set(condition: str) -> dict[str, Any]:
        expression = _validate_condition(condition)
        result = await app.supervisor.debug(f"dspbreak {expression}")
        _dsp_error(result["raw"])
        return {**result, "expression": expression, "processor": "dsp"}

    @server.tool(
        name="dbg_dsp_breakpoint_list",
        description="List DSP breakpoints with dspbreak. Requires Falcon DSP emulation.",
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False),
    )
    @guarded
    async def dbg_dsp_breakpoint_list() -> dict[str, Any]:
        await _require_dsp(app)
        result = await app.supervisor.debug("dspbreak")
        _dsp_error(result["raw"])
        return {**result, "processor": "dsp"}

    @server.tool(
        name="dbg_dsp_continue",
        description="Continue DSP emulation with dspcont. Leaves the debugger prompt; no sentinel is sent.",
        annotations=ToolAnnotations(destructiveHint=False),
    )
    @guarded
    async def dbg_dsp_continue() -> dict[str, Any]:
        await _require_dsp(app)
        return await _dsp_continue(app)

    @server.tool(
        name="dbg_dsp_step",
        description="Single-step the DSP with dspstep. Requires the debugger prompt.",
        annotations=ToolAnnotations(destructiveHint=False),
    )
    @guarded
    async def dbg_dsp_step() -> dict[str, Any]:
        if app.supervisor.state.value != "at_prompt":
            raise error(
                "not_stopped",
                "DSP stepping requires the debugger prompt.",
                "Set a DSP or CPU breakpoint, then continue until Hatari stops.",
            )
        await _require_dsp(app)
        result = await app.supervisor.debug("dspstep", channel="stdin")
        _dsp_error(result["raw"])
        return {**result, "processor": "dsp"}

    @server.tool(
        name="dbg_state_diff",
        description=(
            "Snapshot guest memory with savebin and compare it with a previous snapshot. "
            "Without before, this stores a baseline and returns a snapshot token."
        ),
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False),
    )
    @guarded
    async def dbg_state_diff(address: int, length: int, before: str | None = None) -> dict[str, Any]:
        address = _validate_int_address(address)
        length = int(length)
        if length < 1 or length > 1_048_576:
            raise error(
                "bad_option",
                "length must be between 1 and 1048576.",
                "Diff smaller memory regions.",
            )
        path, after = await _save_memory(app, address, length)
        token = _store_snapshot(path)
        if before is None:
            return {
                "ok": True,
                "baseline": True,
                "address": address,
                "length": length,
                "snapshot": token,
                "snapshot_path": str(path),
                "changed": [],
                "changed_count": 0,
                "identical": True,
                "truncated": False,
                "note": "Baseline stored; call again with before=<snapshot> to compare.",
            }
        before_path = _snapshot_at(before)
        before_data = before_path.read_bytes()
        diff = diff_memory(before_data, after, base=address)
        return {
            "ok": True,
            "baseline": False,
            "address": address,
            "length": length,
            "snapshot": token,
            "snapshot_path": str(path),
            "before_snapshot": before,
            **diff,
        }

    @server.tool(
        name="dbg_breakpoint_actions",
        description=(
            "Validate a breakpoint action suffix (:print, :info, :trace, :once, :quiet, :file) "
            "and preview the command. :file is never executed; safe actions run only with apply=true."
        ),
        annotations=ToolAnnotations(destructiveHint=True),
    )
    @guarded
    async def dbg_breakpoint_actions(
        address: str,
        type: str = "exec",
        condition: str | None = None,
        print_text: str | None = None,
        info: str | None = None,
        trace: bool = False,
        once: bool = False,
        quiet: bool = False,
        file: str | None = None,
        apply: bool = False,
    ) -> dict[str, Any]:
        expression = _breakpoint_expression(address, type, condition)
        suffix = ""
        warnings: list[str] = []
        if print_text is not None:
            if not print_text or any(ord(char) < 0x20 or ord(char) == 0x7F for char in print_text):
                raise error(
                    "bad_option",
                    "print_text must be non-empty and contain no control characters.",
                    "Use a short literal string.",
                )
            if " :" in print_text or len(print_text) > 200:
                raise error(
                    "bad_option",
                    "print_text is too long or contains an option separator.",
                    "Keep it under 200 characters and avoid ' :'.",
                )
            suffix += f" :print {print_text}"
        if info is not None:
            info = info.lower()
            if info not in INFO_ACTIONS:
                raise error(
                    "bad_option",
                    f"Unsupported info action: {info}",
                    "Use an allowlisted info subject; :file is deliberately excluded.",
                )
            suffix += f" :info {info}"
        if trace:
            suffix += " :trace"
        if once:
            suffix += " :once"
        if quiet:
            suffix += " :quiet"
        file_path: str | None = None
        if file is not None:
            file_path = str(app.guard_path(file, must_exist=True))
            suffix += f" :file {file_path}"
            warnings.append(":file actions are previewed only and are never executed by this tool.")
        if not suffix:
            raise error(
                "bad_option",
                "No breakpoint action was requested.",
                "Pass print_text, info, trace, once, quiet or file.",
            )
        command = f"breakpoint {expression}{suffix}"
        executed = False
        result: dict[str, Any] = {}
        if apply and file is None:
            result = await app.supervisor.debug(command)
            executed = True
        return {
            **result,
            "ok": True,
            "command": command,
            "expression": expression,
            "actions": {
                "print": print_text,
                "info": info,
                "trace": trace,
                "once": once,
                "quiet": quiet,
                "file": file_path,
            },
            "executed": executed,
            "warnings": warnings,
        }
