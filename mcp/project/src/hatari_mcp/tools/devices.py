"""Device toggles, stream paths and audio/video recording tools."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from ..app import App
from ..errors import error
from ..state import ALIVE, WRITABLE_SOCKET
from ._common import guarded
from .media import disable_modal_alerts, escape_option_arg

DEVICE_NAMES = {"printer", "rs232", "scca", "sccalan", "sccb", "midi"}
DEVICE_ACTIONS = {"enable", "disable", "toggle"}
INPUT_PATH_NAMES = {"midiin", "rs232in", "sccain", "sccalanin", "sccbin"}
OUTPUT_PATH_NAMES = {
    "memauto",
    "memsave",
    "printout",
    "soundout",
    "midiout",
    "rs232out",
    "sccaout",
    "sccalanout",
    "sccbout",
}
PATH_NAMES = INPUT_PATH_NAMES | OUTPUT_PATH_NAMES
PATH_ALIASES = {
    "printer": "printout",
    "sound": "soundout",
    "midi-in": "midiin",
    "midi-out": "midiout",
    "rs232-in": "rs232in",
    "rs232-out": "rs232out",
    "scc-a-in": "sccain",
    "scc-a-out": "sccaout",
    "scc-a-lan-in": "sccalanin",
    "scc-a-lan-out": "sccalanout",
    "scc-b-in": "sccbin",
    "scc-b-out": "sccbout",
}
AUDIO_CAPTURE_EXTENSIONS = {".wav", ".ym"}
VIDEO_CODECS = {"png", "bmp"}


def _validate_path_text(path: str) -> str:
    if not path:
        raise error("bad_option", "Path must not be empty.", "Pass a file path.")
    if "\n" in path or "\r" in path:
        raise error("bad_option", "Paths must not contain newlines.", "Use a single-line path.")
    if path != path.strip():
        raise error(
            "bad_option",
            "Paths with leading or trailing whitespace are trimmed by Hatari.",
            "Remove the surrounding whitespace from the path.",
        )
    return path


def normalize_device(name: str) -> str:
    normalized = name.strip().lower()
    if normalized not in DEVICE_NAMES:
        raise error(
            "bad_option",
            f"Invalid device: {name!r}",
            f"Use one of: {', '.join(sorted(DEVICE_NAMES))}.",
        )
    return normalized


def normalize_action(action: str) -> str:
    normalized = action.strip().lower()
    if normalized not in DEVICE_ACTIONS:
        raise error(
            "bad_option",
            f"Invalid device action: {action!r}",
            "Use enable, disable or toggle.",
        )
    return normalized


def normalize_path_name(name: str) -> str:
    normalized = PATH_ALIASES.get(name.strip().lower(), name.strip().lower())
    if normalized not in PATH_NAMES:
        raise error(
            "bad_option",
            f"Unknown Hatari path name: {name!r}",
            f"Use one of: {', '.join(sorted(PATH_NAMES))}.",
        )
    return normalized


def build_device_command(name: str, action: str) -> str:
    device = normalize_device(name)
    verb = normalize_action(action)
    return f"hatari-{verb} {device}"


def build_path_command(name: str, path: str) -> str:
    normalized = normalize_path_name(name)
    value = _validate_path_text(path)
    return f"hatari-path {normalized} {value}"


def build_shortcut_command(name: str) -> str:
    shortcut = name.strip().lower()
    if shortcut not in {"recsound", "recanim"}:
        raise error(
            "bad_option",
            f"Unsupported media shortcut: {name!r}",
            "Use recsound or recanim.",
        )
    return f"hatari-shortcut {shortcut}"


def normalize_video_codec(codec: str) -> str:
    normalized = codec.strip().lower()
    if normalized not in VIDEO_CODECS:
        raise error(
            "bad_option",
            f"Unsupported AVI video codec: {codec!r}",
            "Use png (requires libpng) or bmp.",
        )
    return normalized


def normalize_video_fps(fps: int) -> int:
    value = int(fps)
    if not 1 <= value <= 100:
        raise error("bad_option", "AVI fps must be 1-100.", "Pass a supported frame rate such as 25.")
    return value


def build_avi_options(path: str | Path, codec: str, fps: int) -> list[str]:
    normalized_codec = normalize_video_codec(codec)
    normalized_fps = normalize_video_fps(fps)
    return [
        f"--avi-file {escape_option_arg(str(path))}",
        f"--avi-vcodec {normalized_codec}",
        f"--avi-fps {normalized_fps}",
    ]


def build_audio_capture_command(path: str | Path) -> str:
    return build_path_command("soundout", str(path))


def validate_audio_capture(path: str | Path) -> Path:
    target = Path(path)
    suffix = target.suffix.lower()
    if suffix not in AUDIO_CAPTURE_EXTENSIONS:
        raise error(
            "unsupported_format",
            f"Unsupported sound capture extension: {suffix or '<none>'}",
            "recsound chooses the format from the extension; use .wav or .ym.",
        )
    return target


def _require_readable_file(path: Path) -> Path:
    if not path.exists():
        raise error("invalid_path", f"File does not exist: {path}", "Check the path.")
    if not path.is_file():
        raise error("invalid_path", f"Path is not a regular file: {path}", "Pass an input file.")
    try:
        with path.open("rb") as handle:
            handle.read(1)
    except OSError as exc:
        raise error(
            "invalid_path",
            f"File is not readable: {path}",
            "Check permissions and the path.",
            detail=str(exc),
        ) from exc
    return path


def _prepare_output(path: Path) -> Path:
    if path.exists() and path.is_dir():
        raise error("invalid_path", f"Output path is a directory: {path}", "Pass a file path.")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise error(
            "invalid_path",
            f"Cannot create output directory: {path.parent}",
            "Choose a writable path.",
            detail=str(exc),
        ) from exc
    return path


def _prepare_audio_capture(path: Path) -> Path:
    return _prepare_output(validate_audio_capture(path))


async def _send_control(app: App, command: str) -> None:
    state = app.supervisor.state
    if state not in WRITABLE_SOCKET:
        raise error(
            "not_running",
            f"Hatari is {state.value}; control commands need a running or remote-paused emulator.",
            "Call emu_start/emu_resume. At the debugger prompt use dbg_command/continue.",
        )
    await app.supervisor._socket_command(command)


_RECORDING: dict[int, tuple[float | None, dict[str, Any]]] = {}
_CAPTURE_PATHS: dict[int, tuple[float | None, str]] = {}


def _generation(app: App) -> float | None:
    return app.supervisor.started_at


def _get_recording(app: App, kind: str) -> dict[str, Any] | None:
    if app.supervisor.state not in ALIVE:
        return None
    entry = _RECORDING.get(id(app.supervisor))
    if entry is None or entry[0] != _generation(app):
        return None
    return entry[1].get(kind)


def _set_recording(app: App, kind: str, value: dict[str, Any]) -> None:
    generation = _generation(app)
    entry = _RECORDING.get(id(app.supervisor))
    if entry is None or entry[0] != generation:
        entry = (generation, {})
    entry[1][kind] = value
    _RECORDING[id(app.supervisor)] = entry


def _set_capture_path(app: App, path: Path) -> None:
    _CAPTURE_PATHS[id(app.supervisor)] = (_generation(app), str(path))


def _get_capture_path(app: App) -> str | None:
    entry = _CAPTURE_PATHS.get(id(app.supervisor))
    if entry is None or entry[0] != _generation(app):
        return None
    return entry[1]


def register(server: MCPServer, app: App) -> None:
    @server.tool(
        name="media_device",
        description=(
            "Enable, disable or toggle a file-backed device with Hatari's "
            "hatari-enable/disable/toggle control command. Devices: printer, rs232, "
            "scca, sccalan, sccb, midi."
        ),
        annotations=ToolAnnotations(destructiveHint=False, openWorldHint=False),
    )
    @guarded
    async def media_device(name: str, action: str = "toggle") -> dict[str, Any]:
        command = build_device_command(name, action)
        await _send_control(app, command)
        return {
            "ok": True,
            "device": command.split()[1],
            "action": normalize_action(action),
            "command": command,
        }

    @server.tool(
        name="media_set_output",
        description=(
            "Set a Hatari stream path with hatari-path (printout, soundout, rs232in/out, "
            "midiin/out, scca*, sccalan*, sccb*). Output paths are created if needed; input "
            "paths must already exist. Open streams may need a device toggle or restart."
        ),
        annotations=ToolAnnotations(destructiveHint=True, openWorldHint=False),
    )
    @guarded
    async def media_set_output(name: str, path: str) -> dict[str, Any]:
        normalized = normalize_path_name(name)
        resolved = app.guard_path(path)
        if normalized in INPUT_PATH_NAMES:
            target = _require_readable_file(resolved)
            note = "Input path updated; a stream already open may keep reading the old file."
        else:
            target = _prepare_output(resolved)
            note = "Output path updated; re-initialise the device (off/on) or restart if it stays open."
        command = build_path_command(normalized, str(target))
        await _send_control(app, command)
        if normalized == "soundout":
            _set_capture_path(app, target)
        return {
            "ok": True,
            "name": normalized,
            "path": str(target),
            "command": command,
            "note": note,
        }

    @server.tool(
        name="media_set_audio_capture",
        description=(
            "Set Hatari's sound capture filename (ConfigureParams.Sound.szYMCaptureFileName) "
            "for emu_shortcut recsound. src/options.c has no CLI option for this field; this "
            "uses the documented hatari-path soundout control command. Use .wav or .ym."
        ),
        annotations=ToolAnnotations(destructiveHint=True, openWorldHint=False),
    )
    @guarded
    async def media_set_audio_capture(path: str) -> dict[str, Any]:
        target = _prepare_audio_capture(app.guard_path(path))
        command = build_audio_capture_command(target)
        await _send_control(app, command)
        _set_capture_path(app, target)
        return {
            "ok": True,
            "path": str(target),
            "command": command,
            "mechanism": "hatari-path soundout",
            "cli_option": None,
            "limitation": (
                "Hatari has no command-line option for szYMCaptureFileName. Set it before "
                "starting recsound; the .wav/.ym extension selects the format. Sound must "
                "be enabled (the bundled profiles launch with --sound off)."
            ),
        }

    @server.tool(
        name="media_record_audio_start",
        description=(
            "Start YM/WAV capture with the recsound shortcut. recsound is a toggle, and the "
            "filename/format come from ConfigureParams.Sound.szYMCaptureFileName (set with "
            "media_set_audio_capture or hatari-path soundout); there is no per-recording path."
        ),
        annotations=ToolAnnotations(destructiveHint=True, openWorldHint=False),
    )
    @guarded
    async def media_record_audio_start() -> dict[str, Any]:
        if app.supervisor.state not in WRITABLE_SOCKET:
            raise error(
                "not_running",
                f"Hatari is {app.supervisor.state.value}; recording needs a running emulator.",
                "Call emu_start first.",
            )
        current = _get_recording(app, "audio")
        if current and current.get("active"):
            raise error(
                "already_recording",
                "This server already started audio recording for the current Hatari run.",
                "Call media_record_audio_stop before starting another recording. recsound toggles.",
            )
        command = build_shortcut_command("recsound")
        await disable_modal_alerts(app)
        capture = _get_capture_path(app)
        before = 0
        if capture:
            path = Path(capture)
            before = path.stat().st_size if path.is_file() else 0
        await _send_control(app, command)
        verified = False
        if capture:
            await asyncio.sleep(0.2)
            path = Path(capture)
            verified = path.is_file() and path.stat().st_size > before
        _set_recording(app, "audio", {"active": verified or not capture, "path": capture})
        return {
            "ok": True,
            "start_requested": True,
            "recording": True if verified else None,
            "command": command,
            "toggle": True,
            "path": capture,
            "capture_path_known": bool(capture),
            "verified": verified,
            "sound_required": True,
            "sound_likely_disabled": bool(capture) and not verified,
            "note": (
                "recsound toggles recording to the configured soundout path; Hatari exposes "
                "no recording-state query. WAV requires a .wav capture path and enabled sound "
                "(the bundled profiles launch with --sound off). If recsound was started "
                "outside this server, this request may have stopped it."
            ),
        }

    @server.tool(
        name="media_record_audio_stop",
        description=(
            "Stop YM/WAV capture with the recsound shortcut. Only stops a recording this "
            "server tracked as started; Hatari has no query for external recsound state."
        ),
        annotations=ToolAnnotations(destructiveHint=False, openWorldHint=False),
    )
    @guarded
    async def media_record_audio_stop() -> dict[str, Any]:
        if app.supervisor.state not in WRITABLE_SOCKET:
            raise error(
                "not_running",
                f"Hatari is {app.supervisor.state.value}; recording needs a running emulator.",
                "Call emu_start first.",
            )
        current = _get_recording(app, "audio")
        if current is None:
            raise error(
                "recording_state_unknown",
                "Hatari has no control-channel query for recsound state.",
                "If recording was started outside this server, invoke emu_shortcut recsound "
                "once yourself. This tool refuses because recsound would start recording if "
                "it was not already running.",
            )
        if not current.get("active"):
            return {"ok": True, "recording": False, "skipped": True, "reason": "not_recording"}
        command = build_shortcut_command("recsound")
        await _send_control(app, command)
        _set_recording(app, "audio", {"active": False, "path": current.get("path")})
        await asyncio.sleep(0.2)
        capture = current.get("path")
        size = None
        if capture and Path(capture).is_file():
            size = Path(capture).stat().st_size
        return {
            "ok": True,
            "recording": False,
            "command": command,
            "toggle": True,
            "path": capture,
            "size": size,
            "verified": bool(size),
            "note": "recsound toggled off; Hatari closes and finalises the .wav/.ym file.",
        }

    @server.tool(
        name="media_record_video_start",
        description=(
            "Start AVI recording with the recanim shortcut after setting --avi-file, "
            "--avi-vcodec and --avi-fps. Codec and fps are read when recording starts and "
            "cannot be changed safely while it is running. PNG requires a libpng build."
        ),
        annotations=ToolAnnotations(destructiveHint=True, openWorldHint=False),
    )
    @guarded
    async def media_record_video_start(
        path: str | None = None,
        codec: str = "png",
        fps: int = 25,
    ) -> dict[str, Any]:
        normalized_codec = normalize_video_codec(codec)
        normalized_fps = normalize_video_fps(fps)
        current = _get_recording(app, "video")
        if current and current.get("active"):
            raise error(
                "already_recording",
                "AVI recording is already active for this Hatari run.",
                "Stop it with media_record_video_stop. Codec and fps are read at start and "
                "cannot be changed while recording, so a restart or stop/start is required.",
                extra={"path": current.get("path")},
            )
        if app.supervisor.state not in WRITABLE_SOCKET:
            raise error(
                "not_running",
                f"Hatari is {app.supervisor.state.value}; recording needs a running emulator.",
                "Call emu_start first.",
            )
        if path:
            target = app.guard_path(path)
        else:
            base = app.supervisor.runtime.path if app.supervisor.runtime else Path("/tmp")
            target = base / "hatari.avi"
        if target.suffix.lower() != ".avi":
            raise error("bad_option", f"AVI path must end in .avi: {target}", "Use a .avi filename.")
        target = _prepare_output(target)
        launch_args = [
            "--avirecord",
            "on",
            "--avi-file",
            str(target),
            "--avi-vcodec",
            normalized_codec,
            "--avi-fps",
            str(normalized_fps),
        ]
        commands = [
            f"hatari-option {option}"
            for option in build_avi_options(target, normalized_codec, normalized_fps)
        ]
        await disable_modal_alerts(app)
        for command in commands:
            await _send_control(app, command)
        record_command = build_shortcut_command("recanim")
        await _send_control(app, record_command)
        await asyncio.sleep(0.2)
        verified = target.is_file() and target.stat().st_size > 8192
        _set_recording(
            app,
            "video",
            {"active": True, "path": str(target), "codec": normalized_codec, "fps": normalized_fps},
        )
        return {
            "ok": True,
            "start_requested": True,
            "recording": True if verified else None,
            "path": str(target),
            "codec": normalized_codec,
            "fps": normalized_fps,
            "commands": [*commands, record_command],
            "verified": verified,
            "launch_args": launch_args,
            "note": (
                "recanim is a toggle and Hatari has no recording-state query. This tool only "
                "starts when it has not tracked an active recording for this run. PNG needs "
                "a libpng-enabled build; check log_read if the AVI file does not grow."
            ),
        }

    @server.tool(
        name="media_record_video_stop",
        description=(
            "Stop AVI recording with the recanim shortcut. Only stops a recording this "
            "server tracked as started; Hatari has no query for external recanim state."
        ),
        annotations=ToolAnnotations(destructiveHint=False, openWorldHint=False),
    )
    @guarded
    async def media_record_video_stop() -> dict[str, Any]:
        if app.supervisor.state not in WRITABLE_SOCKET:
            raise error(
                "not_running",
                f"Hatari is {app.supervisor.state.value}; recording needs a running emulator.",
                "Call emu_start first.",
            )
        current = _get_recording(app, "video")
        if current is None:
            raise error(
                "recording_state_unknown",
                "Hatari has no control-channel query for recanim state.",
                "If recording was started outside this server, invoke emu_shortcut recanim "
                "once yourself. This tool refuses because recanim would start recording if "
                "it was not already running.",
            )
        if not current.get("active"):
            return {
                "ok": True,
                "recording": False,
                "skipped": True,
                "reason": "not_recording",
                "path": current.get("path"),
            }
        command = build_shortcut_command("recanim")
        await _send_control(app, command)
        _set_recording(
            app,
            "video",
            {
                "active": False,
                "path": current.get("path"),
                "codec": current.get("codec"),
                "fps": current.get("fps"),
            },
        )
        await asyncio.sleep(0.2)
        path = current.get("path")
        size = Path(path).stat().st_size if path and Path(path).is_file() else None
        return {
            "ok": True,
            "recording": False,
            "command": command,
            "path": path,
            "size": size,
            "verified": bool(size),
            "note": "recanim toggled off; Hatari finalises the AVI index and file.",
        }
