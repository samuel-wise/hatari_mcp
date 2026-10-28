"""Start, classify, swap and stop games through the Hatari MCP wrapper."""

from __future__ import annotations

import struct
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:  # package import
    from . import config as config_module
    from . import paths
    from .catalog import stable_id
    from .mcp_client import MCPClient, MCPToolError
except ImportError:  # direct script import
    import config as config_module  # type: ignore[no-redef]
    import paths  # type: ignore[no-redef]
    from catalog import stable_id  # type: ignore[no-redef]
    from mcp_client import MCPClient, MCPToolError  # type: ignore[no-redef]

ALIVE_STATES = {"starting", "running", "at_prompt", "remote_paused", "unresponsive"}
TERMINAL_STATES = {"stopped", "stopping", "exited", "crashed", "failed"}
NONBLACK_THRESHOLD = 0.01
TOS_ROM_START = 0xE00000
TOS_ROM_END = 0xFFFFFF

PROFILE_ARGS: dict[str, list[str]] = {
    "st": [
        "--machine",
        "st",
        "--memsize",
        "1",
        "--sound",
        "off",
        "--confirm-quit",
        "off",
        "--fast-boot",
        "on",
    ],
    "ste": [
        "--machine",
        "ste",
        "--memsize",
        "2",
        "--blitter",
        "on",
        "--sound",
        "off",
        "--confirm-quit",
        "off",
        "--fast-boot",
        "on",
    ],
    "tt": [
        "--machine",
        "tt",
        "--memsize",
        "4",
        "--ttram",
        "4",
        "--sound",
        "off",
        "--confirm-quit",
        "off",
        "--fast-boot",
        "on",
    ],
    "falcon": [
        "--machine",
        "falcon",
        "--memsize",
        "4",
        "--dsp",
        "emu",
        "--sound",
        "off",
        "--confirm-quit",
        "off",
        "--fast-boot",
        "on",
    ],
}


def profile_args(machine: str) -> list[str]:
    return list(PROFILE_ARGS.get((machine or "st").lower(), PROFILE_ARGS["st"]))


def _has_option(args: list[str], option: str) -> bool:
    return any(item == option or item.startswith(f"{option}=") for item in args)


def normalise_launch_path(value: str | None) -> str | None:
    """Return an Atari guest path for ``--auto``.

    The launcher boots disks from A:, so relative paths are anchored there.
    Users may enter ``RUNME.PRG``, ``FOLDER/RUNME.TOS`` or a full ``A:\\...``
    path.  Paths are case-preserving here; Hatari upcases them internally.
    """
    if value is None:
        return None
    text = str(value).strip().replace("/", "\\")
    if not text:
        return None
    if any(ord(ch) < 32 for ch in text):
        raise ValueError("launch path contains control characters")
    if len(text) >= 2 and text[1] == ":" and text[0].isalpha():
        drive = text[0].upper()
        rest = text[2:]
        if not rest:
            raise ValueError("launch path has no file name")
        if not rest.startswith("\\"):
            rest = "\\" + rest
        return drive + ":" + rest
    text = text.lstrip("\\")
    if not text or text.startswith("..") or "\\..\\" in text:
        raise ValueError("launch path must stay inside the boot disk")
    return "A:\\" + text


def _strip_disk_args(items: list[str]) -> list[str]:
    """Drop --disk-a/--disk-b and their values from learned arguments."""
    stripped: list[str] = []
    skip_value = False
    for item in items:
        if skip_value:
            skip_value = False
            continue
        if item in {"--disk-a", "--disk-b"}:
            skip_value = True
            continue
        if item.startswith("--disk-a=") or item.startswith("--disk-b="):
            continue
        stripped.append(item)
    return stripped


@dataclass
class GameResult:
    ok: bool
    game_id: str
    title: str
    machine: str
    tos: str | None
    frame_path: str | None = None
    resolution: tuple[int, int] | None = None
    status: str = "unknown"
    message: str = ""
    failure_reason: str | None = None
    evidence: dict[str, Any] = field(default_factory=dict)
    duration_ms: int = 0
    learned: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "game_id": self.game_id,
            "title": self.title,
            "machine": self.machine,
            "tos": self.tos,
            "frame_path": self.frame_path,
            "resolution": self.resolution,
            "status": self.status,
            "message": self.message,
            "failure_reason": self.failure_reason,
            "evidence": self.evidence,
            "duration_ms": self.duration_ms,
            "learned": self.learned,
        }


def _bmp_stats(path: str | Path) -> dict[str, Any]:
    """Small stdlib BMP sampler used for the non-black frame heuristic."""
    data = Path(path).read_bytes()
    if len(data) < 54 or data[:2] != b"BM":
        raise ValueError("not a BMP frame")
    offset = struct.unpack_from("<I", data, 10)[0]
    width, raw_height = struct.unpack_from("<ii", data, 18)
    bpp = struct.unpack_from("<H", data, 28)[0]
    compression = struct.unpack_from("<I", data, 30)[0]
    if width <= 0 or raw_height == 0 or bpp not in (24, 32) or compression != 0:
        raise ValueError("unsupported BMP frame")
    height = abs(raw_height)
    top_down = raw_height < 0
    stride = ((width * bpp + 31) // 32) * 4
    bytes_per_pixel = bpp // 8
    if offset + stride * height > len(data):
        raise ValueError("truncated BMP frame")

    # Ignore the emulator status bar at the bottom; it is always non-black and
    # would make a blank game screen look alive.
    status_height = min(height // 3, max(16, height // 14))
    content_height = max(1, height - status_height)
    step_x = max(1, width // 160)
    step_y = max(1, content_height // 120)
    sampled = 0
    nonblack = 0
    content_sampled = 0
    content_nonblack = 0
    colors: set[tuple[int, int, int]] = set()
    for y in range(0, height, step_y):
        source_row = y if top_down else height - 1 - y
        row = offset + source_row * stride
        content_row = y < content_height
        for x in range(0, width, step_x):
            pixel = row + x * bytes_per_pixel
            blue = data[pixel]
            green = data[pixel + 1]
            red = data[pixel + 2]
            sampled += 1
            is_nonblack = max(red, green, blue) >= 16
            if is_nonblack:
                nonblack += 1
            if content_row:
                content_sampled += 1
                if is_nonblack:
                    content_nonblack += 1
                colors.add((red, green, blue))
    return {
        "width": width,
        "height": height,
        "bpp": bpp,
        "sampled_pixels": sampled,
        "nonblack_pixels": nonblack,
        "nonblack_ratio": round(nonblack / sampled, 6) if sampled else 0.0,
        "content_height": content_height,
        "content_sampled_pixels": content_sampled,
        "content_nonblack_pixels": content_nonblack,
        "content_nonblack_ratio": round(content_nonblack / content_sampled, 6)
        if content_sampled
        else 0.0,
        "unique_colors": len(colors),
    }


def _exception_of(status: dict[str, Any]) -> dict[str, Any] | None:
    value = status.get("exception")
    if isinstance(value, dict) and value:
        return value
    if status.get("has_exception"):
        return {"type": "exception", "line": status.get("last_error")}
    return None


def _halt_dialog_suspected(
    status: dict[str, Any],
    *,
    capture_failures: int,
    capture_error: str | None,
) -> bool:
    """Infer that Hatari's CPU-halt dialog was closed with Quit.

    A double bus/address error opens Hatari's modal halt dialog.  While it is
    open the control socket is not polled, so a frame capture times out; if the
    user then chooses Quit Hatari the child exits cleanly (code 0) and the
    supervisor reports ``process_exit``.  That generic reason hides the real
    guest fault, so treat a clean exit that followed a control timeout as
    ``guest_cpu_halt``.
    """
    if capture_failures < 1 or not capture_error:
        return False
    if "timeout" not in capture_error.casefold():
        return False
    if status.get("program_exit_code") is not None:
        return False
    if status.get("exit_cause") == "requested_stop":
        return False
    if str(status.get("exit_reason") or "") != "process_exit":
        return False
    return status.get("returncode") in (0, None)


class GameSession:
    """One game at a time, with failure evidence and learned launch config."""

    def __init__(
        self,
        client: MCPClient,
        *,
        config: config_module.Config | None = None,
        state_dir: str | Path | None = None,
        default_machine: str = "st",
        default_tos: str | Path | None = None,
        boot_timeout: float = 45.0,
        poll_interval: float = 0.4,
        min_visible_seconds: float = 1.0,
        disk_mode: str = "symlink",
        display: bool = False,
        turbo: bool = True,
        disk_turbo_seconds: float = 2.0,
    ):
        self.client = client
        self.config = config or config_module.Config()
        self.state_dir = Path(state_dir).expanduser().resolve() if state_dir else paths.state_dir()
        self.default_machine = default_machine
        self.default_tos = Path(default_tos).expanduser() if default_tos else None
        self.boot_timeout = float(boot_timeout)
        self.poll_interval = max(0.05, float(poll_interval))
        self.min_visible_seconds = max(0.0, float(min_visible_seconds))
        self.disk_mode = disk_mode
        self.display = bool(display)
        self.turbo = bool(turbo)
        self.disk_turbo_seconds = max(0.0, float(disk_turbo_seconds))
        self.current_game_id: str | None = None
        self.current_title: str | None = None
        self.current_machine: str | None = None
        self.current_tos: str | None = None
        self.current_disk: Path | None = None
        self.last_result: GameResult | None = None
        self._frame_index = 0
        self._turbo_early_restored = False

    # -- small MCP helpers ---------------------------------------------

    def _status(self) -> dict[str, Any]:
        return self.client.call_tool("emu_status", timeout=10.0)

    def _set_boot_turbo(self, enabled: bool) -> bool:
        options = (
            "--cpuclock 32 --fast-forward on"
            if enabled
            else "--cpuclock 8 --fast-forward off"
        )
        try:
            self.client.call_tool("emu_set_option", options=options, timeout=15.0)
            return True
        except BaseException:
            return False

    def _stop_hard(self) -> dict[str, Any]:
        try:
            return self.client.call_tool("emu_stop", force=True, timeout=20.0)
        except MCPToolError:
            return {}

    def _tails(self) -> dict[str, Any]:
        tails: dict[str, Any] = {}
        for name, tool, extra in (
            ("log", "log_read", {"lines": 80}),
            ("console", "console_read", {"lines": 80}),
        ):
            try:
                result = self.client.call_tool(tool, timeout=8.0, **extra)
            except BaseException:
                tails[name] = []
                continue
            entries = result.get("entries", [])
            tails[name] = [
                entry.get("text") if isinstance(entry, dict) else str(entry)
                for entry in entries[-80:]
            ]
        return tails

    def _capture(
        self, game_id: str
    ) -> tuple[dict[str, Any] | None, str | None, dict[str, Any] | None, str | None]:
        directory = self.state_dir / "run" / game_id / "frames"
        directory.mkdir(parents=True, exist_ok=True)
        self._frame_index += 1
        target = directory / f"frame-{self._frame_index:04d}.bmp"
        try:
            capture = self.client.call_tool(
                "screen_frame_capture",
                path=str(target),
                format="bmp",
                timeout=15.0,
            )
        except BaseException as exc:
            return None, None, None, str(exc)
        try:
            stats = _bmp_stats(target)
        except (OSError, ValueError, struct.error) as exc:
            return capture, str(target), None, str(exc)
        return capture, str(target), stats, None

    # -- public API -----------------------------------------------------

    @property
    def running(self) -> bool:
        try:
            return str(self._status().get("state")) in ALIVE_STATES
        except BaseException:
            return False

    def start_emutos(
        self,
        *,
        machine: str | None = None,
        tos: str | Path | None = None,
        args: list[str] | None = None,
        timeout: float | None = None,
    ) -> GameResult:
        return self._start(
            None,
            game_id="__emutos__",
            title="EmuTOS desktop",
            machine=machine,
            tos=tos,
            args=args,
            timeout=timeout,
            require_game=False,
        )

    def start_game(
        self,
        game: Any,
        *,
        machine: str | None = None,
        profile: str | None = None,
        tos: str | Path | None = None,
        args: list[str] | None = None,
        timeout: float | None = None,
        autostart: str | None = None,
    ) -> GameResult:
        game_id = str(getattr(game, "id", "") or (stable_id(game) if game else "__game__"))
        title = str(getattr(game, "title", "") or Path(str(game)).name)
        return self._start(
            game,
            game_id=game_id,
            title=title,
            machine=machine,
            profile=profile,
            tos=tos,
            args=args,
            timeout=timeout,
            autostart=autostart,
        )

    def _start(
        self,
        game: Any | None,
        *,
        game_id: str,
        title: str,
        machine: str | None,
        profile: str | None = None,
        tos: str | Path | None = None,
        args: list[str] | None = None,
        timeout: float | None = None,
        require_game: bool = True,
        autostart: str | None = None,
    ) -> GameResult:
        started = time.monotonic()
        if self.running:
            self._stop_hard()

        learned = self.config.learned_launch(game_id) if self.config else {}
        machine = (machine or learned.get("machine") or self.default_machine).lower()
        if machine not in PROFILE_ARGS:
            machine = "st"
        profile = profile or learned.get("profile") or machine

        chosen_tos: Path | None = None
        if tos is not None:
            chosen_tos = Path(tos).expanduser()
        elif learned.get("tos"):
            candidate = Path(str(learned["tos"])).expanduser()
            if candidate.is_file():
                chosen_tos = candidate
        elif self.default_tos is not None and self.default_tos.is_file():
            chosen_tos = self.default_tos
        else:
            chosen_tos = paths.tos_for_machine(machine)

        try:
            launch_path = normalise_launch_path(autostart)
        except ValueError as exc:
            result = GameResult(
                ok=False,
                game_id=game_id,
                title=title,
                machine=machine,
                tos=str(chosen_tos) if chosen_tos else None,
                status="failed",
                message=f"invalid launch path: {exc}",
                failure_reason="invalid_launch_path",
                evidence={"launch_path": autostart},
                duration_ms=int((time.monotonic() - started) * 1000),
            )
            self.last_result = result
            self._record(result, [], profile, launch_path=None)
            return result

        launch_args = profile_args(machine)
        extra_args: list[str] = []
        if (
            args is None
            and isinstance(learned.get("args"), list)
            and learned.get("machine") in (None, machine)
        ):
            extra_args = _strip_disk_args([str(item) for item in learned["args"]])
        elif args:
            extra_args = _strip_disk_args([str(item) for item in args])
        launch_args.extend(extra_args)

        turbo = bool(self.turbo) and not (
            _has_option(launch_args, "--cpuclock") or _has_option(launch_args, "--fast-forward")
        )
        if turbo:
            launch_args.extend(["--cpuclock", "32", "--fast-forward", "on"])

        disk_path: Path | None = None
        if game is not None:
            source = getattr(game, "path", None) or game
            try:
                disk_path = paths.materialize_disk(
                    source, self.state_dir / "run" / game_id / "disk", mode=self.disk_mode
                )
            except (OSError, ValueError) as exc:
                result = GameResult(
                    ok=False,
                    game_id=game_id,
                    title=title,
                    machine=machine,
                    tos=str(chosen_tos) if chosen_tos else None,
                    status="failed",
                    message=f"could not prepare disk: {exc}",
                    failure_reason="disk_prepare_failed",
                    duration_ms=int((time.monotonic() - started) * 1000),
                )
                self.last_result = result
                self._record(result, extra_args, profile, launch_path=launch_path)
                return result
            launch_args.extend(["--disk-a", str(disk_path)])

        if launch_path:
            launch_args.extend(["--auto", launch_path])

        start_timeout = max(60.0, (timeout or self.boot_timeout) + 20.0)
        try:
            self.client.call_tool(
                "emu_start",
                tos=str(chosen_tos) if chosen_tos else None,
                args=launch_args,
                display=self.display,
                timeout=start_timeout,
            )
        except BaseException as exc:
            result = GameResult(
                ok=False,
                game_id=game_id,
                title=title,
                machine=machine,
                tos=str(chosen_tos) if chosen_tos else None,
                status="failed",
                message=f"emu_start failed: {exc}",
                failure_reason="start_failed",
                evidence={"error": str(exc), "launch_path": launch_path},
                duration_ms=int((time.monotonic() - started) * 1000),
            )
            self.last_result = result
            self._record(result, extra_args, profile, launch_path=launch_path)
            return result

        self.current_game_id = game_id
        self.current_title = title
        self.current_machine = machine
        self.current_tos = str(chosen_tos) if chosen_tos else None
        self.current_disk = disk_path
        self._frame_index = 0
        self._turbo_early_restored = False

        try:
            result = self._wait_for_game(
                game_id=game_id,
                title=title,
                started=started,
                timeout=timeout or self.boot_timeout,
                require_game=require_game,
                turbo=turbo,
            )
        finally:
            restored = self._set_boot_turbo(False) if turbo else True

        result.evidence.setdefault(
            "boot_turbo",
            {
                "enabled": turbo,
                "restored": restored,
                "early_restore": self._turbo_early_restored,
            },
        )
        result.machine = machine
        result.tos = str(chosen_tos) if chosen_tos else None
        if launch_path:
            result.evidence.setdefault("launch_path", launch_path)
        self.last_result = result
        self._record(result, extra_args, profile, launch_path=launch_path)
        return result

    def _record(
        self,
        result: GameResult,
        args: list[str],
        profile: str,
        *,
        launch_path: str | None = None,
    ) -> None:
        if self.config is None:
            return
        try:
            result.learned = self.config.record_launch(
                result.game_id,
                machine=result.machine,
                profile=profile,
                tos=result.tos,
                args=list(args),
                resolution=result.resolution,
                status="success" if result.ok else "failure",
                failure_reason=result.failure_reason,
                message=result.message,
                success=result.ok,
                extra={"launch_path": launch_path} if launch_path else None,
            )
        except OSError:
            result.learned = {}

    def _pc(self) -> int | None:
        try:
            result = self.client.call_tool("dbg_registers_get", timeout=5.0)
        except BaseException:
            return None
        registers = result.get("registers") if isinstance(result, dict) else None
        if not isinstance(registers, dict):
            return None
        value = registers.get("pc")
        if value is None:
            return None
        try:
            if isinstance(value, str):
                text = value.strip().removeprefix("$")
                return int(text, 16) if not text.lower().startswith("0x") else int(text, 0)
            return int(value)
        except (TypeError, ValueError):
            return None

    def _wait_for_game(
        self,
        *,
        game_id: str,
        title: str,
        started: float,
        timeout: float,
        require_game: bool = True,
        turbo: bool = False,
    ) -> GameResult:
        deadline = time.monotonic() + max(1.0, timeout)
        turbo_active = turbo
        visible_since: float | None = None
        previous_digest: str | None = None
        boot_digest: str | None = None
        post_boot_since: float | None = None
        ram_since: float | None = None
        stable_since: float | None = None
        samples: list[dict[str, Any]] = []
        last_frame: str | None = None
        last_stats: dict[str, Any] | None = None
        last_status: dict[str, Any] | None = None
        last_pc: int | None = None
        seen_ram_pc = False
        max_nonblack = 0.0
        capture_error: str | None = None
        capture_failures = 0

        while time.monotonic() < deadline:
            try:
                status = self._status()
            except BaseException as exc:
                return self._failure(
                    game_id, title, started, "status_failed", f"emu_status failed: {exc}", {}
                )
            last_status = status
            state = str(status.get("state", "unknown"))
            exception = _exception_of(status)
            if exception:
                capture, frame, stats, capture_error = self._capture(game_id)
                evidence = {
                    "status": status,
                    "frame_path": frame,
                    "exception": exception,
                    **self._tails(),
                }
                return self._failure(
                    game_id,
                    title,
                    started,
                    "exception",
                    f"guest exception: {exception}",
                    evidence,
                    frame,
                    stats,
                )
            if state in TERMINAL_STATES:
                halt = _halt_dialog_suspected(
                    status,
                    capture_failures=capture_failures,
                    capture_error=capture_error,
                )
                evidence = {
                    "status": status,
                    "last_frame": last_frame,
                    "capture_failures": capture_failures,
                    "capture_error": capture_error,
                    "halt_dialog_suspected": halt,
                    **self._tails(),
                }
                code = status.get("returncode")
                if halt:
                    reason = "guest_cpu_halt"
                    message = (
                        "guest CPU halted (double bus/address error); "
                        "Hatari's halt dialog was closed"
                    )
                else:
                    reason = str(status.get("exit_reason") or "emulator_exited")
                    message = f"emulator left running state: {state} (exit {code})"
                return self._failure(
                    game_id,
                    title,
                    started,
                    reason,
                    message,
                    evidence,
                    last_frame,
                    last_stats,
                )
            if state == "unresponsive":
                return self._failure(
                    game_id,
                    title,
                    started,
                    "emulator_unresponsive",
                    "emulator stopped answering control-socket heartbeats",
                    {"status": status, "last_frame": last_frame, **self._tails()},
                    last_frame,
                    last_stats,
                )
            if state == "at_prompt":
                evidence = {"status": status, "frame_path": last_frame, **self._tails()}
                return self._failure(
                    game_id,
                    title,
                    started,
                    "debugger_prompt",
                    "emulator entered the debugger prompt during boot",
                    evidence,
                    last_frame,
                    last_stats,
                )

            capture, frame, stats, capture_error = self._capture(game_id)
            if capture_error:
                capture_failures += 1
            else:
                capture_failures = 0
            if capture_failures >= 3 and max_nonblack < NONBLACK_THRESHOLD:
                evidence = {
                    "status": status,
                    "last_frame": last_frame,
                    "capture_error": capture_error,
                    **self._tails(),
                }
                return self._failure(
                    game_id,
                    title,
                    started,
                    "capture_failed",
                    f"screen capture failed repeatedly: {capture_error}",
                    evidence,
                    last_frame,
                    last_stats,
                )
            if frame:
                last_frame = frame
            if stats:
                last_stats = stats
                now = time.monotonic()
                ratio = float(
                    stats.get("content_nonblack_ratio", stats.get("nonblack_ratio", 0.0))
                )
                unique_colors = int(stats.get("unique_colors", 0))
                max_nonblack = max(max_nonblack, ratio)
                digest = str((capture or {}).get("sha256") or "")
                pc = self._pc()
                last_pc = pc
                pc_in_ram = pc is not None and not (TOS_ROM_START <= pc <= TOS_ROM_END)
                if pc_in_ram:
                    seen_ram_pc = True
                changed_from_boot = False

                if ratio >= NONBLACK_THRESHOLD and unique_colors >= 2:
                    if visible_since is None:
                        visible_since = now
                    if boot_digest is None and not pc_in_ram and digest:
                        boot_digest = digest
                    changed_from_boot = bool(digest) and (
                        boot_digest is None or digest != boot_digest
                    )
                    if changed_from_boot and (pc_in_ram or pc is None):
                        if post_boot_since is None:
                            post_boot_since = now
                    else:
                        post_boot_since = None
                    if pc_in_ram and changed_from_boot:
                        if ram_since is None:
                            ram_since = now
                    else:
                        ram_since = None
                    if changed_from_boot and digest == previous_digest:
                        if stable_since is None:
                            stable_since = now
                    else:
                        stable_since = None

                    visible_for = now - visible_since
                    post_boot_for = now - post_boot_since if post_boot_since else 0.0
                    ram_for = now - ram_since if ram_since else 0.0
                    stable_for = now - stable_since if stable_since else 0.0
                    minimum = max(1.0, self.min_visible_seconds)
                    if turbo_active and (
                        stable_for >= 1.0 or now - started >= 10.0
                    ):
                        if self._set_boot_turbo(False):
                            self._turbo_early_restored = True
                            turbo_active = False
                    if not require_game:
                        ready = visible_for >= minimum
                    elif pc_in_ram:
                        ready = (
                            visible_for >= minimum
                            and post_boot_for >= minimum
                            and ram_for >= minimum
                        )
                    elif pc is None:
                        ready = (
                            visible_for >= minimum
                            and post_boot_for >= max(3.0, minimum * 2)
                            and stable_for >= minimum
                        )
                    elif seen_ram_pc and changed_from_boot:
                        # Cracked loaders and GEM-desktop boot disks often hand
                        # control back to the TOS desktop while they wait for a
                        # key or a double-click.  If game code has already run
                        # and a post-boot screen is stable, treat that as a
                        # successful start instead of timing out.
                        ready = stable_for >= max(2.0, minimum * 2)
                    else:
                        ready = False

                    if ready:
                        resolution = (int(stats["width"]), int(stats["height"]))
                        pc_text = f" (pc 0x{pc:x})" if pc is not None else ""
                        loader = bool(
                            require_game
                            and pc is not None
                            and not pc_in_ram
                            and seen_ram_pc
                        )
                        if not require_game:
                            mode = "desktop"
                        elif loader:
                            mode = "loader"
                        else:
                            mode = "game"
                        if loader:
                            message = (
                                f"loader frame after {int((now - started) * 1000)} ms"
                                f"{pc_text}"
                            )
                        elif require_game:
                            message = (
                                f"game frame after {int((now - started) * 1000)} ms"
                                f"{pc_text}"
                            )
                        else:
                            message = (
                                f"desktop frame after {int((now - started) * 1000)} ms"
                                f"{pc_text}"
                            )
                        result = GameResult(
                            ok=True,
                            game_id=game_id,
                            title=title,
                            machine=self.current_machine or self.default_machine,
                            tos=self.current_tos,
                            frame_path=frame,
                            resolution=resolution,
                            status="success",
                            message=message,
                            evidence={
                                "mode": mode,
                                "require_game": require_game,
                                "samples": samples[-8:],
                                "status": status,
                                "max_nonblack_ratio": max_nonblack,
                                "frame_changed": changed_from_boot,
                                "boot_digest": boot_digest[:16] if boot_digest else None,
                                "pc": f"0x{pc:x}" if pc is not None else None,
                                "pc_in_ram": pc_in_ram,
                                "seen_ram_pc": seen_ram_pc,
                                "post_boot_ms": int(post_boot_for * 1000),
                                "ram_ms": int(ram_for * 1000),
                                "stable_ms": int(stable_for * 1000),
                            },
                            duration_ms=int((now - started) * 1000),
                        )
                        return result
                else:
                    visible_since = None
                    post_boot_since = None
                    ram_since = None
                    stable_since = None
                if digest:
                    previous_digest = digest
                samples.append(
                    {
                        "t_ms": int((now - started) * 1000),
                        "state": state,
                        "nonblack_ratio": float(stats.get("nonblack_ratio", 0.0)),
                        "content_nonblack_ratio": ratio,
                        "unique_colors": unique_colors,
                        "digest": digest[:16],
                        "pc": f"0x{pc:x}" if pc is not None else None,
                        "pc_in_ram": pc_in_ram,
                        "changed_from_boot": changed_from_boot,
                        "capture_error": capture_error,
                    }
                )
            time.sleep(self.poll_interval)

        evidence = {
            "status": last_status,
            "last_frame": last_frame,
            "last_stats": last_stats,
            "samples": samples[-12:],
            "max_nonblack_ratio": max_nonblack,
            "boot_digest": boot_digest[:16] if boot_digest else None,
            "last_pc": f"0x{last_pc:x}" if last_pc is not None else None,
            "seen_ram_pc": seen_ram_pc,
            "capture_error": capture_error,
            **self._tails(),
        }
        if max_nonblack < NONBLACK_THRESHOLD:
            reason = "timeout_blank_screen"
            message = "boot timed out with a black/blank screen"
        else:
            reason = "timeout_no_stable_frame"
            visible = [
                sample
                for sample in samples
                if sample.get("content_nonblack_ratio", sample["nonblack_ratio"])
                >= NONBLACK_THRESHOLD
                and sample.get("unique_colors", 0) >= 2
            ]
            if boot_digest and visible and all(
                sample["digest"] == boot_digest[:16] for sample in visible
            ):
                classification = "boot_screen_only"
                message = "boot timed out showing only the boot screen"
            elif not seen_ram_pc:
                classification = "no_game_pc"
                message = "boot timed out without observing game code running from RAM"
            else:
                classification = "no_stable_game_frame"
                message = "boot timed out before a stable game frame"
            evidence["classification"] = classification
        return self._failure(
            game_id, title, started, reason, message, evidence, last_frame, last_stats
        )

    def _failure(
        self,
        game_id: str,
        title: str,
        started: float,
        reason: str,
        message: str,
        evidence: dict[str, Any],
        frame: str | None = None,
        stats: dict[str, Any] | None = None,
    ) -> GameResult:
        resolution = (int(stats["width"]), int(stats["height"])) if stats else None
        return GameResult(
            ok=False,
            game_id=game_id,
            title=title,
            machine=self.current_machine or self.default_machine,
            tos=self.current_tos,
            frame_path=frame,
            resolution=resolution,
            status="failure",
            message=message,
            failure_reason=reason,
            evidence=evidence,
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    def swap_game(
        self,
        game: Any,
        *,
        machine: str | None = None,
        tos: str | Path | None = None,
        args: list[str] | None = None,
        timeout: float | None = None,
    ) -> GameResult:
        """Switch disks in place when the machine and TOS do not change.

        Otherwise this falls back to a clean stop/start. Any failure in the
        hot-swap path also falls back, so a call never leaves a half-swapped
        machine behind.
        """
        started = time.monotonic()
        game_id = str(getattr(game, "id", "") or stable_id(game))
        title = str(getattr(game, "title", "") or Path(str(game)).name)
        learned = self.config.learned_launch(game_id)
        target_machine = (
            machine
            or learned.get("machine")
            or self.current_machine
            or self.default_machine
        ).lower()
        target_tos: Path | None
        if tos is not None:
            target_tos = Path(tos).expanduser()
        elif learned.get("tos") and Path(learned["tos"]).expanduser().is_file():
            target_tos = Path(learned["tos"]).expanduser()
        elif self.current_tos:
            target_tos = Path(self.current_tos)
        else:
            target_tos = paths.tos_for_machine(target_machine)

        same_machine = self.current_machine == target_machine
        same_tos = (
            target_tos is None
            and self.current_tos is None
            or target_tos is not None
            and self.current_tos is not None
            and str(target_tos.resolve()) == str(Path(self.current_tos).resolve())
        )
        if not self.current_game_id or not same_machine or not same_tos:
            return self.start_game(
                game, machine=target_machine, tos=target_tos, args=args, timeout=timeout
            )

        try:
            disk_path = paths.materialize_disk(
                getattr(game, "path", None) or game,
                self.state_dir / "run" / game_id / "disk",
                mode=self.disk_mode,
            )
            if " " in str(disk_path):
                raise ValueError("disk path contains a space")
            status = self._status()
            if str(status.get("state")) not in ALIVE_STATES:
                raise RuntimeError(f"emulator is {status.get('state')}")
            if status.get("at_prompt"):
                self.client.call_tool("emu_reset", kind="hard", timeout=15.0)
                time.sleep(0.1)
            self.client.call_tool(
                "emu_set_option", options=f"--disk-a {disk_path}", timeout=15.0
            )
            turbo = bool(self.turbo) and not (
                _has_option(list(args or []), "--cpuclock")
                or _has_option(list(args or []), "--fast-forward")
            )
            if turbo:
                self._set_boot_turbo(True)
            self.client.call_tool("emu_reset", kind="hard", timeout=20.0)
        except BaseException:
            self._stop_hard()
            return self.start_game(
                game, machine=target_machine, tos=target_tos, args=args, timeout=timeout
            )

        self.current_game_id = game_id
        self.current_title = title
        self.current_machine = target_machine
        self.current_tos = str(target_tos) if target_tos else None
        self.current_disk = disk_path
        self._frame_index = 0
        self._turbo_early_restored = False
        try:
            result = self._wait_for_game(
                game_id=game_id,
                title=title,
                started=started,
                timeout=timeout or self.boot_timeout,
                turbo=turbo,
            )
        finally:
            restored = self._set_boot_turbo(False) if turbo else True
        result.evidence.setdefault(
            "boot_turbo",
            {
                "enabled": turbo,
                "restored": restored,
                "early_restore": self._turbo_early_restored,
            },
        )
        result.machine = target_machine
        result.tos = str(target_tos) if target_tos else None
        self.last_result = result
        self._record(result, list(args or []), target_machine)
        return result

    def switch_disk(
        self,
        game: Any,
        *,
        drive: str = "a",
        timeout: float = 15.0,
    ) -> GameResult:
        """Insert another disk of the running game without resetting it."""
        started = time.monotonic()
        game_id = str(getattr(game, "id", "") or stable_id(game))
        title = str(getattr(game, "title", "") or Path(str(game)).name)
        drive = drive.strip().lower()
        if drive not in {"a", "b"}:
            return self._failure(
                game_id,
                title,
                started,
                "invalid_drive",
                f"unsupported floppy drive: {drive}",
                {},
            )
        if not self.current_game_id:
            return self._failure(
                game_id,
                title,
                started,
                "no_game",
                "no tracked game is running",
                {},
            )
        try:
            disk_path = paths.materialize_disk(
                getattr(game, "path", None) or game,
                self.state_dir / "run" / self.current_game_id / "disks" / game_id,
                mode=self.disk_mode,
            )
            if " " in str(disk_path):
                raise ValueError("disk path contains a space")
            status = self._status()
            state = str(status.get("state"))
            if state not in ALIVE_STATES:
                raise RuntimeError(f"emulator is {state}")
            if status.get("at_prompt"):
                raise RuntimeError("emulator is at the debugger prompt")
            self.client.call_tool(
                "emu_set_option",
                options=f"--disk-{drive} {disk_path}",
                timeout=timeout,
            )
        except BaseException as exc:
            return self._failure(
                game_id,
                title,
                started,
                "disk_switch_failed",
                f"could not insert disk: {exc}",
                {"error": str(exc)},
            )

        disk_turbo = bool(self.turbo)
        disk_turbo_restored = True
        if disk_turbo:
            self._set_boot_turbo(True)
            if self.disk_turbo_seconds:
                time.sleep(self.disk_turbo_seconds)
            disk_turbo_restored = self._set_boot_turbo(False)

        self.current_disk = disk_path
        name = str(getattr(game, "display_name", "") or getattr(game, "filename", "") or game_id)
        return GameResult(
            ok=True,
            game_id=self.current_game_id,
            title=title,
            machine=self.current_machine or self.default_machine,
            tos=self.current_tos,
            status="running",
            message=f"inserted {name} in drive {drive.upper()}",
            evidence={
                "game_id": self.current_game_id,
                "disk_id": game_id,
                "drive": drive,
                "disk_path": str(disk_path),
                "reset": False,
                "disk_turbo": {
                    "enabled": disk_turbo,
                    "seconds": self.disk_turbo_seconds if disk_turbo else 0.0,
                    "restored": disk_turbo_restored,
                },
            },
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    def stop(self, *, force: bool = False, timeout: float = 20.0) -> dict[str, Any]:
        """Stop the guest cleanly; escalation is handled by ``emu_stop``."""
        try:
            result = self.client.call_tool("emu_stop", force=force, timeout=timeout)
        except BaseException:
            try:
                result = self.client.call_tool("emu_stop", force=True, timeout=timeout)
            except BaseException as exc:
                result = {"ok": False, "error": str(exc)}
        self.current_game_id = None
        self.current_title = None
        self.current_machine = None
        self.current_tos = None
        self.current_disk = None
        return result

    def __enter__(self) -> "GameSession":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.stop()
