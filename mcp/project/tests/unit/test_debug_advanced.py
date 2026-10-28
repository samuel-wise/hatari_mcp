from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from hatari_mcp.errors import HatariError
from hatari_mcp.state import State
from hatari_mcp.tools import debug_advanced as da

EXPECTED_TOOLS = {
    "dbg_backtrace",
    "dbg_stack_dump",
    "dbg_profile_start",
    "dbg_profile_stop",
    "dbg_trace_start",
    "dbg_trace_stop",
    "dbg_history",
    "dbg_command_history_clear",
    "dbg_dsp_registers",
    "dbg_dsp_memory",
    "dbg_dsp_disassemble",
    "dbg_dsp_breakpoint_set",
    "dbg_dsp_breakpoint_list",
    "dbg_dsp_continue",
    "dbg_dsp_step",
    "dbg_state_diff",
    "dbg_breakpoint_actions",
}


class FakeServer:
    def __init__(self) -> None:
        self.tools: dict[str, Any] = {}

    def tool(self, name: str | None = None, description: str | None = None, annotations: Any = None):
        def decorator(fn: Any) -> Any:
            assert name is not None
            self.tools[name] = fn
            return fn

        return decorator


class FakeRuntime:
    path = Path("/tmp")


class FakeSupervisor:
    def __init__(self, payloads: list[bytes], cpureg: str = "A7 00001000  D0 00000000") -> None:
        self.state = State.AT_PROMPT
        self.runtime = FakeRuntime()
        self.payloads = list(payloads)
        self.cpureg = cpureg
        self.commands: list[str] = []

    async def debug(self, command: str, channel: str = "auto", timeout_ms: int | None = None) -> dict[str, Any]:
        self.commands.append(command)
        if command.startswith("savebin "):
            path = Path(command.split()[1])
            path.write_bytes(self.payloads.pop(0))
            return {"ok": True, "raw": "", "lines": []}
        if command == "cpureg":
            return {"ok": True, "raw": self.cpureg, "lines": []}
        return {"ok": True, "raw": "", "lines": []}


class FakeApp:
    def __init__(self, supervisor: FakeSupervisor) -> None:
        self.supervisor = supervisor
        self.active_profile = "falcon"

    def guard_path(self, path: str | Path, *, must_exist: bool = False) -> Path:
        resolved = Path(path)
        if must_exist and not resolved.exists():
            raise HatariError("invalid_path", "Path does not exist.")
        return resolved


def test_parse_backtrace_frames_errors_and_truncation() -> None:
    text = "\n".join(
        [
            "#0  0x00fc1234: _main +0x4",
            "#1  0x00fc4321: sub +0x10",
            "#2  0x00fc5000: 0x00fc5000",
            "ERROR: broken frame chain at 0x00fc6000 (parent 0x00fc5000).",
            "(stack trace truncated at 16 frames)",
        ]
    )
    parsed = da.parse_backtrace(text)
    assert parsed["frame_count"] == 3
    assert parsed["truncated"] is True
    assert parsed["source"] == "frame_pointer"
    assert parsed["frames"][0] == {
        "index": 0,
        "pc": 0xFC1234,
        "address": 0xFC1234,
        "hex": "0xfc1234",
        "symbol": "_main",
        "offset": 4,
        "text": "_main",
    }
    assert parsed["frames"][2]["symbol"] is None
    assert parsed["frames"][2]["offset"] is None
    assert parsed["errors"] == ["ERROR: broken frame chain at 0x00fc6000 (parent 0x00fc5000)."]


def test_parse_dsp_registers() -> None:
    text = "\n".join(
        [
            "                                |  SP   SSH    SSL",
            "A2: 01  A1: 000002  A0: 000003\t| 00:  0000   0000",
            "B2: 02  B1: 000004  B0: 000005\t| 01:  0000   0000",
            "        X1: 000006  X0: 000007\t| 02:  0000   0000",
            "        Y1: 000008  Y0: 000009\t| 03:  0000   0000",
            "R0: 0001   N0: 0002   M0: 0003\t| 04:  0000   0000",
            "R7: 0011   N7: 0012   M7: 0013\t| 05:  0000   0000",
            "LA: 0004   LC: 0005   PC: 0200\t| 06:  0000   0000",
            "SR: 0a00   OMR: 01    SP: 02\t| 07:  0000   0000",
        ]
    )
    registers = da.parse_dsp_registers(text)
    assert registers["a2"] == 0x01
    assert registers["a1"] == 0x02
    assert registers["b0"] == 0x05
    assert registers["x0"] == 0x07
    assert registers["y1"] == 0x08
    assert registers["r0"] == 0x01
    assert registers["r7"] == 0x11
    assert registers["n7"] == 0x12
    assert registers["m7"] == 0x13
    assert registers["la"] == 0x04
    assert registers["lc"] == 0x05
    assert registers["pc"] == 0x0200
    assert registers["sr"] == 0x0A00
    assert registers["omr"] == 0x01
    assert registers["sp"] == 0x02


def test_parse_dsp_memory_spaces_and_mapping() -> None:
    text = "\n".join(
        [
            "DSP memdump from 0x0200 in 'P' address space:",
            "P ram:0200  123456",
            "P ram:0201  654321",
            "X:1000 (P:1100): abcdef",
            "X periph:ffeb  HTX : 000000   RTX:000000",
        ]
    )
    values = da.parse_dsp_memory(text)
    assert values == [
        {"space": "P ram", "address": 0x0200, "value": 0x123456},
        {"space": "P ram", "address": 0x0201, "value": 0x654321},
        {"space": "X", "address": 0x1000, "value": 0xABCDEF, "p_address": 0x1100},
    ]


def test_parse_dsp_disassembly() -> None:
    text = "\n".join(
        [
            "DSP disasm 0x0200-0x0205:",
            "p:0200  123456         (02 cyc)  MOVE #$1,A",
            "p:0201  789abc def012  (04 cyc)  MOVE X:$10,A",
        ]
    )
    instructions = da.parse_dsp_disassembly(text)
    assert instructions == [
        {
            "address": 0x0200,
            "hex": "0x0200",
            "words": [0x123456],
            "cycles": 2,
            "text": "MOVE #$1,A",
        },
        {
            "address": 0x0201,
            "hex": "0x0201",
            "words": [0x789ABC, 0xDEF012],
            "cycles": 4,
            "text": "MOVE X:$10,A",
        },
    ]


def test_parse_history_cpu_dsp_reasons_and_repeats() -> None:
    text = "\n".join(
        [
            "$00fc1234 4e71                  NOP",
            "Debugger: *CPU breakpoint*",
            "p:0200  123456         (02 cyc)  MOVE #$1,A",
            "Debugger: *DSP breakpoint*",
            "Last item repeated 3 times.",
        ]
    )
    parsed = da.parse_history(text)
    assert parsed["count"] == 2
    assert parsed["items"][0] == {
        "processor": "cpu",
        "pc": 0xFC1234,
        "hex": "0xfc1234",
        "text": "4e71                  NOP",
        "reason": "CPU breakpoint",
    }
    assert parsed["items"][1]["processor"] == "dsp"
    assert parsed["items"][1]["reason"] == "DSP breakpoint"
    assert parsed["repeats"] == 3
    assert parsed["empty"] is False


def test_parse_history_empty() -> None:
    parsed = da.parse_history("No history items to show.")
    assert parsed["items"] == []
    assert parsed["empty"] is True


def test_parse_trace_selection() -> None:
    selection = da.parse_trace_selection("cpu_disasm,video_hbl")
    assert selection["expression"] == "cpu_disasm,video_hbl"
    assert selection["categories"] == ["cpu_disasm", "video_hbl"]
    assert selection["mode"] == "replace"

    modify = da.parse_trace_selection("+cpu_regs,-dsp_state")
    assert modify["expression"] == "+cpu_regs,-dsp_state"
    assert modify["mode"] == "modify"

    with pytest.raises(ValueError, match="unknown trace category"):
        da.parse_trace_selection("not_a_trace")
    with pytest.raises(ValueError, match="dbg_trace_stop"):
        da.parse_trace_selection("none")
    with pytest.raises(ValueError, match="refused"):
        da.parse_trace_selection("all")
    with pytest.raises(ValueError, match="too many"):
        da.parse_trace_selection(",".join(sorted(da.TRACE_CATEGORIES)[:17]))


def test_profile_enabled_parser() -> None:
    assert da.profile_enabled("Profiling enabled.") is True
    assert da.profile_enabled("Profiling disabled.") is False
    assert da.profile_enabled("ERROR: no CPU profiling data available!") is None


def test_diff_memory_changed_offsets_and_ranges() -> None:
    diff = da.diff_memory(b"\x00\x01\x02\x03\x04\x05", b"\x00\x09\x02\x08\x08\x05", base=0x100)
    assert diff["changed_count"] == 3
    assert diff["identical"] is False
    assert diff["truncated"] is False
    assert diff["changed"] == [
        {"offset": 1, "address": 0x101, "before": "0x01", "after": "0x09"},
        {"offset": 3, "address": 0x103, "before": "0x03", "after": "0x08"},
        {"offset": 4, "address": 0x104, "before": "0x04", "after": "0x08"},
    ]
    assert diff["ranges"] == [
        {"offset": 1, "address": 0x101, "length": 1},
        {"offset": 3, "address": 0x103, "length": 2},
    ]


def test_diff_memory_identical_and_length_mismatch() -> None:
    identical = da.diff_memory(b"\x01\x02", b"\x01\x02")
    assert identical["identical"] is True
    assert identical["changed_count"] == 0

    mismatch = da.diff_memory(b"\x01\x02\x03", b"\x01\x09")
    assert mismatch["length_mismatch"] is True
    assert mismatch["before_length"] == 3
    assert mismatch["after_length"] == 2
    assert mismatch["compared_length"] == 2
    assert mismatch["changed_count"] == 1


def test_diff_memory_caps_changed_entries_and_ranges() -> None:
    before = bytes(range(20))
    after = bytearray(before)
    for offset in range(0, 20, 2):
        after[offset] += 1
    diff = da.diff_memory(before, bytes(after), max_changes=5, max_ranges=2)
    assert diff["changed_count"] == 10
    assert len(diff["changed"]) == 5
    assert diff["truncated"] is True
    assert len(diff["ranges"]) == 2
    assert diff["ranges_truncated"] is True


def test_validate_condition_rejects_control_characters() -> None:
    assert da._validate_condition(" pc = $100 ") == "pc = $100"
    with pytest.raises(HatariError) as excinfo:
        da._validate_condition("pc = $100\nmemwrite 0 0")
    assert excinfo.value.code == "bad_option"
    with pytest.raises(HatariError):
        da._validate_condition("")


def test_register_exposes_expected_tools() -> None:
    server = FakeServer()
    da.register(server, FakeApp(FakeSupervisor([])))
    assert set(server.tools) == EXPECTED_TOOLS


def test_stack_dump_uses_a7_and_big_endian_words() -> None:
    supervisor = FakeSupervisor([b"\x12\x34\x56\x78\x9a\xbc\xde\xf0"])
    server = FakeServer()
    da.register(server, FakeApp(supervisor))
    result = asyncio.run(server.tools["dbg_stack_dump"](words=2))
    assert result["address"] == 0x1000
    assert result["address_source"] == "cpureg"
    assert result["words"] == [0x12345678, 0x9ABCDEF0]
    assert result["hex_words"] == ["0x12345678", "0x9abcdef0"]
    assert supervisor.commands[0] == "cpureg"
    parts = supervisor.commands[1].split()
    assert parts[0] == "savebin"
    assert parts[2:] == ["$1000", "0x8"]


def test_state_diff_baseline_then_compare_and_cleanup() -> None:
    supervisor = FakeSupervisor([b"\x01\x02\x03\x04", b"\x01\x09\x03\x08"])
    server = FakeServer()
    da.register(server, FakeApp(supervisor))
    try:
        first = asyncio.run(server.tools["dbg_state_diff"](address=0x100, length=4))
        assert first["baseline"] is True
        assert first["snapshot"]

        second = asyncio.run(
            server.tools["dbg_state_diff"](address=0x100, length=4, before=first["snapshot"])
        )
        assert second["baseline"] is False
        assert second["changed_count"] == 2
        assert [entry["offset"] for entry in second["changed"]] == [1, 3]
        assert second["ranges"] == [
            {"offset": 1, "address": 0x101, "length": 1},
            {"offset": 3, "address": 0x103, "length": 1},
        ]
    finally:
        for path in da._SNAPSHOTS.values():
            path.unlink(missing_ok=True)
        da._SNAPSHOTS.clear()


def test_trace_start_rejects_runtime_file() -> None:
    supervisor = FakeSupervisor([])
    server = FakeServer()
    da.register(server, FakeApp(supervisor))
    with pytest.raises(Exception) as excinfo:
        asyncio.run(
            server.tools["dbg_trace_start"](what="cpu_disasm", file="/tmp/hatari-trace.log")
        )
    assert "trace_file_unsupported" in str(excinfo.value)
    assert supervisor.commands == []


def test_breakpoint_actions_preview_and_excludes_info_file() -> None:
    supervisor = FakeSupervisor([])
    server = FakeServer()
    da.register(server, FakeApp(supervisor))
    result = asyncio.run(
        server.tools["dbg_breakpoint_actions"](
            address="$1234", print_text="hello", trace=True
        )
    )
    assert result["executed"] is False
    assert result["command"] == "breakpoint pc = $1234 :print hello :trace"
    assert supervisor.commands == []

    with pytest.raises(Exception) as excinfo:
        asyncio.run(
            server.tools["dbg_breakpoint_actions"](address="$1234", info="file", apply=True)
        )
    assert "Unsupported info" in str(excinfo.value)


def test_dsp_unavailable_error_is_typed() -> None:
    supervisor = FakeSupervisor([])
    supervisor.debug = lambda *args, **kwargs: _dsp_failure()
    server = FakeServer()
    app = FakeApp(supervisor)
    app.active_profile = "st"
    da.register(server, app)
    with pytest.raises(Exception) as excinfo:
        asyncio.run(server.tools["dbg_dsp_registers"]())
    assert "dsp_unavailable" in str(excinfo.value)


async def _dsp_failure() -> dict[str, Any]:
    return {"ok": True, "raw": "DSP isn't present or initialized.", "lines": []}
