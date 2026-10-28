"""Timed input sequences, combos, replay and reusable JSON scripts."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import time
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from ..app import App
from ..errors import HatariError, error
from . import input as input_tools
from ._common import guarded

MAX_EVENTS = 10000
MAX_TEXT = 4096
MAX_COMBO_KEYS = 16
ALIASES = {"key_press": "key", "key_down": "keydown", "key_up": "keyup"}
MOUSE_BUTTONS = {"left", "right"}
MOUSE_ACTIONS = {"click", "down", "up", "doubleclick"}
EVENT_TYPES = {"key", "keydown", "keyup", "text", "mouse_move", "mouse_button"}
HOST_TIMED_NOTE = (
    "Events are delivered by host wall-clock timing, not frame/VBL-locked. "
    "Use screen_wait_stable or frame capture to confirm the guest reacted."
)


def _as_int(value: Any, name: str, minimum: int | None = None, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise error("bad_option", f"{name} must be an integer.", "Pass a whole number.")
    if minimum is not None and value < minimum:
        raise error("bad_option", f"{name} must be >= {minimum}.", "Use a value inside the allowed range.")
    if maximum is not None and value > maximum:
        raise error("bad_option", f"{name} must be <= {maximum}.", "Use a value inside the allowed range.")
    return value


def _resolve_key(value: Any, index: int) -> tuple[int, bool]:
    if isinstance(value, bool):
        raise error("bad_option", f"Event {index}: key must be a string or scancode.", "Use a key name or 0-255.")
    if isinstance(value, int):
        if not 0 <= value <= 255:
            raise error("bad_option", f"Event {index}: scancode must be 0-255.", "Use a valid ST scancode.")
        return value, False
    if not isinstance(value, str) or not value:
        raise error("bad_option", f"Event {index}: key must be a non-empty string or scancode.", "Use a key name.")
    if value.isdigit() and (len(value) > 1 or int(value) > 9):
        scancode = int(value)
        if not 0 <= scancode <= 255:
            raise error("bad_option", f"Event {index}: scancode must be 0-255.", "Use a valid ST scancode.")
        return scancode, False
    if len(value) == 1:
        return input_tools._scan_for_char(value)
    named = input_tools._named_key(value)
    if named is None:
        raise error("bad_option", f"Event {index}: unknown key {value!r}.", "Use a named key or one ASCII character.")
    return named, False


def _validate_delay(event: dict[str, Any], index: int) -> None:
    if "delay_ms" not in event:
        return
    _as_int(event["delay_ms"], f"Event {index}: delay_ms", 0, 60000)


def _validate_events(events: Any) -> list[dict[str, Any]]:
    if not isinstance(events, list):
        raise error("bad_option", "events must be a list of event objects.", "Pass a JSON array of objects.")
    if len(events) > MAX_EVENTS:
        raise error("bad_option", f"Too many events: {len(events)}.", f"Use at most {MAX_EVENTS} events.")
    normalized: list[dict[str, Any]] = []
    for index, event in enumerate(events):
        if not isinstance(event, dict):
            raise error("bad_option", f"Event {index} is not an object.", "Pass one object per event.")
        raw_type = event.get("type")
        if not isinstance(raw_type, str):
            raise error("bad_option", f"Event {index} has no string type.", "Set type to a supported event name.")
        event_type = ALIASES.get(raw_type.lower(), raw_type.lower())
        if event_type not in EVENT_TYPES:
            raise error(
                "bad_option",
                f"Event {index}: unsupported type {raw_type!r}.",
                "Use key, keydown, keyup, text, mouse_move or mouse_button.",
            )
        item = dict(event)
        item["type"] = event_type
        _validate_delay(item, index)

        if event_type in {"key", "keydown", "keyup"}:
            if "key" not in item:
                raise error("bad_option", f"Event {index}: {event_type} requires key.", "Add a key field.")
            _resolve_key(item["key"], index)
        elif event_type == "text":
            text = item.get("text")
            if not isinstance(text, str):
                raise error("bad_option", f"Event {index}: text requires a string.", "Add a text field.")
            if len(text) > MAX_TEXT:
                raise error("bad_option", f"Event {index}: text is too long.", f"Use at most {MAX_TEXT} characters.")
            for char in text:
                if char in {"\n", "\t"}:
                    continue
                input_tools._scan_for_char(char)
        elif event_type == "mouse_move":
            _as_int(item.get("dx"), f"Event {index}: dx", -32768, 32767)
            _as_int(item.get("dy"), f"Event {index}: dy", -32768, 32767)
        elif event_type == "mouse_button":
            button = item.get("button", "left")
            action = item.get("action", "click")
            if not isinstance(button, str) or button.lower() not in MOUSE_BUTTONS:
                raise error("bad_option", f"Event {index}: invalid mouse button.", "Use left or right.")
            if not isinstance(action, str) or action.lower() not in MOUSE_ACTIONS:
                raise error("bad_option", f"Event {index}: invalid mouse action.", "Use click, down, up or doubleclick.")
            item["button"] = button.lower()
            item["action"] = action.lower()
        normalized.append(item)
    return normalized


def _send(app: App, command: str) -> Any:
    return app.supervisor._socket_command(command)


async def _emit_mouse_button(app: App, button: str, action: str) -> None:
    if action == "doubleclick":
        if button != "left":
            raise error("bad_option", "doubleclick is only available for the left button.", "Use left.")
        await _send(app, "hatari-event doubleclick")
        return
    if action == "click":
        await _send(app, f"hatari-event {button}down")
        await _send(app, f"hatari-event {button}up")
        return
    await _send(app, f"hatari-event {button}{action}")


async def _dispatch_event(app: App, event: dict[str, Any]) -> dict[str, Any]:
    event_type = event["type"]
    if event_type == "key":
        scancode, shift = _resolve_key(event["key"], -1)
        await input_tools._press_scancode(app, scancode, shift)
        return {"key": event["key"], "scancode": scancode, "shift": shift}
    if event_type == "keydown":
        scancode, _ = _resolve_key(event["key"], -1)
        await _send(app, f"hatari-event keydown {scancode}")
        return {"key": event["key"], "scancode": scancode}
    if event_type == "keyup":
        scancode, _ = _resolve_key(event["key"], -1)
        await _send(app, f"hatari-event keyup {scancode}")
        return {"key": event["key"], "scancode": scancode}
    if event_type == "text":
        text = event["text"]
        characters = 0
        for char in text:
            if char == "\n":
                await _send(app, f"hatari-event keypress {input_tools.SCANCODES['return']}")
            elif char == "\t":
                await _send(app, f"hatari-event keypress {input_tools.SCANCODES['tab']}")
            else:
                scancode, shift = input_tools._scan_for_char(char)
                await input_tools._press_scancode(app, scancode, shift)
            characters += 1
        return {"text": text, "characters": characters}
    if event_type == "mouse_move":
        dx = int(event["dx"])
        dy = int(event["dy"])
        await _send(app, f"hatari-event mousemove {dx} {dy}")
        return {"dx": dx, "dy": dy}
    button = event.get("button", "left")
    action = event.get("action", "click")
    await _emit_mouse_button(app, button, action)
    return {"button": button, "action": action}


async def run_sequence(
    app: App,
    events: Any,
    interval_ms: int = 50,
    stop_on_error: bool = True,
) -> dict[str, Any]:
    normalized = _validate_events(events)
    interval_ms = _as_int(interval_ms, "interval_ms", 0, 60000)
    started = time.monotonic()
    results: list[dict[str, Any]] = []
    failed = False

    for index, event in enumerate(normalized):
        entry: dict[str, Any] = {"index": index, "type": event["type"]}
        try:
            entry.update(await _dispatch_event(app, event))
            entry["ok"] = True
        except HatariError as exc:
            failed = True
            entry.update({"ok": False, "error": exc.code, "message": exc.message, "hint": exc.hint})
            results.append(entry)
            if stop_on_error:
                raise error(
                    exc.code,
                    exc.message,
                    exc.hint,
                    raw=exc.raw,
                    extra={"events": results, "failed_index": index},
                ) from exc
        except Exception as exc:
            failed = True
            entry.update({"ok": False, "error": "internal_error", "message": str(exc)})
            results.append(entry)
            if stop_on_error:
                raise
        else:
            results.append(entry)

        if index < len(normalized) - 1:
            delay_ms = _as_int(event.get("delay_ms", interval_ms), f"Event {index}: delay_ms", 0, 60000)
            if delay_ms:
                await asyncio.sleep(delay_ms / 1000.0)

    return {
        "ok": not failed,
        "events": results,
        "count": len(results),
        "interval_ms": interval_ms,
        "stop_on_error": stop_on_error,
        "host_timed": True,
        "frame_locked": False,
        "elapsed_ms": int((time.monotonic() - started) * 1000),
        "note": HOST_TIMED_NOTE,
    }


def _load_events(app: App, path: str) -> tuple[Path, list[dict[str, Any]]]:
    target = app.guard_path(path, must_exist=True)
    if not target.is_file():
        raise error("invalid_path", f"Script is not a file: {target}", "Pass a JSON file path.")
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as exc:
        raise error("invalid_path", f"Cannot read input script: {exc}", "Check the file and permissions.") from exc
    except json.JSONDecodeError as exc:
        raise error("bad_option", f"Input script is not valid JSON: {exc}", "Pass a JSON array of events.") from exc
    return target, _validate_events(payload)


def register(server: MCPServer, app: App) -> None:
    @server.tool(
        name="input_sequence",
        description=(
            "Execute key/keydown/keyup/text/mouse_move/mouse_button events in order through "
            "hatari-event. Host-timed, not frame-locked; returns a per-event result log."
        ),
        annotations=ToolAnnotations(destructiveHint=False, openWorldHint=False),
    )
    @guarded
    async def input_sequence(
        events: list[dict[str, Any]],
        interval_ms: int = 50,
        stop_on_error: bool = True,
    ) -> dict[str, Any]:
        return await run_sequence(app, events, interval_ms=interval_ms, stop_on_error=stop_on_error)

    @server.tool(
        name="input_key_combo",
        description=(
            "Hold a key combination: send keydowns in order, hold, then release in reverse. "
            "Host-timed, not frame-locked."
        ),
        annotations=ToolAnnotations(destructiveHint=False, openWorldHint=False),
    )
    @guarded
    async def input_key_combo(keys: list[str], hold_ms: int = 50) -> dict[str, Any]:
        if not isinstance(keys, list) or not keys:
            raise error("bad_option", "keys must be a non-empty list.", "Example: [\"ctrl\", \"c\"].")
        if len(keys) > MAX_COMBO_KEYS:
            raise error("bad_option", f"Too many combo keys: {len(keys)}.", f"Use at most {MAX_COMBO_KEYS} keys.")
        hold_ms = _as_int(hold_ms, "hold_ms", 0, 60000)
        scancodes: list[int] = []
        for index, key in enumerate(keys):
            scancode, _ = _resolve_key(key, index)
            scancodes.append(scancode)
        pressed: list[int] = []
        try:
            for scancode in scancodes:
                await _send(app, f"hatari-event keydown {scancode}")
                pressed.append(scancode)
            if hold_ms:
                await asyncio.sleep(hold_ms / 1000.0)
        finally:
            for scancode in reversed(pressed):
                with contextlib.suppress(Exception):
                    await _send(app, f"hatari-event keyup {scancode}")
        return {
            "ok": True,
            "keys": keys,
            "scancodes": scancodes,
            "hold_ms": hold_ms,
            "host_timed": True,
            "frame_locked": False,
            "note": HOST_TIMED_NOTE,
        }

    @server.tool(
        name="input_mouse_click",
        description=(
            "Click the current mouse position. Absolute x/y positioning is not available on stock "
            "Hatari and is rejected instead of being faked. Use input_mouse_move for relative motion."
        ),
        annotations=ToolAnnotations(destructiveHint=False, openWorldHint=False),
    )
    @guarded
    async def input_mouse_click(
        x: int | None = None,
        y: int | None = None,
        button: str = "left",
    ) -> dict[str, Any]:
        if not isinstance(button, str) or button.lower() not in MOUSE_BUTTONS:
            raise error("bad_option", f"Invalid button: {button}", "Use left or right.")
        button = button.lower()
        if x is not None or y is not None:
            raise error(
                "unsupported",
                "Absolute mouse positioning is not available on stock Hatari.",
                (
                    "No honest absolute position is tracked, so x/y cannot be converted safely. "
                    "Use input_mouse_move for relative deltas, or omit x/y to click the current position."
                ),
            )
        await _emit_mouse_button(app, button, "click")
        return {
            "ok": True,
            "button": button,
            "x": None,
            "y": None,
            "absolute": False,
            "click_at": "current position",
            "host_timed": True,
            "frame_locked": False,
            "note": HOST_TIMED_NOTE,
        }

    @server.tool(
        name="input_replay",
        description="Load a JSON list of events and execute it through input_sequence.",
        annotations=ToolAnnotations(destructiveHint=False, openWorldHint=False),
    )
    @guarded
    async def input_replay(path: str, interval_ms: int = 50) -> dict[str, Any]:
        target, events = _load_events(app, path)
        result = await run_sequence(app, events, interval_ms=interval_ms, stop_on_error=True)
        return {**result, "path": str(target)}

    @server.tool(
        name="input_sequence_save",
        description="Validate and save a reusable JSON input sequence. The path goes through app.guard_path.",
        annotations=ToolAnnotations(destructiveHint=True, openWorldHint=False),
    )
    @guarded
    async def input_sequence_save(path: str, events: list[dict[str, Any]]) -> dict[str, Any]:
        target = app.guard_path(path)
        if target.is_dir():
            raise error("invalid_path", f"Path is a directory: {target}", "Pass a file path.")
        normalized = _validate_events(events)
        payload = json.dumps(normalized, indent=2) + "\n"
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(payload, encoding="utf-8")
        except OSError as exc:
            raise error("invalid_path", f"Cannot write input script: {exc}", "Check the directory and permissions.") from exc
        encoded = payload.encode("utf-8")
        return {
            "ok": True,
            "path": str(target),
            "count": len(normalized),
            "bytes": len(encoded),
            "sha256": hashlib.sha256(encoded).hexdigest(),
        }

    @server.tool(
        name="input_sequence_load",
        description="Load and validate a reusable JSON input sequence without executing it.",
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False),
    )
    @guarded
    async def input_sequence_load(path: str) -> dict[str, Any]:
        target, events = _load_events(app, path)
        return {
            "ok": True,
            "path": str(target),
            "count": len(events),
            "events": events,
            "host_timed": True,
            "frame_locked": False,
            "note": HOST_TIMED_NOTE,
        }
