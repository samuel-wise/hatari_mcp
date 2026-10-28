"""Unit tests for machine, extended debugger and watchpoint helpers."""

from __future__ import annotations

import pytest

from hatari_mcp.errors import HatariError
from hatari_mcp.tools import debug_extra, machine, memory_extra


@pytest.mark.parametrize(
    "name",
    ["memauto", "memsave", "midiin", "midiout", "printout", "soundout", "rs232in", "rs232out",
     "sccain", "sccaout", "sccalanin", "sccalanout", "sccbin", "sccbout"],
)
def test_validate_path_name_accepts_documented_names(name: str) -> None:
    assert machine.validate_path_name(name) == name
    assert machine.validate_path_name(f"  {name.upper()}  ") == name


@pytest.mark.parametrize("name", ["", "disk", "rs232", "memauto extra", "scc"])
def test_validate_path_name_rejects_unknown_names(name: str) -> None:
    with pytest.raises(HatariError) as excinfo:
        machine.validate_path_name(name)
    assert excinfo.value.code == "bad_option"


@pytest.mark.parametrize("name", ["printer", "rs232", "scca", "sccalan", "sccb", "midi"])
def test_validate_device_accepts_documented_names(name: str) -> None:
    assert machine.validate_device(name) == name


@pytest.mark.parametrize("name", ["", "disk", "midi2", "scc"])
def test_validate_device_rejects_unknown_names(name: str) -> None:
    with pytest.raises(HatariError):
        machine.validate_device(name)


@pytest.mark.parametrize("action", ["enable", "disable", "toggle", " TOGGLE "])
def test_validate_device_action(action: str) -> None:
    assert machine.validate_device_action(action) in machine.DEVICE_ACTIONS


def test_validate_device_action_rejects_unknown() -> None:
    with pytest.raises(HatariError):
        machine.validate_device_action("reset")


@pytest.mark.parametrize(
    "name",
    ["mousegrab", "coldreset", "warmreset", "screenshot", "bosskey", "recanim", "recsound",
     "savemem", "quit"],
)
def test_validate_shortcut_accepts_documented_names(name: str) -> None:
    assert machine.validate_shortcut(name) == name


@pytest.mark.parametrize("name", ["", "reset", "fullscreen", "quit now"])
def test_validate_shortcut_rejects_unknown_names(name: str) -> None:
    with pytest.raises(HatariError):
        machine.validate_shortcut(name)


def test_validate_path_value() -> None:
    assert machine.validate_path_value(" /tmp/out.log ") == "/tmp/out.log"
    assert machine.validate_path_value("/tmp/with space/out.log") == "/tmp/with space/out.log"
    for bad in ["", "   ", "/tmp/a\nb", "/tmp/a\x00b"]:
        with pytest.raises(HatariError):
            machine.validate_path_value(bad)


@pytest.mark.parametrize("register", ["d0", "A7", "pc", "sr", "usp", "vbr", " caar "])
def test_normalize_register_accepts_stock_names(register: str) -> None:
    assert debug_extra.normalize_register(register) == register.strip().lower()


@pytest.mark.parametrize("register", ["", "d8", "a9", "sp", "d0 d1", "foo"])
def test_normalize_register_rejects_unknown_names(register: str) -> None:
    with pytest.raises(HatariError):
        debug_extra.normalize_register(register)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0, "$0"),
        (0x1234, "$1234"),
        ("1234", "$4d2"),
        ("0xabc", "$abc"),
        ("$ABC", "$abc"),
        ("pc", "pc"),
        ("symbol+4", "symbol+4"),
    ],
)
def test_format_register_value(value: int | str, expected: str) -> None:
    assert debug_extra.format_register_value(value) == expected


@pytest.mark.parametrize("value", [-1, 0x1_0000_0000, "", "  ", "0x", "$", "a=b", "1 2", True])
def test_format_register_value_rejects_bad_values(value: object) -> None:
    with pytest.raises(HatariError):
        debug_extra.format_register_value(value)  # type: ignore[arg-type]


def test_register_assignment_uses_stock_syntax() -> None:
    command, name, formatted = debug_extra.register_assignment("A0", 0x1234)
    assert command == "cpureg a0=$1234"
    assert name == "a0"
    assert formatted == "$1234"


@pytest.mark.parametrize(
    "condition",
    ["pc = $1234", "d0 != 5", "(0x1234).w = 0xffff", "pc >= $100", "d1 <> d2"],
)
def test_validate_breakpoint_condition_accepts_comparisons(condition: str) -> None:
    assert debug_extra.validate_breakpoint_condition(condition) == condition


@pytest.mark.parametrize("condition", ["", "pc", "d0 5", "pc = $1\n:once", "pc\x00 = 1"])
def test_validate_breakpoint_condition_rejects_missing_comparison(condition: str) -> None:
    with pytest.raises(HatariError) as excinfo:
        debug_extra.validate_breakpoint_condition(condition)
    assert excinfo.value.code == "bad_option"


def test_parse_breakpoint_id() -> None:
    raw = "CPU condition breakpoint 3 with 1 condition(s) added:\n\tpc = $1234\n"
    assert debug_extra.parse_breakpoint_id(raw) == 3
    assert debug_extra.parse_breakpoint_id("No conditional CPU breakpoints.") is None


def test_parse_pc() -> None:
    raw = "  A7 000068A6\nNext PC: 00fc6600\n"
    assert debug_extra.parse_pc(raw) == 0x00FC6600
    assert debug_extra.parse_pc("no pc here") is None


@pytest.mark.parametrize(
    ("pattern", "expected"),
    [
        ("48656c", [0x48, 0x65, 0x6C]),
        ("48 65 6c", [0x48, 0x65, 0x6C]),
        ("$48 $65", [0x48, 0x65]),
        ("0x4865", [0x48, 0x65]),
    ],
)
def test_parse_find_pattern(pattern: str, expected: list[int]) -> None:
    assert debug_extra.parse_find_pattern(pattern) == expected
    assert debug_extra.format_find_pattern(pattern) == [f"${value:02x}" for value in expected]


@pytest.mark.parametrize("pattern", ["", "486", "zz", "0x", "$", "100 200", "48 6g"])
def test_parse_find_pattern_rejects_bad_input(pattern: str) -> None:
    with pytest.raises(HatariError):
        debug_extra.parse_find_pattern(pattern)


def test_format_find_range() -> None:
    assert debug_extra.format_find_range(None, None) == "$0"
    assert debug_extra.format_find_range(0x100, None) == "$100"
    assert debug_extra.format_find_range(0x100, 0x20) == "$100-$120"
    with pytest.raises(HatariError):
        debug_extra.format_find_range(0x100, 0)
    with pytest.raises(HatariError):
        debug_extra.format_find_range(0xFFFFFFFF, 1)
    with pytest.raises(HatariError):
        debug_extra.format_find_range(0xFFFFFFFF, 2)


def test_parse_find_output_extracts_addresses_and_count() -> None:
    raw = (
        "00001000: 48 65 6c  He\n"
        "00002000: 48 65 6c  He\n"
        "2 matches.\n"
    )
    parsed = debug_extra.parse_find_output(raw, max_hits=20)
    assert parsed["matches"] == [0x1000, 0x2000]
    assert parsed["addresses"] == ["0x00001000", "0x00002000"]
    assert parsed["count"] == 2
    assert parsed["parsed"] == 2
    assert parsed["truncated"] is False


def test_parse_find_output_truncates_to_max_hits() -> None:
    raw = "".join(f"{0x1000 + index:08X}: 41  A\n" for index in range(5)) + "5 matches.\n"
    parsed = debug_extra.parse_find_output(raw, max_hits=2)
    assert parsed["matches"] == [0x1000, 0x1001]
    assert parsed["count"] == 5
    assert parsed["parsed"] == 5
    assert parsed["truncated"] is True


def test_parse_find_output_without_count_line() -> None:
    parsed = debug_extra.parse_find_output("00001000: 41  A\n", max_hits=20)
    assert parsed["matches"] == [0x1000]
    assert parsed["count"] == 1


def test_watchpoint_validators() -> None:
    assert memory_extra.validate_watchpoint_address(0x1234) == 0x1234
    assert memory_extra.validate_watchpoint_length(2) == 2
    assert memory_extra.normalize_watchpoint_mode("W") == "write"
    assert memory_extra.normalize_watchpoint_mode("r") == "read"
    assert memory_extra.normalize_watchpoint_mode("read-write") == "rw"
    for bad in ["", "execute", "write-only"]:
        with pytest.raises(HatariError):
            memory_extra.normalize_watchpoint_mode(bad)
    with pytest.raises(HatariError):
        memory_extra.validate_watchpoint_length(0)
    with pytest.raises(HatariError):
        memory_extra.validate_watchpoint_length(9)
    with pytest.raises(HatariError):
        memory_extra.validate_watchpoint_address(-1)


def test_watchpoint_limitation_is_explicit() -> None:
    text = memory_extra.WATCHPOINT_LIMITATION.lower()
    assert "no native watchpoint" in text
    assert "does not fake" in text
    assert memory_extra.WATCHPOINT_HINT
