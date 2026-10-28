"""Blocking crash-detective workflow built on the shared MCP examples package.

The Tk UI submits these methods to :class:`common.ui.TaskRunner`.  Nothing here
imports Tk.  One emulator is active at a time; every launch collects evidence,
writes a bundle, and then stops Hatari so the GUI cannot leave an orphan behind.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from pathlib import Path
from typing import Any, Mapping

try:  # package import
    from common import config as config_module
    from common import paths
    from common.catalog import parse_tosec_name, stable_id
    from common.debug_session import DebugSession
    from common.game_runner import (
        ALIVE_STATES,
        TERMINAL_STATES,
        GameSession,
        normalise_launch_path,
        profile_args,
    )
    from common.mcp_client import MCPClient
except ImportError:  # direct script import
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from common import config as config_module  # type: ignore[no-redef]
    from common import paths  # type: ignore[no-redef]
    from common.catalog import parse_tosec_name, stable_id  # type: ignore[no-redef]
    from common.debug_session import DebugSession  # type: ignore[no-redef]
    from common.game_runner import (  # type: ignore[no-redef]
        ALIVE_STATES,
        TERMINAL_STATES,
        GameSession,
        normalise_launch_path,
        profile_args,
    )
    from common.mcp_client import MCPClient  # type: ignore[no-redef]

from . import analysis
from .discovery import PrgTarget, prg_target

DEFAULT_TIMEOUT = 45.0
PRG_SETTLE_SECONDS = 3.0
PRG_NO_LOADER_GRACE = 4.0


def default_state_dir() -> Path:
    return paths.state_dir() / "crash_detective"


def resolve_state_dir(path: str | Path | None) -> Path:
    if path:
        return Path(path).expanduser().resolve()
    return default_state_dir()


def _new_run_id(identifier: str) -> str:
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    slug = "".join(char for char in identifier if char.isalnum())[:10] or "run"
    suffix = int(time.time() * 1000) % 1000
    return f"{stamp}-{slug}-{suffix:03d}"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(256 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _entry_value(entry: Any, key: str, default: Any = None) -> Any:
    if isinstance(entry, Mapping):
        return entry.get(key, default)
    return getattr(entry, key, default)


class CrashDetective:
    """Launch one disk or PRG, collect evidence, diagnose and stop."""

    def __init__(
        self,
        *,
        state_dir: str | Path | None = None,
        config: config_module.Config | None = None,
        client: MCPClient | None = None,
        machine: str = "st",
        tos: str | Path | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        boot_timeout: float | None = None,
        poll_interval: float = 0.4,
        keep_running: bool = False,
    ):
        self.state_dir = resolve_state_dir(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.config = config or config_module.Config(self.state_dir / "config.json")
        self.client = client
        self._owns_client = client is None
        self.machine = (machine or "st").lower()
        self.tos = Path(tos).expanduser() if tos else None
        self.timeout = max(1.0, float(boot_timeout if boot_timeout is not None else timeout))
        self.poll_interval = max(0.1, float(poll_interval))
        self.keep_running = bool(keep_running)
        self._lock = threading.RLock()
        self._session: GameSession | DebugSession | None = None
        self._closed = False
        self.last_run: dict[str, Any] | None = None

    # -- lifecycle -----------------------------------------------------

    def _ensure_client(self) -> MCPClient:
        with self._lock:
            if self._closed:
                raise RuntimeError("crash detective is closed")
            if self.client is None:
                self.client = MCPClient()
                self.client.start()
            return self.client

    def status(self) -> dict[str, Any]:
        client = self.client
        if client is None:
            return {"ok": False, "state": "stopped", "message": "MCP client not started"}
        try:
            return client.call_tool("emu_status", timeout=5.0)
        except BaseException as exc:  # noqa: BLE001 - status probe
            return {"ok": False, "state": "unavailable", "message": str(exc)}

    def abort(self) -> dict[str, Any]:
        with self._lock:
            session = self._session
            client = self.client
        result: dict[str, Any] = {"ok": True, "state": "no-session"}
        if session is not None:
            try:
                result = session.stop(force=True)
            except BaseException as exc:  # noqa: BLE001 - best effort
                result = {"ok": False, "error": str(exc)}
        elif client is not None:
            try:
                result = client.call_tool("emu_stop", force=True, timeout=20.0)
            except BaseException as exc:  # noqa: BLE001
                result = {"ok": False, "error": str(exc)}
        return result

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            session = self._session
            client = self.client
            owns = self._owns_client
        if session is not None:
            try:
                session.stop(force=True)
            except BaseException:
                pass
        if owns and client is not None:
            try:
                client.stop()
            except BaseException:
                pass

    def __enter__(self) -> "CrashDetective":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    # -- public launches ----------------------------------------------

    def launch_disk(
        self,
        entry: Any,
        *,
        machine: str | None = None,
        tos: str | Path | None = None,
        timeout: float | None = None,
        debug_exceptions: bool = False,
        args: list[str] | None = None,
        autorun_path: str | None = None,
    ) -> dict[str, Any]:
        client = self._ensure_client()
        machine = (machine or self.machine or "st").lower()
        tos_path = Path(tos).expanduser() if tos else self.tos
        boundary = float(timeout if timeout is not None else self.timeout)
        guest_path = normalise_launch_path(autorun_path)
        game_id = str(_entry_value(entry, "id", "") or stable_id(_entry_value(entry, "path", entry)))
        title = str(_entry_value(entry, "title", "") or _entry_value(entry, "filename", ""))
        run_id = _new_run_id(game_id)
        frames_dir = self.state_dir / "runs" / run_id / "frames"
        frames_dir.mkdir(parents=True, exist_ok=True)

        session_state = self.state_dir / "emulator" / run_id
        session = GameSession(
            client,
            config=self.config,
            state_dir=session_state,
            default_machine=machine,
            default_tos=tos_path,
            boot_timeout=boundary,
            poll_interval=self.poll_interval,
            min_visible_seconds=1.0,
        )
        with self._lock:
            self._session = session
        extra_args = list(args or [])
        if debug_exceptions:
            extra_args += ["--debug-except", "all"]
        started = time.monotonic()
        try:
            result = session.start_game(
                entry,
                machine=machine,
                tos=tos_path,
                args=extra_args or None,
                timeout=boundary,
                autostart=guest_path,
            )
            status_before_stop = self._safe_status(client)
            logs = self._read_logs(client)
            frames = self._gather_frames(result, session, frames_dir)
            debug: dict[str, Any] = {}
            if not result.ok:
                debug = self._collect_debug(client, session, status_before_stop)
            status_after_stop: dict[str, Any]
            if self.keep_running:
                status_after_stop = status_before_stop
            else:
                try:
                    session.stop(force=True)
                except BaseException:
                    pass
                status_after_stop = self._safe_status(client)
            game = self._disk_game(entry)
            source = self._disk_source(entry, session)
            record = self._record(
                run_id=run_id,
                kind="disk",
                game=game,
                source=source,
                launch={
                    "machine": machine,
                    "profile": machine,
                    "tos": str(tos_path) if tos_path else None,
                    "args": extra_args,
                    "timeout": boundary,
                    "debug_exceptions": bool(debug_exceptions),
                    "autorun_path": guest_path,
                    "profile_args": profile_args(machine),
                },
                result=result,
                status=status_before_stop,
                status_after_stop=status_after_stop,
                logs=logs,
                frames=frames,
                debug=debug,
                duration_ms=int((time.monotonic() - started) * 1000),
            )
            return self._finalize(record, machine=machine, tos=tos_path, args=extra_args, ok=result.ok)
        finally:
            if not self.keep_running:
                try:
                    session.stop(force=True)
                except BaseException:
                    pass
            with self._lock:
                if self._session is session:
                    self._session = None

    def launch_prg(
        self,
        prg: str | Path | PrgTarget,
        *,
        machine: str | None = None,
        tos: str | Path | None = None,
        timeout: float | None = None,
        debug_exceptions: bool = False,
        args: list[str] | None = None,
    ) -> dict[str, Any]:
        target = prg if isinstance(prg, PrgTarget) else prg_target(prg)
        client = self._ensure_client()
        machine = (machine or self.machine or "st").lower()
        tos_path = Path(tos).expanduser() if tos else self.tos
        boundary = float(timeout if timeout is not None else self.timeout)
        run_id = _new_run_id(target.id)
        frames_dir = self.state_dir / "runs" / run_id / "frames"
        frames_dir.mkdir(parents=True, exist_ok=True)

        session_state = self.state_dir / "emulator" / run_id
        session = DebugSession(
            client,
            state_dir=session_state,
            machine=machine,
            tos=tos_path,
            poll_interval=self.poll_interval,
        )
        with self._lock:
            self._session = session
        started = time.monotonic()
        status: dict[str, Any] = {}
        logs: dict[str, Any] = {"stderr": [], "console": []}
        frames: list[dict[str, Any]] = []
        debug: dict[str, Any] = {}
        failure_reason: str | None = None
        message = ""
        ok = False
        try:
            try:
                session.load_prg(
                    target.path,
                    machine=machine,
                    tos=tos_path,
                    args=list(args or []),
                    wait=False,
                    debug_exceptions="all" if debug_exceptions else None,
                    timeout=boundary,
                )
            except BaseException as exc:  # noqa: BLE001 - reported in evidence
                failure_reason = "start_failed"
                message = f"load_prg failed: {exc}"
                status = self._safe_status(client)
                logs = self._read_logs(client)
            else:
                ok, failure_reason, message, status, logs, frames = self._watch_prg(
                    client,
                    session,
                    frames_dir,
                    started=started,
                    timeout=boundary,
                )
                if not ok:
                    debug = self._collect_debug(client, session, status)
            logs = self._read_logs(client) if not logs.get("stderr") and not logs.get("console") else logs
            status = status or self._safe_status(client)
            if not frames:
                frames = self._gather_frames(None, session, frames_dir)
            status_after_stop: dict[str, Any]
            if self.keep_running:
                status_after_stop = status
            else:
                try:
                    session.stop(force=True)
                except BaseException:
                    pass
                status_after_stop = self._safe_status(client)
            record = self._record(
                run_id=run_id,
                kind="prg",
                game={
                    "id": target.id,
                    "title": target.title,
                    "filename": target.filename,
                    "path": str(target.path),
                    "size": target.size,
                    "sha256": target.sha256,
                },
                source={
                    "kind": "prg",
                    "path": str(target.path),
                    "filename": target.filename,
                    "sha256": target.sha256,
                    "size": target.size,
                    "workspace": str(session.state_dir / "run" / f"prg-{hashlib.sha1(str(target.path).encode()).hexdigest()[:8]}"),
                },
                launch={
                    "machine": machine,
                    "profile": machine,
                    "tos": str(tos_path) if tos_path else None,
                    "args": list(args or []),
                    "timeout": boundary,
                    "debug_exceptions": bool(debug_exceptions),
                    "profile_args": profile_args(machine),
                },
                result={
                    "ok": ok,
                    "status": "success" if ok else "failure",
                    "message": message,
                    "failure_reason": failure_reason,
                    "duration_ms": int((time.monotonic() - started) * 1000),
                },
                status=status,
                status_after_stop=status_after_stop,
                logs=logs,
                frames=frames,
                debug=debug,
                duration_ms=int((time.monotonic() - started) * 1000),
                resolution=self._resolution(frames),
            )
            return self._finalize(record, machine=machine, tos=tos_path, args=list(args or []), ok=ok)
        finally:
            if not self.keep_running:
                try:
                    session.stop(force=True)
                except BaseException:
                    pass
            with self._lock:
                if self._session is session:
                    self._session = None

    # -- PRG watcher ---------------------------------------------------

    def _watch_prg(
        self,
        client: MCPClient,
        session: DebugSession,
        frames_dir: Path,
        *,
        started: float,
        timeout: float,
    ) -> tuple[bool, str | None, str, dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
        deadline = time.monotonic() + max(1.0, timeout)
        frames: list[dict[str, Any]] = []
        timeline: list[dict[str, Any]] = []
        logs: dict[str, Any] = {"stderr": [], "console": []}
        status: dict[str, Any] = {}
        previous_digest: str | None = None
        visible_since: float | None = None
        program_started_at: float | None = None
        max_ratio = 0.0
        capture_errors = 0
        index = 0
        while time.monotonic() < deadline:
            now = time.monotonic()
            try:
                status = session.status()
            except BaseException as exc:  # noqa: BLE001
                return False, "status_failed", f"emu_status failed: {exc}", status, logs, frames
            state = str(status.get("state") or "unknown")
            logs = self._read_logs(client)
            lines = list(logs.get("stderr") or []) + list(logs.get("console") or [])
            exception_line = _exception_text_from(lines, status)
            if exception_line:
                return False, "exception", f"guest exception: {exception_line}", status, logs, frames
            fatal = _fatal_from(lines)
            if fatal:
                return False, "console_error", f"fatal-looking log line: {fatal}", status, logs, frames
            if state in TERMINAL_STATES:
                return (
                    False,
                    str(status.get("exit_reason") or "emulator_exited"),
                    f"emulator left running state: {state}",
                    status,
                    logs,
                    frames,
                )
            if status.get("at_prompt") or state == "at_prompt":
                return False, "debugger_prompt", "emulator entered the debugger prompt", status, logs, frames
            if state == "unresponsive":
                return False, "emulator_unresponsive", "emulator stopped answering heartbeats", status, logs, frames
            if any(
                marker in line.casefold()
                for line in logs.get("stderr") or []
                for marker in ("reading symbols from program", "tos executable", "can not parse relocation table")
            ):
                if program_started_at is None:
                    program_started_at = now

            index += 1
            target = frames_dir / f"frame-{index:04d}.bmp"
            capture: dict[str, Any] | None = None
            stats: dict[str, Any] | None = None
            capture_error: str | None = None
            try:
                capture = client.call_tool(
                    "screen_frame_capture", path=str(target), format="bmp", timeout=15.0
                )
                stats = analysis.bmp_stats(target)
                frames.append(
                    {
                        "path": str(target),
                        "sha256": str(capture.get("sha256") or analysis.sha256_file(target)),
                        "width": int(stats["width"]),
                        "height": int(stats["height"]),
                        "nonblack_ratio": float(stats["nonblack_ratio"]),
                        "t_ms": int((now - started) * 1000),
                    }
                )
                capture_errors = 0
            except BaseException as exc:  # noqa: BLE001 - capture is best effort
                capture_error = str(exc)
                capture_errors += 1
            ratio = float(stats["nonblack_ratio"]) if stats else 0.0
            digest = str((capture or {}).get("sha256") or (frames[-1].get("sha256") if frames else ""))
            max_ratio = max(max_ratio, ratio)
            if ratio >= analysis.NONBLACK_THRESHOLD:
                if visible_since is None:
                    visible_since = now
            else:
                visible_since = None
            timeline.append(
                {
                    "t_ms": int((now - started) * 1000),
                    "state": state,
                    "nonblack_ratio": ratio,
                    "digest": digest[:16],
                    "capture_error": capture_error,
                }
            )
            if capture_errors >= 3 and max_ratio < analysis.NONBLACK_THRESHOLD:
                return False, "capture_failed", f"screen capture failed repeatedly: {capture_error}", status, logs, frames
            if previous_digest is not None:
                changed = bool(digest) and digest != previous_digest
            else:
                changed = False
            if digest:
                previous_digest = digest

            if ratio >= analysis.NONBLACK_THRESHOLD and (changed or visible_since is not None):
                settle_from = max(program_started_at or 0.0, visible_since or now)
                if program_started_at is None:
                    settle_from = (visible_since or now) + PRG_NO_LOADER_GRACE
                if now - settle_from >= PRG_SETTLE_SECONDS:
                    message = f"visible PRG frame after {int((now - started) * 1000)} ms"
                    return True, None, message, status, logs, frames
            time.sleep(self.poll_interval)

        reason = "timeout_blank_screen" if max_ratio < analysis.NONBLACK_THRESHOLD else "timeout_no_stable_frame"
        message = (
            "PRG boot timed out with a black/blank screen"
            if reason == "timeout_blank_screen"
            else "PRG boot timed out before a stable visible frame"
        )
        return False, reason, message, status, logs, frames

    # -- evidence helpers ---------------------------------------------

    def _safe_status(self, client: MCPClient) -> dict[str, Any]:
        try:
            return client.call_tool("emu_status", timeout=10.0)
        except BaseException as exc:  # noqa: BLE001
            return {"ok": False, "state": "unavailable", "message": str(exc)}

    def _read_logs(self, client: MCPClient) -> dict[str, Any]:
        logs: dict[str, Any] = {"stderr": [], "console": []}
        for key, tool in (("stderr", "log_read"), ("console", "console_read")):
            try:
                result = client.call_tool(tool, lines=160, timeout=8.0)
            except BaseException:
                continue
            entries = result.get("entries", [])
            logs[key] = [
                entry.get("text") if isinstance(entry, Mapping) else str(entry)
                for entry in entries[-160:]
            ]
        return logs

    def _gather_frames(
        self,
        result: Any | None,
        session: GameSession | DebugSession,
        fallback_dir: Path,
    ) -> list[dict[str, Any]]:
        candidates: list[Path] = []
        evidence = getattr(result, "evidence", {}) if result is not None else {}
        if isinstance(evidence, Mapping):
            for key in ("frame_path", "last_frame"):
                value = evidence.get(key)
                if isinstance(value, str):
                    candidates.append(Path(value))
        value = getattr(result, "frame_path", None) if result is not None else None
        if isinstance(value, str):
            candidates.append(Path(value))
        candidates.extend(sorted(fallback_dir.glob("*.bmp")))
        game_id = getattr(session, "current_game_id", None)
        if isinstance(session, GameSession) and game_id:
            session_state = Path(getattr(session, "state_dir", self.state_dir / "emulator"))
            candidates.extend(sorted((session_state / "run" / str(game_id) / "frames").glob("*.bmp")))
        frames: list[dict[str, Any]] = []
        seen: set[str] = set()
        for path in candidates:
            try:
                resolved = str(path.expanduser().resolve())
            except OSError:
                continue
            if resolved in seen or not path.is_file():
                continue
            seen.add(resolved)
            try:
                stats = analysis.bmp_stats(path)
            except (OSError, ValueError):
                stats = {}
            frames.append(
                {
                    "path": resolved,
                    "sha256": analysis.sha256_file(path),
                    "width": stats.get("width"),
                    "height": stats.get("height"),
                    "nonblack_ratio": stats.get("nonblack_ratio"),
                    "size": path.stat().st_size,
                }
            )
        return frames

    def _collect_debug(
        self,
        client: MCPClient,
        session: GameSession | DebugSession,
        status: Mapping[str, Any],
    ) -> dict[str, Any]:
        debug: dict[str, Any] = {"errors": []}
        state = str(status.get("state") or "")
        # Freeze a running machine at the next instruction so register,
        # disassembly and stack reads are coherent.  A prompt is left untouched.
        if state in ALIVE_STATES and not status.get("at_prompt"):
            helper = DebugSession(
                client,
                state_dir=Path(getattr(session, "state_dir", self.state_dir / "emulator")),
                machine=self.machine,
                poll_interval=0.1,
            )
            try:
                helper.ensure_prompt(timeout=5.0)
            except BaseException as exc:  # noqa: BLE001 - best effort
                debug["errors"].append(f"ensure_prompt: {exc}")

        def call(name: str, **kwargs: Any) -> Any:
            try:
                return client.call_tool(name, timeout=8.0, **kwargs)
            except BaseException as exc:  # noqa: BLE001
                debug["errors"].append(f"{name}: {exc}")
                return None

        registers = call("dbg_registers_get")
        if registers is not None:
            debug["registers"] = registers
            register_map = registers.get("registers") if isinstance(registers, Mapping) else {}
            if isinstance(register_map, Mapping) and isinstance(register_map.get("pc"), int):
                debug["pc"] = register_map["pc"]
        pc = debug.get("pc")
        if isinstance(pc, int):
            debug["disassembly"] = call("dbg_disassemble", address=f"0x{pc:x}", count=16)
        debug["stack"] = call("dbg_stack_dump", words=32)
        debug["backtrace"] = call("dbg_backtrace", max_frames=16)
        debug["exception"] = call("dbg_exception_get")
        debug["media_status"] = call("media_status")
        return debug

    def _disk_game(self, entry: Any) -> dict[str, Any]:
        try:
            if hasattr(entry, "as_dict"):
                game = dict(entry.as_dict())
            elif isinstance(entry, Mapping):
                game = dict(entry)
            else:
                path = Path(_entry_value(entry, "path", entry))
                parsed = parse_tosec_name(path.name)
                game = {
                    "id": stable_id(path),
                    "path": str(path),
                    "title": parsed.title,
                    "filename": path.name,
                    "year": parsed.year,
                    "publisher": parsed.publisher,
                    "genre": parsed.genre,
                    "disk": parsed.disk,
                    "disk_total": parsed.disk_total,
                    "part": parsed.part,
                    "tags": list(parsed.tags),
                }
        except BaseException:
            game = {"path": str(_entry_value(entry, "path", entry))}
        game.setdefault("title", Path(str(game.get("path", ""))).stem)
        return game

    def _disk_source(self, entry: Any, session: GameSession) -> dict[str, Any]:
        path = Path(str(_entry_value(entry, "path", entry)))
        source: dict[str, Any] = {
            "kind": "disk",
            "path": str(path),
            "filename": path.name,
            "size": path.stat().st_size if path.is_file() else None,
            "sha256": _sha256(path) if path.is_file() else None,
            "materialized": str(session.current_disk) if session.current_disk else None,
        }
        if path.suffix.lower() == ".zip":
            try:
                source["zip_members"] = paths.zip_disk_members(path)
            except BaseException as exc:  # noqa: BLE001
                source["zip_members"] = []
                source["zip_error"] = str(exc)
        return source

    def _record(
        self,
        *,
        run_id: str,
        kind: str,
        game: Mapping[str, Any],
        source: Mapping[str, Any],
        launch: Mapping[str, Any],
        result: Any,
        status: Mapping[str, Any],
        logs: Mapping[str, Any],
        frames: list[dict[str, Any]],
        debug: Mapping[str, Any],
        duration_ms: int,
        status_after_stop: Mapping[str, Any] | None = None,
        resolution: list[int] | tuple[int, int] | None = None,
    ) -> dict[str, Any]:
        if hasattr(result, "as_dict"):
            result_dict = result.as_dict()
        elif isinstance(result, Mapping):
            result_dict = dict(result)
        else:
            result_dict = {"ok": False, "message": str(result)}
        result_dict.setdefault("duration_ms", duration_ms)
        if resolution is None:
            value = result_dict.get("resolution")
            if isinstance(value, (list, tuple)) and len(value) == 2:
                resolution = [int(value[0]), int(value[1])]
            elif frames:
                last = frames[-1]
                if last.get("width") and last.get("height"):
                    resolution = [int(last["width"]), int(last["height"])]
        if resolution is not None:
            result_dict["resolution"] = list(resolution)
        learned = {}
        try:
            learned = self.config.get_game(str(game.get("id") or ""))
        except BaseException:
            learned = {}
        return {
            "schema": analysis.SCHEMA_VERSION,
            "run_id": run_id,
            "created_at": int(time.time()),
            "kind": kind,
            "game": analysis._jsonable(dict(game)),
            "source": analysis._jsonable(dict(source)),
            "launch": analysis._jsonable(dict(launch)),
            "learned_config": analysis._jsonable(learned),
            "result": analysis._jsonable(result_dict),
            "status": analysis._jsonable(dict(status)),
            "status_after_stop": analysis._jsonable(dict(status_after_stop or {})),
            "timeline": analysis._jsonable(list(getattr(result, "evidence", {}).get("samples", []) if getattr(result, "evidence", None) else [])),
            "frames": analysis._jsonable(frames),
            "logs": analysis._jsonable(dict(logs)),
            "debug": analysis._jsonable(dict(debug)),
        }

    def _resolution(self, frames: list[Mapping[str, Any]]) -> list[int] | None:
        for frame in reversed(frames):
            width = frame.get("width")
            height = frame.get("height")
            if isinstance(width, int) and isinstance(height, int) and width > 0 and height > 0:
                return [width, height]
        return None

    def _finalize(
        self,
        record: dict[str, Any],
        *,
        machine: str,
        tos: str | Path | None,
        args: list[str],
        ok: bool,
    ) -> dict[str, Any]:
        classification = analysis.classify(record)
        hypotheses = analysis.diagnose(record, classification)
        record["bundle"] = str(
            self.state_dir / "runs" / str(record["run_id"]) / "bundle"
        )
        bundle = analysis.write_bundle(
            self.state_dir, record, classification, hypotheses, run_id=str(record["run_id"])
        )
        record["bundle"] = str(bundle)
        result = record.get("result") or {}
        status = "success" if ok else "failure"
        try:
            self.config.record_launch(
                str((record.get("game") or {}).get("id") or ""),
                machine=machine,
                profile=machine,
                tos=str(tos) if tos else None,
                args=list(args),
                resolution=result.get("resolution"),
                status=status,
                failure_reason=result.get("failure_reason"),
                message=result.get("message"),
                success=ok,
                extra={
                    "crash_detective": {
                        "run_id": record.get("run_id"),
                        "bundle": str(bundle),
                        "classification": classification.get("category"),
                        "booted": classification.get("booted"),
                    }
                },
            )
        except BaseException:
            pass
        payload = {
            "record": record,
            "classification": classification,
            "hypotheses": hypotheses,
            "bundle": str(bundle),
        }
        self.last_run = payload
        try:
            config_module.atomic_write_json(self.state_dir / "last_run.json", analysis._jsonable(payload))
        except BaseException:
            pass
        return payload


def _exception_text_from(lines: list[str], status: Mapping[str, Any]) -> str | None:
    exception = status.get("exception")
    if isinstance(exception, Mapping) and exception:
        return " ".join(str(exception.get(key)) for key in ("type", "line", "message") if exception.get(key)) or json.dumps(exception, default=str)
    if isinstance(exception, str) and exception.strip():
        return exception.strip()
    for line in lines:
        if analysis.EXCEPTION_RE.search(line):
            return line.strip()
    return None


def _fatal_from(lines: list[str]) -> str | None:
    for line in lines:
        if analysis._noise(line):
            continue
        if analysis.FATAL_RE.search(line):
            return line.strip()
    return None
