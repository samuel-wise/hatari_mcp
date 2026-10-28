"""Keyboard, mouse and joystick input through Hatari's control channel."""

from __future__ import annotations

import asyncio
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from ..app import App
from ..errors import error
from ._common import guarded

SCANCODES = {
    "esc": 1,
    "escape": 1,
    "backspace": 14,
    "tab": 15,
    "return": 28,
    "enter": 28,
    "ctrl": 29,
    "control": 29,
    "lshift": 42,
    "shift": 42,
    "rshift": 54,
    "alt": 56,
    "space": 57,
    "caps": 58,
    "f1": 59,
    "f2": 60,
    "f3": 61,
    "f4": 62,
    "f5": 63,
    "f6": 64,
    "f7": 65,
    "f8": 66,
    "f9": 67,
    "f10": 68,
    "home": 71,
    "up": 72,
    "left": 75,
    "right": 77,
    "down": 80,
    "insert": 82,
    "delete": 83,
    "help": 98,
    "undo": 97,
}

LETTER_SCANCODE = {
    **{chr(ord("a") + i): 30 + i for i in range(9)},
    **{chr(ord("j") + i): 36 + i for i in range(7)},
    **{chr(ord("q") + i): 16 + i for i in range(10)},
    **{chr(ord("z") + i): 44 + i for i in range(7)},
}
DIGIT_SCANCODE = {str(i): 2 + ((i - 1) % 10) for i in range(1, 10)}
DIGIT_SCANCODE["0"] = 11
SHIFTED = {
    "!": "1",
    "@": "2",
    "#": "3",
    "$": "4",
    "%": "5",
    "^": "6",
    "&": "7",
    "*": "8",
    "(": "9",
    ")": "0",
    "_": "-",
    "+": "=",
    "{": "[",
    "}": "]",
    "|": "\\",
    ":": ";",
    '"': "'",
    "<": ",",
    ">": ".",
    "?": "/",
    "~": "`",
}
PUNCT_SCANCODE = {
    "-": 12,
    "=": 13,
    "[": 26,
    "]": 27,
    ";": 39,
    "'": 40,
    "`": 41,
    "\\": 43,
    ",": 51,
    ".": 52,
    "/": 53,
}
JOY_DIRECTIONS = {
    "up": 72,
    "down": 80,
    "left": 75,
    "right": 77,
}


def _event(name: str) -> str:
    return f"hatari-event {name}"


def _scan_for_char(char: str) -> tuple[int, bool]:
    if char.isalpha():
        return LETTER_SCANCODE[char.lower()], char.isupper()
    if char.isdigit():
        return DIGIT_SCANCODE[char], False
    if char in PUNCT_SCANCODE:
        return PUNCT_SCANCODE[char], False
    if char in SHIFTED:
        base = SHIFTED[char]
        if base.isdigit():
            return DIGIT_SCANCODE[base], True
        if base.isalpha():
            return LETTER_SCANCODE[base.lower()], True
        return PUNCT_SCANCODE[base], True
    if char == " ":
        return SCANCODES["space"], False
    raise error("bad_option", f"Unsupported character: {char!r}", "Use printable ASCII or a named key.")


async def _press_scancode(app: App, scancode: int, shift: bool = False) -> None:
    if shift:
        await app.supervisor._socket_command(_event(f"keydown {SCANCODES['lshift']}"))
    await app.supervisor._socket_command(_event(f"keypress {scancode}"))
    if shift:
        await app.supervisor._socket_command(_event(f"keyup {SCANCODES['lshift']}"))


def _named_key(key: str) -> int | None:
    lowered = key.lower()
    if lowered in SCANCODES:
        return SCANCODES[lowered]
    if len(key) == 1:
        try:
            return _scan_for_char(key)[0]
        except Exception:
            return None
    return None


def register(server: MCPServer, app: App) -> None:
    @server.tool(
        name="input_key",
        description="Inject one key press. Accepts a single alphanumeric/printable ASCII or an ST scancode 0-255.",
        annotations=ToolAnnotations(destructiveHint=False),
    )
    @guarded
    async def input_key(key: str) -> dict[str, Any]:
        if key.isdigit() and len(key) <= 3 and (len(key) > 1 or int(key) > 9):
            scancode = int(key)
            if not 0 <= scancode <= 255:
                raise error("bad_option", "Scancode must be 0-255.", "Pass a valid ST scancode.")
            await app.supervisor._socket_command(_event(f"keypress {scancode}"))
            return {"ok": True, "key": key, "scancode": scancode}
        if len(key) == 1:
            scancode, shift = _scan_for_char(key)
        else:
            named = _named_key(key)
            if named is None:
                raise error("bad_option", f"Unknown key: {key!r}", "Use a named key or one ASCII character.")
            scancode, shift = named, False
        await _press_scancode(app, scancode, shift)
        return {"ok": True, "key": key, "scancode": scancode, "shift": shift}

    @server.tool(
        name="input_text",
        description="Type a string as individual keys. Supports printable ASCII and space.",
        annotations=ToolAnnotations(destructiveHint=False),
    )
    @guarded
    async def input_text(text: str, delay_ms: int = 10) -> dict[str, Any]:
        delay = max(0, min(int(delay_ms), 1000)) / 1000.0
        sent = 0
        for char in text:
            if char == "\n":
                await app.supervisor._socket_command(_event(f"keypress {SCANCODES['return']}"))
            elif char == "\t":
                await app.supervisor._socket_command(_event(f"keypress {SCANCODES['tab']}"))
            else:
                scancode, shift = _scan_for_char(char)
                await _press_scancode(app, scancode, shift)
            sent += 1
            if delay:
                await asyncio.sleep(delay)
        return {"ok": True, "characters": sent, "text": text}

    @server.tool(
        name="input_key_down",
        description="Hold a key down (keydown). Use input_key_up to release it.",
        annotations=ToolAnnotations(destructiveHint=False),
    )
    @guarded
    async def input_key_down(key: str) -> dict[str, Any]:
        named = _named_key(key)
        if named is None:
            raise error("bad_option", f"Unknown key: {key!r}", "Use a named key or one ASCII character.")
        await app.supervisor._socket_command(_event(f"keydown {named}"))
        return {"ok": True, "key": key, "scancode": named}

    @server.tool(
        name="input_key_up",
        description="Release a held key (keyup).",
        annotations=ToolAnnotations(destructiveHint=False),
    )
    @guarded
    async def input_key_up(key: str) -> dict[str, Any]:
        named = _named_key(key)
        if named is None:
            raise error("bad_option", f"Unknown key: {key!r}", "Use a named key or one ASCII character.")
        await app.supervisor._socket_command(_event(f"keyup {named}"))
        return {"ok": True, "key": key, "scancode": named}

    @server.tool(
        name="input_mouse_move",
        description="Move the mouse by a relative offset (Hatari's mousemove dx dy). No absolute API exists.",
        annotations=ToolAnnotations(destructiveHint=False),
    )
    @guarded
    async def input_mouse_move(dx: int, dy: int) -> dict[str, Any]:
        await app.supervisor._socket_command(_event(f"mousemove {int(dx)} {int(dy)}"))
        return {"ok": True, "dx": int(dx), "dy": int(dy)}

    @server.tool(
        name="input_mouse_button",
        description="Press, release, or click a mouse button (left/right).",
        annotations=ToolAnnotations(destructiveHint=False),
    )
    @guarded
    async def input_mouse_button(button: str = "left", action: str = "click") -> dict[str, Any]:
        button = button.lower()
        action = action.lower()
        if button not in {"left", "right"}:
            raise error("bad_option", f"Invalid button: {button}", "Use left or right.")
        if action == "click":
            await app.supervisor._socket_command(_event("doubleclick" if button == "left" else "rightdown"))
            if button == "right":
                await app.supervisor._socket_command(_event("rightup"))
        elif action in {"down", "up"}:
            await app.supervisor._socket_command(_event(f"{button}{action}"))
        else:
            raise error("bad_option", f"Invalid action: {action}", "Use click, down or up.")
        return {"ok": True, "button": button, "action": action}

    @server.tool(
        name="input_joystick",
        description=(
            "Indirect joystick control. Hatari exposes no joystick event; with --joystick <port> "
            "cursor keys emulate the joystick, so this sends arrow key events. The guest must be "
            "configured to use keyboard/joystick emulation."
        ),
        annotations=ToolAnnotations(destructiveHint=False),
    )
    @guarded
    async def input_joystick(direction: str, action: str = "press") -> dict[str, Any]:
        direction = direction.lower()
        action = action.lower()
        if direction not in JOY_DIRECTIONS:
            raise error("bad_option", f"Invalid direction: {direction}", "Use up, down, left or right.")
        scancode = JOY_DIRECTIONS[direction]
        if action in {"press", "down"}:
            await app.supervisor._socket_command(_event(f"keydown {scancode}"))
        if action in {"release", "up", "press"}:
            await app.supervisor._socket_command(_event(f"keyup {scancode}"))
        return {
            "ok": True,
            "direction": direction,
            "action": action,
            "indirect": True,
            "hint": "Start Hatari with --joystick <port> for cursor-key joystick emulation.",
        }
