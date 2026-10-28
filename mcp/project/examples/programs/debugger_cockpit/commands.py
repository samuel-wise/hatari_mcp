"""Debugger command construction and safe console routing.

The rich UI never has to hand-assemble Hatari syntax, and the raw command
console only forwards a small allowlist of read-only debugger verbs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .addresses import AddressError, format_address, parse_value

BREAKPOINT_ID_RE = re.compile(
    r"condition breakpoint\s+(\d+)\s+with", re.IGNORECASE
)
BREAKPOINT_INDEX_RE = re.compile(r"breakpoint\s+(\d+)\b", re.IGNORECASE)
REGISTER_RE = re.compile(r"^(?:[da][0-7]|pc|sr|usp|isp)$", re.IGNORECASE)


@dataclass(frozen=True)
class BreakpointCommand:
    expression: str
    command: str
    type: str


def _address_text(address: int | str) -> str:
    if isinstance(address, int):
        return format_address(address)
    text = str(address).strip()
    if not text:
        raise AddressError("breakpoint address is empty")
    if text.startswith("$"):
        return text
    if text.lower().startswith("0x"):
        return "$" + text[2:]
    if re.fullmatch(r"[0-9a-fA-F]+", text):
        return "$" + text
    return text


def build_breakpoint_expression(
    address: int | str,
    *,
    type: str = "exec",
    condition: str | None = None,
) -> str:
    """Build the expression part of a Hatari breakpoint command."""

    if type not in {"exec", "mem", "reg"}:
        raise ValueError("type must be exec, mem or reg")
    if condition:
        return condition.strip()
    if type != "exec":
        raise ValueError("mem/reg breakpoints require an explicit condition")
    return f"pc = {_address_text(address)}"


def build_breakpoint_command(
    address: int | str,
    *,
    type: str = "exec",
    condition: str | None = None,
    once: bool = True,
    quiet: bool = False,
    count: int | None = None,
) -> BreakpointCommand:
    expression = build_breakpoint_expression(address, type=type, condition=condition)
    suffix = ""
    if count is not None:
        if count < 1:
            raise ValueError("breakpoint count must be positive")
        suffix += f" :{int(count)}"
    if once:
        suffix += " :once"
    if quiet:
        suffix += " :quiet"
    return BreakpointCommand(
        expression=expression,
        command=f"breakpoint {expression}{suffix}",
        type=type,
    )


def build_register_assignment(register: str, value: int | str) -> str:
    name = str(register).strip().lower()
    if not REGISTER_RE.fullmatch(name):
        raise ValueError(f"unknown M68k register: {register!r}")
    if isinstance(value, str) and value.strip().startswith(("$", "0x")):
        number = parse_value(value)
    else:
        number = parse_value(value)
    return f"cpureg {name}=0x{number:x}"


def parse_breakpoint_list(raw: str) -> list[dict[str, object]]:
    """Extract breakpoint id/text pairs from Hatari's formatted output."""

    entries: list[dict[str, object]] = []
    for line in str(raw).splitlines():
        text = line.strip()
        if not text:
            continue
        if text.startswith(">") or "echo __HMCP_" in text:
            continue
        match = BREAKPOINT_ID_RE.search(text)
        if match is None:
            match = BREAKPOINT_INDEX_RE.search(text)
        identifier = int(match.group(1)) if match else None
        if identifier is not None or "breakpoint" in text.lower():
            entries.append({"id": identifier, "text": text})
    return entries


@dataclass(frozen=True)
class ConsoleRoute:
    raw: str
    verb: str
    action: str
    allowed: bool
    warning: str = ""
    reason: str = ""


SAFE_VERBS = {
    "cpureg",
    "memdump",
    "disasm",
    "d",
    "evaluate",
    "e",
    "symbols",
    "backtrace",
    "bt",
    "find",
    "history",
    "info",
}

DANGEROUS_VERBS = {
    "loadbin",
    "savebin",
    "statesave",
    "stateload",
    "memwrite",
    "quit",
    "exit",
    "exec",
    "shell",
    "system",
    "run",
    "profile",
    "trace",
    "file",
    ":file",
    "script",
    "reset",
}

CONSOLE_HELP = (
    "Safe console verbs: cpureg, memdump, disasm, evaluate, symbols, "
    "backtrace, find, history, breakpoint (list only), info. Use the toolbar "
    "for stepping, continuing, register/memory writes and state changes."
)


def route_console_command(text: str) -> ConsoleRoute:
    raw = str(text).strip()
    if not raw:
        return ConsoleRoute(raw, "", "refused", False, reason="command is empty")
    if "\n" in raw or "\r" in raw:
        return ConsoleRoute(raw, "", "refused", False, reason="only one command per line is allowed")
    if any(token in raw for token in (";", "|", ">", "<", "`", "\x00")):
        return ConsoleRoute(
            raw,
            "",
            "refused",
            False,
            reason="command chaining, redirection and shell metacharacters are not allowed",
        )
    parts = raw.split()
    verb = parts[0].lower()
    if verb in {"help", "?"}:
        return ConsoleRoute(raw, verb, "local_help", True)

    if verb in DANGEROUS_VERBS:
        return ConsoleRoute(
            raw,
            verb,
            "refused",
            False,
            reason=(
                f"'{verb}' can mutate guest state or the host. Use the typed "
                "controls so the cockpit can capture evidence."
            ),
        )

    if verb in {"step", "next", "cont", "continue"}:
        return ConsoleRoute(
            raw,
            verb,
            "refused",
            False,
            reason="execution control is disabled in the raw console; use the toolbar or shortcuts",
        )

    if verb == "breakpoint":
        if len(parts) > 1:
            return ConsoleRoute(
                raw,
                verb,
                "refused",
                False,
                reason="raw breakpoint changes are disabled; use the Breakpoints panel",
            )
        return ConsoleRoute(
            raw,
            verb,
            "dbg_command",
            True,
            warning=(
                "Raw dbg_command: lists breakpoints only. Breakpoint edits use the typed panel."
            ),
        )

    if verb in SAFE_VERBS:
        return ConsoleRoute(
            raw,
            verb,
            "dbg_command",
            True,
            warning=(
                "Raw dbg_command (read-only intent): output is live guest state and is not "
                "validated by the cockpit. This can still be slow on large ranges."
            ),
        )

    return ConsoleRoute(
        raw,
        verb,
        "refused",
        False,
        reason=(
            f"'{verb or raw}' is not on the safe console allowlist. "
            "Use the typed controls or extend route_console_command deliberately."
        ),
    )
