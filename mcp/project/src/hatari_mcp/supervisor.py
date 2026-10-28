"""One Hatari child process, its control socket, and its debugger channels."""

from __future__ import annotations

import asyncio
import contextlib
import ctypes
import os
import re
import secrets
import signal
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import Settings
from .errors import HatariError, error
from .ring import RingBuffer
from .socketdir import RuntimeDir, create_runtime_dir
from .state import ALIVE, HEARTBEAT_ACTIVE, State, WRITABLE_SOCKET, WRITABLE_STDIN

BREAKPOINT_RE = re.compile(
    r"(?:CPU|DSP) breakpoint condition\(s\) matched|Breakpoint condition\(s\) matched",
    re.IGNORECASE,
)
EXCEPTION_RE = re.compile(
    r"\b(Bus error|Address error|Illegal instruction|Zero divide|CHK|TRAPV|"
    r"Privilege violation|Trace|Line A|Line F|MMU)\s+(?:exception|reading)\b",
    re.IGNORECASE,
)
NATFEATS_EXIT_RE = re.compile(r"NatFeats:\s*exit\((-?\d+)\)", re.IGNORECASE)
DEBUGGER_ENTER_RE = re.compile(r"You have entered debug mode", re.IGNORECASE)
PAGER_RE = re.compile(r"---\s*q to exit .*enter to continue\s*---")
TERMINAL = {"cont", "continue", "c", "quit", "q", "exit", "reset"}
PROMPT_RE = re.compile(r"^\s*>\s")

HEARTBEAT_INTERVAL = 0.5
HEARTBEAT_PROBE_INTERVAL = 2.0
HEARTBEAT_TIMEOUT = 1.0
HEARTBEAT_MISSES = 3
# A banner is advisory and may arrive just after the control socket connects.
# Keep a short grace window so a delayed banner is not dropped silently.
BANNER_WINDOW_SECONDS = 1.5


@dataclass
class RestartPolicy:
    """Bounded restart policy; off by default so normal runs are unchanged."""

    enabled: bool = False
    max_restarts: int = 3
    window_seconds: float = 600.0
    backoff_seconds: tuple[float, ...] = (1.0, 2.0, 4.0)
    terminate_unresponsive: bool = False

    def delay(self, attempt: int) -> float:
        if not self.backoff_seconds:
            return 0.0
        return float(self.backoff_seconds[min(attempt, len(self.backoff_seconds) - 1)])


def _set_parent_death_signal() -> None:
    """Ask Linux to SIGKILL the child when the server dies.

    A remotely paused child is SIGSTOPped and cannot run a SIGTERM handler (or
    even terminate) until it is continued, which would leave a stopped orphan
    if the server itself is SIGKILLed. SIGKILL is delivered to stopped
    processes immediately, so it is the only safe parent-death signal here.

    Called in the child between fork and exec. This is best-effort and is a
    no-op on platforms without ``prctl``; the heartbeat detects a reparented
    process as a portable fallback.
    """
    if not sys.platform.startswith("linux"):
        return
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        PR_SET_PDEATHSIG = 1
        libc.prctl(PR_SET_PDEATHSIG, int(signal.SIGKILL), 0, 0, 0)
    except Exception:
        pass


def atomic_write_bytes(path: Path, data: bytes, *, mode: int = 0o600) -> None:
    """Write ``data`` to ``path`` via a same-directory temporary file + replace."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
        with contextlib.suppress(OSError):
            dir_fd = os.open(path.parent, os.O_DIRECTORY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
    finally:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()


@dataclass
class CommandResult:
    text: str
    channel: str
    state: str
    timed_out: bool = False
    lines: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": True,
            "raw": self.text,
            "lines": self.lines,
            "channel": self.channel,
            "state": self.state,
            "timed_out": self.timed_out,
        }


@dataclass
class _Waiter:
    token: str
    future: asyncio.Future[str]
    lines: list[str] = field(default_factory=list)


class Supervisor:
    def __init__(
        self,
        settings: Settings,
        *,
        heartbeat_interval: float = HEARTBEAT_INTERVAL,
        heartbeat_probe_interval: float = HEARTBEAT_PROBE_INTERVAL,
        heartbeat_timeout: float = HEARTBEAT_TIMEOUT,
        heartbeat_misses: int = HEARTBEAT_MISSES,
        restart_policy: RestartPolicy | None = None,
    ):
        self.settings = settings
        self.state = State.STOPPED
        self.proc: asyncio.subprocess.Process | None = None
        self.runtime: RuntimeDir | None = None
        self.started_at: float | None = None
        self.stopped_at: float | None = None
        self.returncode: int | None = None
        self.program_exit_code: int | None = None
        self.exit_reason: str | None = None
        self.last_error: str | None = None
        self.exception: dict[str, Any] | None = None
        self.stdout_ring = RingBuffer(settings.ring_bytes)
        self.stderr_ring = RingBuffer(settings.ring_bytes)
        self._command_lock = asyncio.Lock()
        self._lifecycle_lock = asyncio.Lock()
        self._socket_server: asyncio.AbstractServer | None = None
        self._socket_reader: asyncio.StreamReader | None = None
        self._socket_writer: asyncio.StreamWriter | None = None
        self._socket_connected: asyncio.Future[bool] | None = None
        self._exit_event: asyncio.Event | None = None
        self._stdout_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self._wait_task: asyncio.Task | None = None
        self._socket_task: asyncio.Task | None = None
        self._waiter: _Waiter | None = None
        self._stderr_partial = ""
        self._stdout_partial = ""
        self._pager_blocked = False
        self._stopping = False
        self._stdin: asyncio.StreamWriter | None = None

        # P7 heartbeat / recovery state.
        self.heartbeat_interval = max(0.05, float(heartbeat_interval))
        self.heartbeat_probe_interval = max(0.05, float(heartbeat_probe_interval))
        self.heartbeat_timeout = max(0.05, float(heartbeat_timeout))
        self.heartbeat_misses_threshold = max(1, int(heartbeat_misses))
        self.restart_policy = restart_policy or RestartPolicy()
        self.protocol_banner: Any = None
        self.protocol_banner_raw: str | None = None
        self.protocol_banner_error: str | None = None
        self._banner_nonce: str | None = None
        self._banner_seen = False
        self._banner_window_open = False
        self._banner_deadline: float | None = None
        self._natfeats_enabled = False
        self.heartbeat_misses = 0
        self.last_pong: float | None = None
        self.unresponsive_since: float | None = None
        self.exit_cause: str | None = None
        self.crash_loop = False
        self.restart_count = 0
        self._restart_times: list[float] = []
        self._heartbeat_task: asyncio.Task | None = None
        self._restart_task: asyncio.Task | None = None
        self._framed_ping: Any = None
        self._last_probe = 0.0
        self._requested_stop = False
        self._launch_kwargs: dict[str, Any] | None = None
        # Watch the server's own parent; PR_SET_PDEATHSIG covers the child.
        self._parent_pid = os.getppid()

    # -- lifecycle -----------------------------------------------------

    async def start(
        self,
        *,
        args: list[str] | None = None,
        tos: Path | None = None,
        env: dict[str, str] | None = None,
        display: bool = False,
        natfeats: bool = False,
        startup_timeout: float | None = None,
    ) -> dict[str, Any]:
        # Validate the protocol selector before any spawn so a bad
        # HATARI_MCP_PROTOCOL cannot leave a live child or STARTING state.
        self._validate_protocol_env()
        async with self._lifecycle_lock:
            if self.state in ALIVE:
                return await self._start_locked(
                    args=args,
                    tos=tos,
                    env=env,
                    display=display,
                    natfeats=natfeats,
                    startup_timeout=startup_timeout,
                )
            self._requested_stop = False
            if self._restart_task and not self._restart_task.done():
                self._restart_task.cancel()
            self._restart_times.clear()
            self.restart_count = 0
            self.crash_loop = False
            self._launch_kwargs = {
                "args": list(args) if args is not None else None,
                "tos": tos,
                "env": dict(env) if env is not None else None,
                "display": display,
                "natfeats": natfeats,
                "startup_timeout": startup_timeout,
            }
            return await self._start_locked(
                args=args,
                tos=tos,
                env=env,
                display=display,
                natfeats=natfeats,
                startup_timeout=startup_timeout,
            )

    async def _start_locked(
        self,
        *,
        args: list[str] | None = None,
        tos: Path | None = None,
        env: dict[str, str] | None = None,
        display: bool = False,
        natfeats: bool = False,
        startup_timeout: float | None = None,
    ) -> dict[str, Any]:
        self._validate_protocol_env()
        if self.state in ALIVE:
            status = self.status()
            status["already_running"] = True
            return status
        if self.runtime is not None:
            await self._cleanup_runtime()
        hatari = self.settings.require_hatari()
        tos_path = tos or self.settings.default_tos
        if not tos_path:
            raise error(
                "missing_tos",
                "No TOS/EmuTOS image was configured.",
                "Set HATARI_TOS or pass tos=... to emu_start.",
            )
        tos_path = Path(tos_path).expanduser().resolve()
        if not tos_path.is_file():
            raise error(
                "missing_tos",
                f"TOS image does not exist: {tos_path}",
                "Set HATARI_TOS to a readable EmuTOS/TOS image.",
            )

        self._reset_state()
        self._natfeats_enabled = bool(natfeats)
        self._banner_nonce = secrets.token_hex(16)
        self._banner_window_open = True
        # The pre-connect part of the window is bounded by startup_timeout;
        # the post-connect grace starts once the control socket is ready.
        self._banner_deadline = None
        runtime = create_runtime_dir(self.settings.runtime_root)
        self.runtime = runtime
        runtime.socket_path.unlink(missing_ok=True)
        self._socket_connected = asyncio.get_running_loop().create_future()
        self._exit_event = asyncio.Event()

        try:
            self._socket_server = await asyncio.start_unix_server(
                self._on_socket_client, path=str(runtime.socket_path)
            )
        except OSError as exc:
            runtime.cleanup()
            self.runtime = None
            raise error("socket_error", f"Could not create control socket: {exc}", "Check /tmp permissions.") from exc

        cmd = [
            str(hatari),
            "--control-socket",
            str(runtime.socket_path),
            "--configfile",
            str(runtime.config_path),
            "--tos",
            str(tos_path),
            "--sound",
            "off",
            "--confirm-quit",
            "off",
            "--conout",
            "2",
            "--log-level",
            "info",
        ]
        if natfeats:
            cmd += ["--natfeats", "on"]
        cmd += list(args or [])

        child_env = os.environ.copy()
        child_env["SDL_AUDIODRIVER"] = "dummy"
        if display:
            child_env.pop("SDL_VIDEODRIVER", None)
        else:
            child_env["SDL_VIDEODRIVER"] = "dummy"
        child_env["HATARI_MCP"] = "1"
        if self._banner_nonce:
            child_env["HATARI_MCP_PROTOCOL_NONCE"] = self._banner_nonce
        child_env.update(env or {})

        self.state = State.STARTING
        linux = sys.platform.startswith("linux")
        try:
            self.proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=child_env,
                start_new_session=True,
                # preexec_fn is Linux-only here. macOS/BSD/Windows have no
                # parent-death signal; the heartbeat parent-pid watchdog is the
                # documented fallback. Avoid passing a no-op preexec_fn because
                # it is unsafe in threaded processes.
                preexec_fn=_set_parent_death_signal if linux else None,
            )
        except asyncio.CancelledError:
            self.state = State.FAILED
            self.last_error = "startup cancelled"
            await self._cleanup_runtime()
            raise
        except BaseException as exc:
            self.state = State.FAILED
            self.last_error = str(exc)
            await self._cleanup_runtime()
            if isinstance(exc, OSError):
                raise error(
                    "startup_failed",
                    f"Could not launch Hatari: {exc}",
                    "Check HATARI_BIN and the launch arguments.",
                ) from exc
            raise

        assert self.proc.stdin and self.proc.stdout and self.proc.stderr
        self._stdin = self.proc.stdin
        self._stdout_task = asyncio.create_task(self._read_stdout())
        self._stderr_task = asyncio.create_task(self._read_stderr())
        self._wait_task = asyncio.create_task(self._wait_child())
        self._start_heartbeat_task()

        timeout = self.settings.startup_timeout if startup_timeout is None else startup_timeout
        try:
            assert self._socket_connected is not None
            connected = await asyncio.wait_for(asyncio.shield(self._socket_connected), timeout)
        except asyncio.TimeoutError:
            self.last_error = "startup socket timeout"
            await self._stop_unlocked(force=True, reason="startup_timeout")
            raise error(
                "startup_timeout",
                f"Hatari did not connect to the control socket within {timeout:.0f}s.",
                "Check the TOS path and Hatari stderr with log_read.",
            ) from None
        except asyncio.CancelledError:
            await self._stop_unlocked(force=True, reason="cancelled")
            raise
        if not connected or self.state in (State.EXITED, State.CRASHED, State.FAILED):
            if self.state is State.STARTING:
                self._set_state(State.FAILED)
                self.last_error = self.last_error or "Hatari exited before connecting"
            await self._cleanup_runtime()
            raise error(
                "startup_failed",
                "Hatari exited before the control socket connected.",
                "Read log_read to see the emulator error.",
            )
        if self._banner_deadline is None:
            self._banner_deadline = time.monotonic() + BANNER_WINDOW_SECONDS
        await self._enforce_protocol_mode()
        # Let the waiter run once so an exit that raced with the socket
        # connection is visible before advertising RUNNING.
        await asyncio.sleep(0)
        if (
            self.proc is None
            or self.proc.returncode is not None
            or (self._exit_event is not None and self._exit_event.is_set())
            or self.state in (State.EXITED, State.CRASHED, State.FAILED)
        ):
            if self.state not in (State.EXITED, State.CRASHED, State.FAILED):
                self._set_state(State.FAILED)
                self.last_error = self.last_error or "Hatari exited after the control socket connected"
            await self._cleanup_runtime()
            raise error(
                "startup_failed",
                "Hatari exited immediately after the control socket connected.",
                "Read log_read to see the emulator error.",
            )
        self._set_state(State.RUNNING)
        self.started_at = time.time()
        return self.status()

    def _validate_protocol_env(self) -> None:
        try:
            from .protocol.selector import mode_from_env
        except Exception:
            return
        # Raises a typed bad_option for an invalid HATARI_MCP_PROTOCOL value.
        mode_from_env()

    def _set_state(self, state: State) -> None:
        if state is self.state:
            return
        # A late stderr line or an already completed waiter must never revive
        # a terminal process. Terminal states may still be normalized to
        # EXITED by stop(); a fresh launch goes through _reset_state().
        if self.state in (State.EXITED, State.CRASHED, State.FAILED) and state not in (
            State.EXITED,
            State.CRASHED,
            State.FAILED,
        ):
            return
        self.state = state
        self.heartbeat_misses = 0
        self.last_pong = None
        if state is not State.UNRESPONSIVE:
            self.unresponsive_since = None
        if state is State.RUNNING and self.last_error and "heartbeat" in self.last_error.lower():
            self.last_error = None

    def _signal_child(self, sig: int) -> bool:
        if self.proc is None or self.proc.returncode is not None:
            return False
        if not sys.platform.startswith("linux") and os.name != "posix":
            return False
        try:
            os.kill(self.proc.pid, sig)
            return True
        except (ProcessLookupError, PermissionError, OSError):
            return False

    async def _wait_for_banner(self, timeout: float) -> None:
        deadline = time.monotonic() + max(0.0, timeout)
        # A rejected first banner does not end the wait: with the nonce check a
        # later valid banner is still trustworthy.
        while self.protocol_banner is None and time.monotonic() < deadline:
            if self.proc is not None and self.proc.returncode is not None:
                return
            await asyncio.sleep(0.02)

    async def _enforce_protocol_mode(self) -> None:
        try:
            from .protocol.selector import ProtocolMode, mode_from_env, select_protocol
        except Exception:
            return
        mode = mode_from_env()
        if mode in {ProtocolMode.AUTO, ProtocolMode.LEGACY}:
            return
        await self._wait_for_banner(min(1.5, self.settings.startup_timeout))
        try:
            # No HELLO/HELLO_ACK is wired yet, so strict framed mode refuses
            # even when a (nonce-validated) banner was seen.
            select_protocol(mode, banner=self.protocol_banner)
        except HatariError as exc:
            if self.protocol_banner_error:
                exc.detail = f"{exc.detail or ''}; banner: {self.protocol_banner_error}".strip("; ")
            self.last_error = str(exc)
            await self._stop_unlocked(force=True, reason="protocol_unavailable")
            raise

    async def stop(self, *, force: bool = False) -> dict[str, Any]:
        # Set before waiting for the lifecycle lock: a crash racing with a
        # requested stop must never schedule a restart.
        self._requested_stop = True
        self._stopping = True
        if self._restart_task and not self._restart_task.done():
            self._restart_task.cancel()
        async with self._lifecycle_lock:
            return await self._stop_unlocked(force=force, reason="requested")

    async def _stop_unlocked(self, *, force: bool, reason: str) -> dict[str, Any]:
        if not self.proc:
            # A proc-less STARTING/RUNNING state can happen after a failed
            # spawn path; stop must still leave a terminal state, not hang.
            if self.state is State.CRASHED and self.exit_cause == "requested_stop":
                self._set_state(State.EXITED)
                self.last_error = None
            elif self.state not in (State.STOPPED, State.EXITED, State.CRASHED, State.FAILED):
                self._set_state(State.STOPPED)
                self.stopped_at = time.time()
            await self._cleanup_runtime()
            return self.status()
        if self.state in (State.EXITED, State.CRASHED, State.FAILED):
            # A requested stop racing with the waiter may have stored CRASHED
            # before it observed requested_stop; normalize that race.
            if self.state is State.CRASHED and self.exit_cause == "requested_stop":
                self._set_state(State.EXITED)
                self.last_error = None
            await self._cleanup_runtime()
            return self.status()

        was_at_prompt = self.state == State.AT_PROMPT
        was_paused = self.state in (State.REMOTE_PAUSED, State.UNRESPONSIVE)
        if was_paused:
            # A SIGSTOP'd child cannot process a graceful quit or SIGTERM.
            self._signal_child(signal.SIGCONT)
        self.state = State.STOPPING
        self._stopping = True
        try:
            if was_at_prompt and self._stdin:
                with contextlib.suppress(Exception):
                    self._stdin.write(b"quit\n")
                    await self._stdin.drain()
            if self._socket_writer:
                with contextlib.suppress(Exception):
                    self._socket_writer.write(b"hatari-debug quit\n")
                    await self._socket_writer.drain()
        except Exception:
            pass

        try:
            if self.proc.returncode is None:
                await asyncio.wait_for(self.proc.wait(), self.settings.stop_timeout)
        except (asyncio.TimeoutError, ProcessLookupError):
            self._signal_child(signal.SIGCONT)
            with contextlib.suppress(ProcessLookupError):
                if force:
                    self.proc.kill()
                else:
                    self.proc.terminate()
            try:
                await asyncio.wait_for(self.proc.wait(), self.settings.stop_timeout)
            except (asyncio.TimeoutError, ProcessLookupError):
                with contextlib.suppress(ProcessLookupError):
                    self.proc.kill()
                with contextlib.suppress(Exception):
                    await self.proc.wait()

        self.returncode = self.proc.returncode
        requested_stop = reason == "requested" or self._requested_stop or self.exit_cause == "requested_stop"
        if requested_stop or self.state not in (State.CRASHED, State.FAILED):
            self._set_state(State.EXITED)
        self.stopped_at = time.time()
        self.last_error = None if requested_stop or reason == "startup_timeout" else self.last_error
        await self._cleanup_runtime()
        return self.status()

    async def _cleanup_runtime(self) -> None:
        if self._socket_writer:
            with contextlib.suppress(Exception):
                self._socket_writer.close()
            with contextlib.suppress(Exception):
                await self._socket_writer.wait_closed()
        self._socket_writer = None
        if self._socket_server:
            self._socket_server.close()
            with contextlib.suppress(Exception):
                await self._socket_server.wait_closed()
        self._socket_server = None
        for task in (self._socket_task, self._stdout_task, self._stderr_task, self._heartbeat_task):
            if task and task is not asyncio.current_task() and not task.done():
                task.cancel()
        self._heartbeat_task = None
        if self.runtime:
            self.runtime.cleanup()
            self.runtime = None
        self._stderr_partial = ""
        self._stdout_partial = ""
        self._waiter = None
        self._stdin = None
        # A banner is valid only for the launch that produced it.
        self.protocol_banner = None
        self.protocol_banner_raw = None
        self.protocol_banner_error = None
        self._banner_nonce = None
        self._banner_seen = False
        self._banner_window_open = False
        self._banner_deadline = None

    def _reset_state(self) -> None:
        self.state = State.STOPPED
        self.returncode = None
        self.program_exit_code = None
        self.exit_reason = None
        self.last_error = None
        self.exception = None
        self.started_at = None
        self.stopped_at = None
        self.stdout_ring.clear()
        self.stderr_ring.clear()
        self._pager_blocked = False
        self._stopping = False
        self._socket_reader = None
        self._socket_writer = None
        self._socket_server = None
        self._stdin = None
        self.protocol_banner = None
        self.protocol_banner_raw = None
        self.protocol_banner_error = None
        self._banner_nonce = None
        self._banner_seen = False
        self._banner_window_open = False
        self._banner_deadline = None
        self._natfeats_enabled = False
        self.heartbeat_misses = 0
        self.last_pong = None
        self.unresponsive_since = None
        self.exit_cause = None
        self._last_probe = 0.0
        self._stopping = False

    async def shutdown(self) -> None:
        self._requested_stop = True
        if self._restart_task and not self._restart_task.done():
            self._restart_task.cancel()
        with contextlib.suppress(Exception):
            await self.stop(force=True)
        if self._wait_task and not self._wait_task.done():
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._wait_task, 2)
        await self._cleanup_runtime()

    # -- child and pipes -----------------------------------------------

    async def _wait_child(self) -> None:
        assert self.proc
        was_starting = self.state is State.STARTING or self.started_at is None
        try:
            rc = await self.proc.wait()
        except asyncio.CancelledError:
            raise
        # Resolve the startup wait immediately on early exit instead of waiting
        # for the full startup timeout. Do the same for the socket-or-exit race.
        if self._socket_connected is not None and not self._socket_connected.done():
            self._socket_connected.set_result(False)
        # stderr EOF may arrive a tick after wait(); give it a bounded chance so
        # a final NatFeats exit line is seen before classifying/restarting.
        stderr_task = self._stderr_task
        if stderr_task is not None and stderr_task is not asyncio.current_task() and not stderr_task.done():
            await asyncio.wait({stderr_task}, timeout=0.5)
        self.returncode = rc
        if self.program_exit_code is not None:
            self.exit_cause = "guest_exit"
        else:
            self.exit_cause = self._classify_exit(rc)
        if self.exit_cause == "requested_stop":
            # A stop the caller asked for is clean even if a late stderr line
            # raced the state machine or the child used a signal to quit.
            self.state = State.EXITED
            self.last_error = None
        elif self.state in (State.STOPPING, State.EXITED):
            self.state = State.EXITED
        elif self.exit_cause == "guest_exit":
            self.state = State.EXITED
        elif rc != 0 or self.exit_cause not in {"process_exit"}:
            self.state = State.CRASHED
            self.last_error = f"Hatari exited with code {rc} ({self.exit_cause})"
        else:
            self.state = State.EXITED
        if self.exit_reason is None:
            self.exit_reason = "process_exit"
        if self._exit_event:
            self._exit_event.set()
        waiter = self._waiter
        if waiter and not waiter.future.done():
            waiter.future.set_exception(
                error("emulator_gone", "Hatari exited while the command was running.", "Call emu_status.")
            )
        self._close_socket()
        if self._heartbeat_task is not None and self._heartbeat_task is not asyncio.current_task():
            self._heartbeat_task.cancel()
        self.protocol_banner = None
        self.protocol_banner_raw = None
        self._banner_seen = False
        self._banner_window_open = False
        self._banner_deadline = None
        # Never restart while the launch is still STARTING; startup failures
        # are reported to the caller, not hidden behind an automatic restart.
        if not was_starting and self.program_exit_code is None and self.state is State.CRASHED:
            self._schedule_restart()
        asyncio.create_task(self._deferred_cleanup())

    async def _deferred_cleanup(self) -> None:
        """Release the runtime socket after a terminal exit without dropping logs."""
        try:
            await asyncio.sleep(0.2)
            if self.state in (State.EXITED, State.CRASHED, State.FAILED):
                await self._cleanup_runtime()
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

    # -- crash safety / heartbeat --------------------------------------

    def configure_restart(self, policy: RestartPolicy) -> None:
        self.restart_policy = policy

    def set_framed_ping(self, ping: Any) -> None:
        """Install an async ``ping()`` coroutine from the framed client, if any."""
        self._framed_ping = ping

    def _classify_exit(self, rc: int | None) -> str:
        if self._requested_stop or self.state in (State.STOPPING, State.EXITED):
            return "requested_stop"
        if self.program_exit_code is not None or self.exit_reason == "natfeats":
            return "guest_exit"
        if self.crash_loop:
            return "crash_loop"
        if rc is not None and rc < 0:
            sig = -rc
            if sig == signal.SIGKILL:
                return "host_kill"
            if sig == signal.SIGTERM:
                return "terminated"
            return f"signal_{sig}"
        if rc == 0:
            return "process_exit"
        return "crash"

    def _schedule_restart(self) -> None:
        if self._requested_stop or not self.restart_policy.enabled:
            return
        if self.state is State.STARTING or self.started_at is None:
            return
        if self.program_exit_code is not None or self.exit_reason == "natfeats":
            return
        if self.exit_cause != "crash":
            return
        now = time.monotonic()
        window = max(0.0, float(self.restart_policy.window_seconds))
        self._restart_times = [stamp for stamp in self._restart_times if now - stamp <= window]
        if len(self._restart_times) >= max(0, self.restart_policy.max_restarts):
            self.crash_loop = True
            self.exit_cause = "crash_loop"
            self.last_error = (
                f"Crash loop: {len(self._restart_times)} restarts within {window:.0f}s; not restarting."
            )
            return
        if self._restart_task and not self._restart_task.done():
            return
        attempt = len(self._restart_times)
        self._restart_times.append(now)
        self.restart_count += 1
        delay = self.restart_policy.delay(attempt)
        self._restart_task = asyncio.create_task(self._restart_after(delay))

    async def _restart_after(self, delay: float) -> None:
        try:
            if delay > 0:
                await asyncio.sleep(delay)
            async with self._lifecycle_lock:
                if self._requested_stop or not self.restart_policy.enabled:
                    return
                if self.state != State.CRASHED or self._launch_kwargs is None:
                    return
                self._requested_stop = False
                try:
                    await self._start_locked(**self._launch_kwargs)
                except Exception as exc:
                    self.state = State.CRASHED
                    self.exit_cause = "restart_failed"
                    self.last_error = f"Automatic restart failed: {exc}"
                    self._restart_task = None
                    self._schedule_restart()
        except asyncio.CancelledError:
            raise
        finally:
            if self._restart_task is asyncio.current_task():
                self._restart_task = None

    def _start_heartbeat_task(self) -> None:
        if self._heartbeat_task and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

    async def _heartbeat_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self.heartbeat_interval)
                if self._stopping or self._requested_stop:
                    continue
                if self.proc is None or self.proc.returncode is not None:
                    continue
                if self._parent_pid and os.getppid() != self._parent_pid:
                    # Parent-death fallback: never leave an orphan emulator.
                    # SIGKILL (not SIGTERM) so a SIGSTOPped child still dies.
                    self._signal_child(signal.SIGCONT)
                    with contextlib.suppress(Exception):
                        self.proc.kill()
                    continue
                now = time.monotonic()
                if self.state is State.UNRESPONSIVE:
                    # Keep probing at a slower cadence; unresponsive is not a
                    # one-way door. A later successful probe recovers to RUNNING.
                    slow = max(self.heartbeat_probe_interval, self.heartbeat_interval * 4)
                    if now - self._last_probe < slow:
                        continue
                    self._last_probe = now
                    alive = await self._probe_liveness()
                    if alive is True and self.state is State.UNRESPONSIVE:
                        self._set_state(State.RUNNING)
                        self.last_pong = time.time()
                        self.stderr_ring.add(
                            "lifecycle", "Hatari recovered from the unresponsive state."
                        )
                    continue
                if self.state not in HEARTBEAT_ACTIVE:
                    continue
                if now - self._last_probe < self.heartbeat_probe_interval:
                    continue
                self._last_probe = now
                alive = await self._probe_liveness()
                if alive is None:
                    # A skipped probe is neither a pong nor a miss.
                    continue
                if self.state not in HEARTBEAT_ACTIVE:
                    continue
                if alive:
                    self.heartbeat_misses = 0
                    self.last_pong = time.time()
                    continue
                self.heartbeat_misses += 1
                if self.heartbeat_misses < self.heartbeat_misses_threshold:
                    continue
                self.state = State.UNRESPONSIVE
                self.unresponsive_since = time.time()
                self.last_error = (
                    f"Hatari missed {self.heartbeat_misses} heartbeat(s); process is unresponsive."
                )
                self.stderr_ring.add("lifecycle", self.last_error)
                if self.restart_policy.terminate_unresponsive:
                    with contextlib.suppress(Exception):
                        self.proc.terminate()
        except asyncio.CancelledError:
            raise

    async def _probe_liveness(self) -> bool | None:
        if self._framed_ping is not None:
            try:
                result = await asyncio.wait_for(self._framed_ping(), self.heartbeat_timeout)
            except Exception:
                return False
            return bool(result)
        return await self._probe_legacy_liveness()

    async def _probe_socket_echo(self, timeout: float) -> bool:
        """Send one echo sentinel over the socket and wait for it (no lock)."""
        if self._socket_writer is None:
            return False
        token = f"__HMCP_HB_{uuid.uuid4().hex}__"
        waiter = _Waiter(token, asyncio.get_running_loop().create_future())
        self._waiter = waiter
        try:
            await self._write_socket(f"hatari-debug echo {token}\n")
            await asyncio.wait_for(asyncio.shield(waiter.future), timeout)
            return True
        except (asyncio.TimeoutError, HatariError, ConnectionError, OSError):
            return False
        finally:
            if self._waiter is waiter:
                self._waiter = None

    async def _probe_legacy_liveness(self) -> bool | None:
        if self._command_lock.locked():
            # Do not race a real tool call; skipping is not a missed pong.
            return None
        if self._socket_writer is None:
            return False
        async with self._command_lock:
            if self.state not in (State.RUNNING, State.UNRESPONSIVE) or self._socket_writer is None:
                return None
            return await self._probe_socket_echo(self.heartbeat_timeout)

    async def autosave_state(self, path: str | Path, *, timeout_ms: int = 10000) -> dict[str, Any]:
        """Snapshot guest state atomically; off by default, caller scheduled.

        A hard-killed emulator cannot be snapshotted afterwards, so this only
        guarantees the last successful periodic snapshot.
        """
        raw = str(path)
        if any(char in raw for char in ('"', "\x00", "\n", "\r")):
            raise error(
                "bad_option",
                "Autosave path contains a forbidden quote or control character.",
                "Use a plain filesystem path without quotes/newlines.",
            )
        target = Path(path).expanduser().resolve()
        if target.exists() and target.is_dir():
            raise error("bad_option", f"Autosave path is a directory: {target}", "Pass a file path.")
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        try:
            result = await self.debug(f'statesave "{temp}"', timeout_ms=timeout_ms)
            if not temp.is_file():
                raise error(
                    "autosave_failed",
                    "Hatari did not create the state snapshot.",
                    "Check the debugger output and the artifact path.",
                )
            data = temp.read_bytes()
            atomic_write_bytes(target, data)
            return {"ok": True, "path": str(target), "bytes": len(data), "debug": result}
        finally:
            with contextlib.suppress(FileNotFoundError):
                temp.unlink()

    def _protocol_status(self) -> dict[str, Any]:
        try:
            from .protocol.selector import selection_for_status
            from .protocol.hello import find_banner

            banner = self.protocol_banner
            if banner is None and self.protocol_banner_raw:
                banner = find_banner([self.protocol_banner_raw])
            return selection_for_status(
                banner=banner, banner_error=self.protocol_banner_error
            )
        except Exception as exc:  # never let capability reporting break status
            from .protocol.capabilities import describe

            report = describe(requested_mode="auto")
            report["protocol_available"] = False
            report["diagnostics"].append(f"capability error: {exc}")
            return report

    def _close_socket(self) -> None:
        if self._socket_writer:
            with contextlib.suppress(Exception):
                self._socket_writer.close()

    async def _on_socket_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if self._socket_writer is not None:
            writer.close()
            return
        self._socket_reader = reader
        self._socket_writer = writer
        if self._socket_connected and not self._socket_connected.done():
            self._socket_connected.set_result(True)
        self._socket_task = asyncio.current_task()
        try:
            while True:
                chunk = await reader.read(4096)
                if not chunk:
                    break
        except (asyncio.CancelledError, ConnectionError):
            pass
        finally:
            with contextlib.suppress(Exception):
                writer.close()

    async def _read_stdout(self) -> None:
        assert self.proc and self.proc.stdout
        stream = self.proc.stdout
        while True:
            try:
                chunk = await stream.read(4096)
            except (asyncio.CancelledError, ConnectionError):
                break
            if not chunk:
                break
            self._stdout_partial += chunk.decode("utf-8", "replace")
            if len(self._stdout_partial) > 1_000_000:
                self._stdout_partial = self._stdout_partial[-500_000:]
            while "\n" in self._stdout_partial:
                line, self._stdout_partial = self._stdout_partial.split("\n", 1)
                self._ingest_stdout(line.rstrip("\r"))
        if self._stdout_partial:
            self._ingest_stdout(self._stdout_partial)
            self._stdout_partial = ""

    async def _read_stderr(self) -> None:
        assert self.proc and self.proc.stderr
        stream = self.proc.stderr
        while True:
            try:
                chunk = await stream.read(4096)
            except (asyncio.CancelledError, ConnectionError):
                break
            if not chunk:
                break
            self._stderr_partial += chunk.decode("utf-8", "replace")
            if len(self._stderr_partial) > 1_000_000:
                self._stderr_partial = self._stderr_partial[-500_000:]
            self._drain_stderr()
        if self._stderr_partial:
            self._ingest_stderr(self._stderr_partial)
            self._stderr_partial = ""

    def _drain_stderr(self) -> None:
        if PAGER_RE.search(self._stderr_partial):
            self._pager_blocked = True
            if self._stdin:
                with contextlib.suppress(Exception):
                    self._stdin.write(b"q\n")
            if self._waiter and not self._waiter.future.done():
                self._waiter.future.set_exception(
                    error(
                        "pager_blocked",
                        "Hatari's debugger pager tried to read stdin.",
                        "The generated config should disable paging; report this as a bug.",
                    )
                )
                self._waiter = None
            self._stderr_partial = PAGER_RE.sub("", self._stderr_partial)

        while self._waiter and self._waiter.token in self._stderr_partial:
            before, self._stderr_partial = self._stderr_partial.split(self._waiter.token, 1)
            self._consume_stderr_text(before)
            waiter, self._waiter = self._waiter, None
            if not waiter.future.done():
                waiter.future.set_result("\n".join(waiter.lines))

        while "\n" in self._stderr_partial:
            line, self._stderr_partial = self._stderr_partial.split("\n", 1)
            self._ingest_stderr(line.rstrip("\r"))

    def _consume_stderr_text(self, text: str) -> None:
        for line in text.splitlines():
            self._ingest_stderr(line.rstrip("\r"))

    def _ingest_stdout(self, line: str) -> None:
        kind = "prompt" if PROMPT_RE.match(line) else "console"
        self.stdout_ring.add(kind, line)

    def _accept_banner(self, line: str) -> None:
        """Accept one valid nonce-bound banner inside a bounded startup window.

        An invalid first line (wrong nonce, wrong version, no ``framed``) is
        diagnostic only; it does not consume the slot, so a later valid banner
        can replace it. Only a successfully parsed banner marks ``_banner_seen``.
        """
        if self._banner_seen:
            return
        if not self._banner_window_open:
            return
        if self._banner_deadline is not None and time.monotonic() > self._banner_deadline:
            self._banner_window_open = False
            if self.protocol_banner is None:
                self.protocol_banner_error = (
                    "protocol banner arrived after the startup window closed; ignored"
                )
            return
        if self._stopping or self.state not in ALIVE:
            # Guest output after the lifecycle left the live states cannot
            # influence protocol reporting.
            return
        if not self._banner_nonce:
            self.protocol_banner_error = "no per-launch protocol nonce is configured; banner ignored"
            return
        try:
            from .protocol.hello import parse_banner_line

            banner = parse_banner_line(line, expected_nonce=self._banner_nonce)
        except HatariError as exc:
            self.protocol_banner_error = str(exc)
            return
        except Exception as exc:  # defensive: never let a banner break logging
            self.protocol_banner_error = f"invalid banner: {exc}"
            return
        if banner is None or "framed" not in banner.capabilities:
            self.protocol_banner_error = "banner does not advertise the framed capability; ignored"
            return
        # Advisory only: capabilities stay banner-only until a real handshake.
        self._banner_seen = True
        self.protocol_banner = banner
        self.protocol_banner_raw = line.strip()
        self.protocol_banner_error = None

    def _ingest_stderr(self, line: str) -> None:
        if not line:
            return
        if "HATARI-MCP-PROTOCOL" in line:
            self._accept_banner(line)
        token_at = line.find("__HMCP_")
        if token_at >= 0:
            line = line[:token_at].rstrip()
            if not line:
                return
        if PROMPT_RE.match(line) and line.strip().lower() in {"> echo", ">echo"}:
            return
        if self._waiter:
            self._waiter.lines.append(line)
        kind = "debugger" if PROMPT_RE.match(line) else "log"
        # Guest stderr is untrusted whenever NatFeats is on: NF_STDERR can
        # reproduce any debugger/NatFeats text. Only the explicit NatFeats
        # exit line is still honoured (the user opted into NF_EXIT).
        trust_state = not self._natfeats_enabled
        # Once a stop begins, late queued stderr lines from the exiting child
        # must not revive RUNNING/AT_PROMPT and misclassify a requested stop.
        transitions_allowed = trust_state and not self._stopping and self.state in ALIVE
        if BREAKPOINT_RE.search(line):
            kind = "breakpoint_hit"
            if transitions_allowed:
                self._set_state(State.AT_PROMPT)
        elif EXCEPTION_RE.search(line):
            kind = "exception"
            if transitions_allowed:
                self._set_state(State.AT_PROMPT)
                match = EXCEPTION_RE.search(line)
                pc_match = re.search(r"at\s+\$?([0-9A-Fa-f]+)", line)
                self.exception = {
                    "type": match.group(1) if match else "exception",
                    "pc": pc_match.group(1) if pc_match else None,
                    "line": line,
                }
        elif DEBUGGER_ENTER_RE.search(line):
            kind = "debugger"
            if transitions_allowed:
                self._set_state(State.AT_PROMPT)
        elif "Returning to emulation" in line:
            kind = "debugger"
            if transitions_allowed:
                self._set_state(State.RUNNING)
                self.exception = None
        elif NATFEATS_EXIT_RE.search(line):
            kind = "natfeats"
            if self._natfeats_enabled:
                match = NATFEATS_EXIT_RE.search(line)
                if match:
                    self.program_exit_code = int(match.group(1))
                    self.exit_reason = "natfeats"
        elif line.startswith("NatFeats:"):
            kind = "natfeats"
        elif line.startswith("ERROR") or "ERROR:" in line:
            kind = "error"
        self.stderr_ring.add(kind, line)

    # -- channel writes ------------------------------------------------

    async def _write_socket(self, data: str) -> None:
        if not self._socket_writer:
            raise error("emulator_gone", "Control socket is not connected.", "Call emu_status, then emu_start.")
        self._socket_writer.write(data.encode("latin-1", "replace"))
        await self._socket_writer.drain()

    async def _write_stdin(self, data: str) -> None:
        if not self._stdin:
            raise error("emulator_gone", "Hatari stdin is not available.", "Call emu_status, then emu_start.")
        self._stdin.write(data.encode("latin-1", "replace"))
        await self._stdin.drain()

    async def _socket_command(self, command: str) -> None:
        await self._write_socket(command + "\n")

    # -- debugger commands ---------------------------------------------

    async def debug(
        self,
        command: str,
        *,
        channel: str = "auto",
        timeout_ms: int | None = None,
    ) -> dict[str, Any]:
        command = command.strip("\r\n")
        if not command:
            raise error("bad_option", "Empty debugger command.", "Pass a non-empty command.")
        if "\n" in command or "\r" in command:
            raise error("bad_option", "Debugger command must be one line.", "Send a single command.")
        if len(command.encode("utf-8")) > self.settings.max_command_bytes:
            raise error(
                "command_too_long",
                f"Command exceeds {self.settings.max_command_bytes} bytes.",
                "Split the operation into smaller commands.",
            )
        timeout = (timeout_ms / 1000.0) if timeout_ms else self.settings.command_timeout
        word = command.split()[0].lower()
        async with self._command_lock:
            if self.state in (State.STOPPED, State.EXITED, State.CRASHED, State.FAILED, State.STOPPING):
                raise error("not_running", f"Hatari is {self.state.value}.", "Call emu_start first.")
            if self.state == State.STARTING:
                raise error("not_running", "Hatari is still starting.", "Wait for emu_start to return.")

            use_stdin = self.state == State.AT_PROMPT if channel == "auto" else channel == "stdin"
            if channel not in ("auto", "socket", "stdin"):
                raise error("bad_option", f"Unknown channel: {channel}", "Use auto, socket or stdin.")
            if use_stdin and self.state not in WRITABLE_STDIN:
                raise error(
                    "not_stopped",
                    f"Hatari is {self.state.value}, not at the debugger prompt.",
                    "Use channel='socket' while running, or stop at a breakpoint first.",
                )
            if not use_stdin and self.state is State.REMOTE_PAUSED:
                raise error(
                    "not_stopped",
                    "Hatari is remote-paused; its control loop is stopped by SIGSTOP.",
                    "Call emu_resume first; debugger commands are unavailable while paused.",
                )
            if not use_stdin and self.state not in WRITABLE_SOCKET:
                raise error(
                    "not_stopped",
                    f"Hatari is {self.state.value} and cannot take socket commands.",
                    "Use channel='stdin' at the debugger prompt.",
                )

            if word in TERMINAL:
                await self._terminal_command(command, use_stdin=use_stdin)
                return CommandResult("", "stdin" if use_stdin else "socket", self.state.value).as_dict()

            token = f"__HMCP_{uuid.uuid4().hex}__"
            waiter = _Waiter(token, asyncio.get_running_loop().create_future())
            self._waiter = waiter
            try:
                if use_stdin:
                    await self._write_stdin(f"{command}\necho {token}\n")
                else:
                    await self._write_socket(
                        f"hatari-debug {command}\nhatari-debug echo {token}\n"
                    )
                try:
                    await asyncio.wait_for(asyncio.shield(waiter.future), timeout)
                except asyncio.TimeoutError:
                    lines = list(waiter.lines)
                    raise error(
                        "timeout",
                        f"Debugger command timed out after {timeout:.1f}s.",
                        "Call emu_status; the machine may be stopped at a breakpoint.",
                        raw="\n".join(lines),
                    ) from None
                lines = waiter.future.result().splitlines()
                clean = self._clean_lines(lines, command)
                return CommandResult(
                    "\n".join(clean),
                    "stdin" if use_stdin else "socket",
                    self.state.value,
                    lines=clean,
                ).as_dict()
            finally:
                if self._waiter is waiter:
                    self._waiter = None

    def _clean_lines(self, lines: list[str], command: str) -> list[str]:
        out: list[str] = []
        for line in lines:
            stripped = line.strip()
            if not stripped:
                out.append("")
                continue
            if stripped == command or stripped == f"> {command}":
                continue
            if stripped.startswith("> ") and stripped[2:].strip() == command:
                continue
            if "__HMCP_" in stripped:
                continue
            if stripped.lower() in {"> echo", ">echo"}:
                continue
            out.append(line)
        while out and not out[0]:
            out.pop(0)
        while out and not out[-1]:
            out.pop()
        return out

    async def _terminal_command(self, command: str, *, use_stdin: bool) -> None:
        word = command.split()[0].lower()
        if use_stdin:
            await self._write_stdin(command + "\n")
        else:
            await self._write_socket(f"hatari-debug {command}\n")
        if word in {"quit", "q", "exit"}:
            self.state = State.STOPPING
            if self.proc and self.proc.returncode is None:
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self.proc.wait(), self.settings.stop_timeout)
            self._set_state(State.EXITED)
            self.returncode = self.proc.returncode if self.proc else None
            self.exit_reason = self.exit_reason or "debugger_quit"
        elif word == "reset":
            self._set_state(State.RUNNING)
        else:
            self._set_state(State.RUNNING)

    async def probe_prompt(self, timeout: float = 0.75) -> bool:
        if self.state == State.AT_PROMPT:
            return True
        if self.state not in ALIVE:
            return False
        token = f"__HMCP_PROBE_{uuid.uuid4().hex}__"
        waiter = _Waiter(token, asyncio.get_running_loop().create_future())
        async with self._command_lock:
            self._waiter = waiter
            try:
                await self._write_stdin(f"echo {token}\n")
                await asyncio.wait_for(asyncio.shield(waiter.future), timeout)
                self._set_state(State.AT_PROMPT)
                return True
            except (asyncio.TimeoutError, HatariError):
                return False
            finally:
                if self._waiter is waiter:
                    self._waiter = None

    async def pause(self) -> dict[str, Any]:
        async with self._command_lock:
            if self.state == State.AT_PROMPT:
                raise error(
                    "not_stopped",
                    "Hatari is at the debugger prompt, not remotely running.",
                    "Use dbg_command with channel='stdin', then continue before pausing.",
                )
            if self.state == State.REMOTE_PAUSED:
                return self.status()
            if self.state != State.RUNNING:
                raise error("not_running", f"Hatari is {self.state.value}.", "Call emu_start first.")
            if not self._signal_child(signal.SIGSTOP):
                raise error(
                    "unsupported",
                    "Remote pause needs a live POSIX child process.",
                    "Run Hatari on Linux/macOS, or use a debugger breakpoint instead.",
                )
            self._set_state(State.REMOTE_PAUSED)
            return self.status()

    async def resume(self) -> dict[str, Any]:
        async with self._command_lock:
            if self.state == State.RUNNING:
                return self.status()
            if self.state == State.AT_PROMPT:
                await self._write_stdin("cont\n")
                self._set_state(State.RUNNING)
                return self.status()
            if self.state != State.REMOTE_PAUSED:
                raise error("not_running", f"Hatari is {self.state.value}.", "Call emu_start first.")
            if not self._signal_child(signal.SIGCONT):
                raise error(
                    "unsupported",
                    "Remote resume needs a live POSIX child process.",
                    "Call emu_status and restart Hatari if it has exited.",
                )
            # Acknowledge before claiming RUNNING: SIGCONT alone is only a
            # request; wait for the control loop to answer an echo sentinel.
            timeout = max(1.0, self.heartbeat_timeout * 2)
            if not await self._probe_socket_echo(timeout):
                self._set_state(State.REMOTE_PAUSED)
                raise error(
                    "timeout",
                    "Hatari did not acknowledge resume after SIGCONT.",
                    "Call emu_status; retry emu_resume or stop/restart the emulator.",
                )
            self._set_state(State.RUNNING)
            return self.status()

    async def wait_for_exit(self, timeout_ms: int) -> dict[str, Any]:
        timeout = max(1, timeout_ms) / 1000.0
        if self._exit_event is None:
            raise error("not_running", "Hatari has not been started.", "Call emu_start first.")
        start = time.time()
        timed_out = False
        try:
            await asyncio.wait_for(self._exit_event.wait(), timeout)
        except asyncio.TimeoutError:
            timed_out = True
        return {
            "ok": True,
            "exited": not timed_out,
            "timed_out": timed_out,
            "exit_code": self.program_exit_code if self.program_exit_code is not None else self.returncode,
            "exit_reason": self.exit_reason,
            "duration_ms": int((time.time() - start) * 1000),
        }

    # -- status --------------------------------------------------------

    def status(self) -> dict[str, Any]:
        now = time.time()
        return {
            "ok": True,
            "state": self.state.value,
            "pid": self.proc.pid if self.proc else None,
            "returncode": self.returncode,
            "program_exit_code": self.program_exit_code,
            "exit_reason": self.exit_reason,
            "since": self.started_at,
            "uptime_ms": int((now - self.started_at) * 1000) if self.started_at and self.state in ALIVE else 0,
            "socket": str(self.runtime.socket_path) if self.runtime else None,
            "last_error": self.last_error,
            "exception": self.exception,
            "stdout_cursor": self.stdout_ring._next - 1,
            "stderr_cursor": self.stderr_ring._next - 1,
            "at_prompt": self.state == State.AT_PROMPT,
            "paused": self.state is State.REMOTE_PAUSED,
            "unresponsive": self.state is State.UNRESPONSIVE,
            "unresponsive_since": self.unresponsive_since,
            "natfeats": self._natfeats_enabled,
            "exit_cause": self.exit_cause,
            "protocol_banner": self.protocol_banner_raw,
            "protocol_banner_error": self.protocol_banner_error,
            "capabilities": self._protocol_status(),
            "heartbeat": {
                "interval": self.heartbeat_interval,
                "probe_interval": self.heartbeat_probe_interval,
                "timeout": self.heartbeat_timeout,
                "misses": self.heartbeat_misses,
                "threshold": self.heartbeat_misses_threshold,
                "last_pong": self.last_pong,
            },
            "restart": {
                "enabled": self.restart_policy.enabled,
                "count": self.restart_count,
                "crash_loop": self.crash_loop,
                "requested_stop": self._requested_stop,
                "policy": {
                    "max_restarts": self.restart_policy.max_restarts,
                    "window_seconds": self.restart_policy.window_seconds,
                    "backoff_seconds": list(self.restart_policy.backoff_seconds),
                },
            },
        }
