"""Unit tests for address parsing, command construction and triage text."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parents[3]
PROGRAMS = PROJECT / "examples" / "programs"
if str(PROGRAMS) not in sys.path:
    sys.path.insert(0, str(PROGRAMS))

from debugger_cockpit import addresses, commands, triage  # noqa: E402


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("$1234", 0x1234),
        ("0x1234", 0x1234),
        ("0X1234", 0x1234),
        ("1234", 0x1234),
        ("1234h", 0x1234),
        ("1234d", 1234),
        ("%1010", 0b1010),
        ("0b1010", 0b1010),
        (0xCAFE, 0xCAFE),
    ],
)
def test_parse_address_forms(text, expected):
    assert addresses.parse_address(text) == expected


@pytest.mark.parametrize("text", ["", "   ", "not-hex", "-1", "0x", "100000000"])
def test_parse_address_rejects_bad_input(text):
    with pytest.raises(addresses.AddressError):
        addresses.parse_address(text)


def test_parse_address_default_and_limits():
    assert addresses.parse_address("", default=7) == 7
    assert addresses.parse_address("$ffffffff") == 0xFFFFFFFF
    with pytest.raises(addresses.AddressError):
        addresses.parse_address("$100000000")


@pytest.mark.parametrize(
    ("text", "expected"),
    [("1234", 1234), ("$1234", 0x1234), ("0x1234", 0x1234), ("%1010", 10), (42, 42)],
)
def test_parse_value(text, expected):
    assert addresses.parse_value(text) == expected


def test_parse_count_and_hex_normalization():
    assert addresses.parse_count("64") == 64
    assert addresses.parse_count("0x10") == 16
    with pytest.raises(addresses.AddressError):
        addresses.parse_count("0")
    with pytest.raises(addresses.AddressError):
        addresses.parse_count("65537")
    assert addresses.normalize_hex_data("de ad be ef") == "deadbeef"
    assert addresses.normalize_hex_data("0xDE-AD_BE-EF") == "deadbeef"
    with pytest.raises(addresses.AddressError):
        addresses.normalize_hex_data("abc")
    with pytest.raises(addresses.AddressError):
        addresses.normalize_hex_data("zz")
    with pytest.raises(addresses.AddressError):
        addresses.normalize_hex_data("   ")


def test_hex_dump_and_formatting():
    assert addresses.format_address(0xEA08) == "$0000ea08"
    assert addresses.format_value(0x1, width=4) == "$0001"
    dump = addresses.format_hex_dump(b"A\x00BC", 0x10)
    assert "$00000010" in dump
    assert "41 00 42 43" in dump
    assert "|A.BC|" in dump
    assert addresses.format_hex_dump(b"", 0) == "(no data)"


def test_breakpoint_command_construction():
    assert (
        commands.build_breakpoint_expression(0x1234)
        == "pc = $00001234"
    )
    assert (
        commands.build_breakpoint_expression("0x2000")
        == "pc = $2000"
    )
    assert (
        commands.build_breakpoint_expression("$2000")
        == "pc = $2000"
    )
    assert (
        commands.build_breakpoint_expression(
            0, type="mem", condition="(0x1234).w = 0xffff"
        )
        == "(0x1234).w = 0xffff"
    )
    with pytest.raises(ValueError):
        commands.build_breakpoint_expression(0x100, type="mem")
    with pytest.raises(ValueError):
        commands.build_breakpoint_expression(0x100, type="invalid")

    bp = commands.build_breakpoint_command(
        0x1234, once=True, quiet=True, count=2
    )
    assert bp.command == "breakpoint pc = $00001234 :2 :once :quiet"
    assert bp.expression == "pc = $00001234"
    assert bp.type == "exec"
    assert commands.build_breakpoint_command(0x1234, once=False).command == (
        "breakpoint pc = $00001234"
    )
    with pytest.raises(ValueError):
        commands.build_breakpoint_command(0x1234, count=0)


def test_register_assignment_and_breakpoint_list_parsing():
    assert commands.build_register_assignment("D0", 0x1234) == "cpureg d0=0x1234"
    assert commands.build_register_assignment("a7", "$20") == "cpureg a7=0x20"
    with pytest.raises(ValueError):
        commands.build_register_assignment("d8", 1)

    raw = (
        "Condition breakpoint 1 with pc = $00001234 :once\n"
        "Condition breakpoint 2 with pc = $00002000\n"
        "> echo __HMCP_abc__\n"
    )
    entries = commands.parse_breakpoint_list(raw)
    assert [entry["id"] for entry in entries] == [1, 2]
    assert "00001234" in str(entries[0]["text"])


def test_console_router_allows_only_safe_read_only_verbs():
    route = commands.route_console_command("cpureg")
    assert route.allowed and route.action == "dbg_command"
    assert "Raw dbg_command" in route.warning

    assert commands.route_console_command("breakpoint").allowed
    assert not commands.route_console_command("breakpoint 1").allowed
    assert not commands.route_console_command("loadbin /tmp/x $100").allowed
    assert not commands.route_console_command("statesave /tmp/x").allowed
    assert not commands.route_console_command("cont").allowed
    assert not commands.route_console_command("cpureg; quit").allowed
    assert not commands.route_console_command("cpureg > /tmp/x").allowed
    assert not commands.route_console_command("").allowed
    help_route = commands.route_console_command("help")
    assert help_route.allowed and help_route.action == "local_help"


def _evidence(tmp_path: Path, *, with_exception: bool = True) -> dict:
    screenshot = tmp_path / "screen.bmp"
    screenshot.write_bytes(b"BMfake")
    return {
        "captured_at": "20260101-120000-000000",
        "reason": "exception: Line F" if with_exception else "stopped at debugger prompt",
        "program": "/tmp/COCKPIT.TOS",
        "tos": "/tmp/emutos.img",
        "status": {"state": "at_prompt", "at_prompt": True},
        "exception": (
            {
                "type": "Line F",
                "pc": "ea08",
                "line": "Line F exception at $0000ea08",
            }
            if with_exception
            else None
        ),
        "registers": {
            "d0": 0x1234,
            "a7": 0x0000F000,
            "pc": 0x0000EA08,
            "sr": 0x2700,
        },
        "disassembly": ["0000ea08 4afc illegal", "0000ea0a 4e71 nop"],
        "stack": {"address": 0x0000F000, "words": [0x0000EA08, 0x00000001]},
        "backtrace": {
            "frames": [{"index": 0, "pc": 0x0000EA08, "text": "cockpit_crash"}],
            "confidence": "best_effort",
        },
        "console_tail": ["COCKPIT SAMPLE READY", "deliberate illegal instruction now"],
        "log_tail": ["Line F exception at $0000ea08"],
        "screenshot": str(screenshot),
        "errors": [],
    }


def test_triage_diagnosis_is_evidence_based(tmp_path: Path):
    evidence = _evidence(tmp_path)
    diagnosis = triage.diagnose(evidence)
    assert "Line-F" in diagnosis
    assert "$0000ea08" in diagnosis
    assert "does not" in diagnosis.lower()
    assert "root cause" in diagnosis.lower()
    report = triage.format_triage_report(evidence)
    assert "# Debugger cockpit evidence bundle" in report
    assert "## Registers" in report
    assert "## Stack and backtrace" in report
    assert "cockpit_crash" in report
    assert "COCKPIT SAMPLE READY" in report
    assert str(tmp_path / "screen.bmp") in report

    no_exception = _evidence(tmp_path, with_exception=False)
    diagnosis = triage.diagnose(no_exception)
    assert "No guest exception" in diagnosis
    assert "breakpoint" in diagnosis.lower()


def test_write_triage_bundle(tmp_path: Path):
    evidence = _evidence(tmp_path)
    bundle = triage.write_triage_bundle(tmp_path / "state", evidence, timestamp="20260101-120000")
    path = Path(bundle["path"])
    assert path.is_dir()
    assert path.parent.name == "crash-bundles"
    assert (path / "report.md").is_file()
    assert (path / "evidence.json").is_file()
    assert (path / "registers.txt").is_file()
    assert (path / "disassembly.txt").is_file()
    assert (path / "stack.txt").is_file()
    assert (path / "console-tail.txt").is_file()
    assert (path / "screenshot.bmp").is_file()
    payload = json.loads((path / "evidence.json").read_text(encoding="utf-8"))
    assert payload["bundle"] == str(path)
    assert "Line-F" in payload["diagnosis"]
    assert "Line-F" in (path / "report.md").read_text(encoding="utf-8")
    assert bundle["files"]


def test_import_does_not_create_tk_root():
    env = os.environ.copy()
    env["PYTHONPATH"] = str(PROGRAMS)
    env.pop("DISPLAY", None)
    env.pop("WAYLAND_DISPLAY", None)
    code = (
        "import tkinter, debugger_cockpit, debugger_cockpit.app;"
        "print(tkinter._default_root)"
    )
    completed = subprocess.run(
        [sys.executable, "-c", code],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip().endswith("None")
