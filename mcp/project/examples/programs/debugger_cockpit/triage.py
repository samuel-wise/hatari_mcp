"""Evidence bundles and evidence-based crash triage text.

Nothing in this module guesses a root cause.  It records what Hatari reported,
formats the captured proof, and states which conclusions the evidence supports
and which it does not.
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .addresses import format_address, format_value, format_word_dump

REGISTER_ORDER = [f"d{i}" for i in range(8)] + [f"a{i}" for i in range(8)] + [
    "pc",
    "sr",
    "usp",
    "isp",
]


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def exception_text(evidence: dict[str, Any]) -> str:
    exception = evidence.get("exception")
    if isinstance(exception, dict):
        for key in ("line", "type", "message"):
            text = _text(exception.get(key))
            if text:
                return text
    return _text(exception) or _text(evidence.get("exception_text"))


def _register_int(registers: dict[str, Any], name: str) -> int | None:
    value = registers.get(name)
    if isinstance(value, int):
        return value
    try:
        return int(str(value), 0)
    except (TypeError, ValueError):
        return None


def format_registers(registers: dict[str, Any]) -> str:
    if not registers:
        return "(no registers captured)"
    lines: list[str] = []
    seen: set[str] = set()
    for name in REGISTER_ORDER:
        if name not in registers:
            continue
        seen.add(name)
        value = _register_int(registers, name)
        if value is None:
            lines.append(f"{name.upper():<3}  ?")
        elif name == "sr":
            lines.append(f"{name.upper():<3}  {format_value(value, width=4)}")
        else:
            lines.append(f"{name.upper():<3}  {format_value(value)}")
    for name in sorted(registers):
        if name in seen:
            continue
        value = _register_int(registers, name)
        lines.append(f"{name.upper():<3}  {format_value(value) if value is not None else '?'}")
    return "\n".join(lines)


def _exception_basis(exception: dict[str, Any], pc: int | None) -> list[str]:
    kind = _text(exception.get("type")) or "exception"
    lowered = kind.lower()
    lines: list[str] = [f"Hatari recorded a {kind} exception."]
    if "line f" in lowered or "line-f" in lowered or "linef" in lowered:
        lines.append(
            "The Line-F vector is taken for unimplemented opcodes. That often means "
            "execution reached a word that is not valid 68000 code, but the captured "
            "PC and stack only show where it surfaced, not how execution got there."
        )
    elif "line a" in lowered or "line-a" in lowered or "linea" in lowered:
        lines.append(
            "The Line-A vector is used by the original TOS for unimplemented A-line "
            "opcodes and some OS calls. The flip side is that a stray A-line opcode "
            "can also land here; the disassembly around PC is the useful discriminator."
        )
    elif "illegal" in lowered:
        lines.append(
            "An illegal-instruction exception means the CPU fetched an invalid opcode. "
            "Check the bytes at the captured PC, not just the PC value."
        )
    elif "privilege" in lowered:
        lines.append(
            "A privilege violation means user-mode code executed a supervisor-only "
            "instruction. The stack frame should identify the faulting instruction."
        )
    elif "bus" in lowered or "address" in lowered:
        lines.append(
            "A bus/address error points at a memory access fault. It is not proof that "
            "the pointer was bad earlier; inspect the captured address and stack."
        )
    elif "trace" in lowered:
        lines.append(
            "A trace exception is normally deliberate single-step state, but it can also "
            "surface when the trace bit was left set by other code."
        )
    else:
        lines.append(
            "The exception type alone does not identify a root cause. Use the captured "
            "PC, disassembly and stack as the starting evidence."
        )
    if pc is not None:
        lines.append(f"The captured PC is {format_address(pc)}.")
        if pc % 2:
            lines.append(
                "The PC is odd. 68000 instruction fetches are even-aligned, so this is "
                "itself suspicious evidence."
            )
    return lines


def diagnose(evidence: dict[str, Any]) -> str:
    """Return a short, evidence-grounded diagnosis for the Triage pane."""

    status = evidence.get("status") if isinstance(evidence.get("status"), dict) else {}
    registers = (
        evidence.get("registers") if isinstance(evidence.get("registers"), dict) else {}
    )
    exception = evidence.get("exception")
    exception = exception if isinstance(exception, dict) else {}
    pc = _register_int(registers, "pc")
    if pc is None:
        exception_pc = _text(exception.get("pc"))
        if exception_pc:
            try:
                pc = int(exception_pc, 16)
            except ValueError:
                pc = None

    reason = _text(evidence.get("reason")) or "captured stop"
    lines = [f"Evidence summary for: {reason}"]
    if exception_text(evidence):
        lines.extend(_exception_basis(exception, pc))
    else:
        lines.append("No guest exception is recorded in this capture.")
        state = _text(status.get("state"))
        if state == "at_prompt":
            lines.append(
                "The emulator is at the debugger prompt, so this is most consistent with "
                "a breakpoint, single-step or explicit debugger entry."
            )
        elif state:
            lines.append(f"The emulator state is {state}; no exception payload was captured.")
        lines.append(
            "If the guest appeared to crash, a handler may already have consumed the "
            "exception before this evidence sample. Do not infer a cause from the stop alone."
        )

    disassembly = evidence.get("disassembly") or []
    if isinstance(disassembly, list) and disassembly:
        lines.append(
            f"Disassembly around PC contains {len(disassembly)} captured line(s); read "
            "the first instructions before assigning blame."
        )
    else:
        lines.append("No disassembly lines were captured for this stop.")

    stack = evidence.get("stack")
    stack_words = stack.get("words") if isinstance(stack, dict) else None
    if stack_words:
        lines.append(
            f"The stack dump contains {len(stack_words)} raw 32-bit word(s); it is "
            "evidence, not an unwound call chain."
        )
    else:
        lines.append("No stack words were captured.")

    frames = None
    backtrace = evidence.get("backtrace")
    if isinstance(backtrace, dict):
        frames = backtrace.get("frames")
    if isinstance(frames, list) and frames:
        lines.append(
            f"The best-effort A6 backtrace produced {len(frames)} frame(s). Frame-pointer "
            "walks are only as reliable as the guest's A6 chain and loaded symbols."
        )
    else:
        lines.append(
            "No A6 frame-pointer frames were recovered. That is common for hand-written "
            "or optimized code and does not by itself mean the stack is corrupt."
        )

    errors = evidence.get("errors") or []
    if errors:
        lines.append(
            "Some evidence could not be captured: " + "; ".join(str(item) for item in errors)
        )
    lines.append(
        "This diagnosis reports the captured evidence only. It deliberately does not "
        "claim a root cause that the registers, disassembly and stack do not prove."
    )
    return "\n".join(lines)


def _tail_text(name: str, lines: Any) -> str:
    if not lines:
        return f"(no {name})"
    if isinstance(lines, str):
        return lines
    return "\n".join(str(line) for line in lines)


def _stack_text(evidence: dict[str, Any]) -> str:
    parts: list[str] = []
    stack = evidence.get("stack")
    if isinstance(stack, dict):
        address = stack.get("address")
        words = stack.get("words") or []
        header = "Stack words"
        if isinstance(address, int):
            header += f" at {format_address(address)}"
        parts.append(header)
        if isinstance(address, int):
            parts.append(format_word_dump(words, base_address=address))
        else:
            parts.append(format_word_dump(words))
    else:
        parts.append("(no stack dump)")
    backtrace = evidence.get("backtrace")
    if isinstance(backtrace, dict) and backtrace.get("frames"):
        parts.append("")
        parts.append("A6 frame-pointer backtrace (best effort)")
        for frame in backtrace["frames"]:
            pc = frame.get("pc")
            text = frame.get("text") or frame.get("symbol") or ""
            if isinstance(pc, int):
                parts.append(f"#{frame.get('index', '?')}  {format_address(pc)}  {text}")
            else:
                parts.append(f"#{frame.get('index', '?')}  {text}")
    else:
        parts.append("")
        parts.append("(no A6 frame-pointer frames)")
    return "\n".join(parts)


def format_triage_report(evidence: dict[str, Any]) -> str:
    captured = _text(evidence.get("captured_at")) or "unknown time"
    status = evidence.get("status") if isinstance(evidence.get("status"), dict) else {}
    registers = (
        evidence.get("registers") if isinstance(evidence.get("registers"), dict) else {}
    )
    disassembly = evidence.get("disassembly") or []
    if not isinstance(disassembly, list):
        disassembly = [str(disassembly)]
    exception = exception_text(evidence) or "(none recorded)"
    screenshot = _text(evidence.get("screenshot")) or "(not captured)"
    program = _text(evidence.get("program")) or "(unknown)"
    tos = _text(evidence.get("tos")) or "(unknown)"
    state = _text(status.get("state")) or "(unknown)"
    at_prompt = status.get("at_prompt")

    lines = [
        "# Debugger cockpit evidence bundle",
        "",
        f"Captured: {captured}",
        f"Reason:   {_text(evidence.get('reason')) or 'captured stop'}",
        f"Program:  {program}",
        f"TOS:      {tos}",
        f"State:    {state} (at_prompt={at_prompt})",
        f"Exception: {exception}",
        f"Screenshot: {screenshot}",
        "",
        "## Evidence-based diagnosis",
        "",
        diagnose(evidence),
        "",
        "## Registers",
        "",
        "```",
        format_registers(registers),
        "```",
        "",
        "## Disassembly around PC",
        "",
        "```",
        "\n".join(str(line) for line in disassembly) if disassembly else "(no disassembly)",
        "```",
        "",
        "## Stack and backtrace",
        "",
        "```",
        _stack_text(evidence),
        "```",
        "",
        "## Guest console tail",
        "",
        "```",
        _tail_text("console output", evidence.get("console_tail")),
        "```",
        "",
        "## Hatari/debugger log tail",
        "",
        "```",
        _tail_text("log output", evidence.get("log_tail")),
        "```",
        "",
        "## Capture errors",
        "",
        _tail_text("capture errors", evidence.get("errors")) if evidence.get("errors") else "(none)",
        "",
    ]
    return "\n".join(lines)


def _safe_component(text: str, fallback: str = "stop") -> str:
    cleaned = []
    for char in text.lower():
        if char.isalnum() or char in "._-":
            cleaned.append(char)
        elif char.isspace():
            cleaned.append("-")
        else:
            cleaned.append("-")
    result = "".join(cleaned).strip("-._")
    while "--" in result:
        result = result.replace("--", "-")
    return (result or fallback)[:80]


def _timestamp_token(timestamp: str | None) -> str:
    if timestamp:
        token = "".join(
            char for char in timestamp.replace("T", "-").replace(":", "")
            if char.isalnum() or char in "-._"
        )
        if token:
            return token[:80]
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")


def write_triage_bundle(
    root: str | Path,
    evidence: dict[str, Any],
    *,
    timestamp: str | None = None,
) -> dict[str, Any]:
    """Write the report, JSON evidence and proof files into a new bundle."""

    root_path = Path(root).expanduser().resolve()
    bundle_root = root_path / "crash-bundles"
    bundle_root.mkdir(parents=True, exist_ok=True)
    stamp = _timestamp_token(timestamp or _text(evidence.get("captured_at")))
    reason = _safe_component(_text(evidence.get("reason")) or "stop")
    bundle = bundle_root / f"{stamp}-{reason}"
    index = 2
    while bundle.exists():
        bundle = bundle_root / f"{stamp}-{reason}-{index}"
        index += 1
    bundle.mkdir(parents=True, exist_ok=False)

    report = format_triage_report(evidence)
    diagnosis = diagnose(evidence)
    files: list[str] = []

    def write(name: str, content: str) -> None:
        path = bundle / name
        path.write_text(content.rstrip() + "\n", encoding="utf-8")
        files.append(str(path))

    write("report.md", report)
    write("registers.txt", format_registers(evidence.get("registers") or {}))
    disassembly = evidence.get("disassembly") or []
    if not isinstance(disassembly, list):
        disassembly = [str(disassembly)]
    write("disassembly.txt", "\n".join(str(line) for line in disassembly) or "(no disassembly)")
    write("stack.txt", _stack_text(evidence))
    write("console-tail.txt", _tail_text("console output", evidence.get("console_tail")))
    write("log-tail.txt", _tail_text("log output", evidence.get("log_tail")))
    write(
        "capture-errors.txt",
        _tail_text("capture errors", evidence.get("errors")) if evidence.get("errors") else "(none)",
    )

    payload = dict(evidence)
    payload["captured_at"] = _text(evidence.get("captured_at")) or stamp
    payload["bundle"] = str(bundle)
    payload["diagnosis"] = diagnosis
    write("evidence.json", json.dumps(payload, indent=2, sort_keys=True, default=str))

    screenshot = _text(evidence.get("screenshot"))
    if screenshot:
        source = Path(screenshot).expanduser()
        if source.is_file():
            suffix = source.suffix or ".bmp"
            target = bundle / f"screenshot{suffix}"
            shutil.copy2(source, target)
            files.append(str(target))

    return {
        "path": str(bundle),
        "report": report,
        "diagnosis": diagnosis,
        "files": files,
    }
