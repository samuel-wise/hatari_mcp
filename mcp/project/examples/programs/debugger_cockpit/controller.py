"""Blocking debugger operations used by the Tk cockpit.

Every method here is safe to run on a worker thread.  The Tk layer only sees
plain dictionaries.  A small client proxy logs every MCP call and result.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from common import paths
from common.debug_session import DebugSession
from common.game_runner import ALIVE_STATES, TERMINAL_STATES
from common.mcp_client import MCPClient

from .addresses import format_address
from .commands import build_breakpoint_command, parse_breakpoint_list
from .triage import write_triage_bundle

EventSink = Callable[[str, str], None]


def default_state_dir() -> Path:
    env = os.environ.get("HATARI_COCKPIT_STATE")
    base = Path(env).expanduser() if env else paths.state_dir() / "debugger_cockpit"
    return base.resolve()


def _summary(value: Any, limit: int = 500) -> str:
    text = repr(value)
    if len(text) > limit:
        text = text[: limit - 3] + "..."
    return text


class LoggingMCPClient:
    """Thin proxy that logs each MCP call and result to an event sink."""

    def __init__(self, client: MCPClient, event_sink: EventSink | None = None):
        self.client = client
        self.event_sink = event_sink

    def _emit(self, kind: str, message: str) -> None:
        self.emit(kind, message)

    def emit(self, kind: str, message: str) -> None:
        if self.event_sink is None:
            return
        try:
            self.event_sink(kind, message)
        except BaseException:
            pass

    def call_tool(self, name: str, timeout: float | None = None, **args: Any) -> dict[str, Any]:
        self._emit("call", f"{name}({_summary(args)})")
        started = time.monotonic()
        try:
            result = self.client.call_tool(name, timeout=timeout, **args)
        except BaseException as exc:
            elapsed = (time.monotonic() - started) * 1000
            self._emit("error", f"{name} failed after {elapsed:.0f} ms: {exc}")
            raise
        elapsed = (time.monotonic() - started) * 1000
        self._emit("result", f"{name} ok in {elapsed:.0f} ms: {_summary(result)}")
        return result

    def call(self, name: str, timeout: float | None = None, **args: Any) -> dict[str, Any]:
        return self.call_tool(name, timeout=timeout, **args)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.client, name)


class CockpitController:
    def __init__(
        self,
        *,
        state_dir: str | Path | None = None,
        machine: str = "st",
        tos: str | Path | None = None,
        event_sink: EventSink | None = None,
        client_factory: Callable[[], MCPClient] | None = None,
    ):
        self.state_dir = Path(state_dir).expanduser().resolve() if state_dir else default_state_dir()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.machine = machine
        self.tos = Path(tos).expanduser() if tos else None
        self.event_sink = event_sink
        self._client_factory = client_factory or (
            lambda: MCPClient(call_timeout=60.0, shutdown_timeout=15.0)
        )
        self._lock = threading.RLock()
        self._client: LoggingMCPClient | None = None
        self._session: DebugSession | None = None
        self._exception_debug_enabled = False

    # -- lifecycle -----------------------------------------------------

    @property
    def connected(self) -> bool:
        return self._client is not None

    @property
    def session(self) -> DebugSession | None:
        return self._session

    @property
    def sample_source(self) -> Path:
        return Path(__file__).resolve().with_name("sample") / "cockpit_sample.C"

    @property
    def sample_output(self) -> Path:
        return self.state_dir / "build" / "COCKPIT.TOS"

    def _emit(self, kind: str, message: str) -> None:
        self.emit(kind, message)

    def emit(self, kind: str, message: str) -> None:
        if self.event_sink is None:
            return
        try:
            self.event_sink(kind, message)
        except BaseException:
            pass

    def connect(self) -> None:
        with self._lock:
            if self._client is not None:
                return
            self._emit("info", "starting MCP server")
            raw = self._client_factory()
            raw.start()
            client = LoggingMCPClient(raw, self.event_sink)
            session = DebugSession(
                client,
                state_dir=self.state_dir,
                machine=self.machine,
                tos=self.tos,
            )
            self._client = client
            self._session = session
            self._emit("info", f"MCP connected: {raw.python}")

    def _require_session(self) -> DebugSession:
        self.connect()
        assert self._session is not None
        return self._session

    def close(self) -> None:
        with self._lock:
            client = self._client
            self._client = None
            self._session = None
        if client is not None:
            self._emit("info", "closing MCP client")
            try:
                client.stop()
            except BaseException as exc:
                self._emit("error", f"MCP close failed: {exc}")

    # -- emulator lifecycle -------------------------------------------

    def status(self) -> dict[str, Any]:
        return self._require_session().status()

    def attach_or_start(self) -> dict[str, Any]:
        session = self._require_session()
        status = session.status()
        state = str(status.get("state"))
        if state in ALIVE_STATES:
            self._emit("info", f"attached to live emulator ({state})")
            return status
        self._emit("info", f"starting EmuTOS ({self.machine})")
        status = session.start_emutos(machine=self.machine, tos=self.tos, timeout=45.0)
        self._exception_debug_enabled = False
        return status

    def start_emutos(self) -> dict[str, Any]:
        session = self._require_session()
        status = session.start_emutos(machine=self.machine, tos=self.tos, timeout=45.0)
        self._exception_debug_enabled = False
        return status

    def load_prg(self, path: str | Path) -> dict[str, Any]:
        session = self._require_session()
        status = session.load_prg(path, machine=self.machine, tos=self.tos, timeout=45.0)
        self._exception_debug_enabled = False
        return status

    def enable_exception_debugger(self, flags: str = "all") -> None:
        """Enable Hatari's CPU-exception debugger after boot.

        ``--debug-except`` only changes Hatari's saved mask; the live mask is
        enabled by ``--debug``.  Doing this after the debugger prompt avoids
        stopping on EmuTOS' early illegal-instruction probes.
        """
        if self._exception_debug_enabled:
            return
        session = self._require_session()
        try:
            session.client.call_tool(
                "dbg_command", command=f"setopt --debug-except {flags}", timeout=10.0
            )
            session.client.call_tool("dbg_command", command="setopt --debug", timeout=10.0)
            self._exception_debug_enabled = True
            self._emit("info", f"exception debugging enabled ({flags})")
        except BaseException as exc:
            self._emit("warn", f"could not enable exception debugging: {exc}")

    def enter_debugger(self) -> dict[str, Any]:
        session = self._require_session()
        status = session.ensure_prompt(timeout=12.0)
        self.enable_exception_debugger()
        return status

    def stop_emulator(self, *, force: bool = False) -> dict[str, Any]:
        session = self._require_session()
        status = session.stop(force=force)
        self._exception_debug_enabled = False
        return status

    def build_sample(self) -> dict[str, Any]:
        session = self._require_session()
        source = self.sample_source
        output = self.sample_output
        output.parent.mkdir(parents=True, exist_ok=True)
        self._emit("info", f"building sample {source.name} -> {output}")
        try:
            result = session.client.call_tool(
                "ahcc_compile",
                source=str(source),
                output=str(output),
                timeout_ms=120000,
                timeout=180.0,
            )
            return {**result, "source": str(source), "output": str(output), "fallback": None}
        except BaseException as exc:
            script = paths.repo_root() / "mcp/project/scripts/ahcc-cli.py"
            if not script.is_file():
                raise RuntimeError(
                    f"MCP AHCC build failed: {exc}. ahcc-cli.py fallback not found at {script}."
                ) from exc
            self._emit("info", f"MCP AHCC build failed, falling back to {script.name}: {exc}")
            completed = subprocess.run(
                [sys.executable, str(script), str(source), "-o", str(output)],
                capture_output=True,
                text=True,
                timeout=240,
                check=False,
            )
            if completed.returncode != 0:
                detail = (completed.stderr or completed.stdout or "").strip()
                raise RuntimeError(
                    f"AHCC build failed (MCP: {exc}; CLI: {detail[-1200:]})"
                ) from exc
            return {
                "ok": True,
                "source": str(source),
                "output": str(output),
                "fallback": str(script),
                "stdout": completed.stdout[-4000:],
                "stderr": completed.stderr[-4000:],
            }

    # -- inspection ----------------------------------------------------

    def wait_until_stopped(self, *, timeout: float = 15.0) -> dict[str, Any]:
        session = self._require_session()
        deadline = time.monotonic() + max(0.5, timeout)
        last = session.status()
        while time.monotonic() < deadline:
            last = session.status()
            if last.get("at_prompt") or last.get("exception"):
                return last
            if str(last.get("state")) in TERMINAL_STATES:
                return last
            time.sleep(0.15)
        return {**last, "timed_out": True}

    def read_context(self, *, console_lines: int = 200) -> dict[str, Any]:
        session = self._require_session()
        context: dict[str, Any] = {
            "status": {},
            "registers": {},
            "disassembly": [],
            "stack": None,
            "backtrace": None,
            "console_tail": [],
            "errors": [],
        }
        try:
            context["status"] = session.status()
        except BaseException as exc:
            context["errors"].append(f"status: {exc}")
            return context
        status = context["status"]
        try:
            context["console_tail"] = self.console_tail(console_lines)
        except BaseException as exc:
            context["errors"].append(f"console: {exc}")
        if not (status.get("at_prompt") or status.get("exception")):
            return context
        registers: dict[str, Any] = {}
        try:
            result = session.registers()
            registers = result.get("registers") or {}
            context["registers"] = registers
        except BaseException as exc:
            context["errors"].append(f"registers: {exc}")
        pc = registers.get("pc")
        if isinstance(pc, int):
            try:
                disassembly = session.disassemble(format_address(pc), count=20)
                context["disassembly"] = list(disassembly.get("instructions") or [])
            except BaseException as exc:
                context["errors"].append(f"disassembly: {exc}")
        try:
            context["stack"] = session.stack(words=48)
        except BaseException as exc:
            context["errors"].append(f"stack: {exc}")
        try:
            context["backtrace"] = session.client.call_tool(
                "dbg_backtrace", max_frames=16, timeout=10.0
            )
        except BaseException as exc:
            context["errors"].append(f"backtrace: {exc}")
        return context

    def registers(self) -> dict[str, Any]:
        return self._require_session().registers()

    def disassemble(self, address: int | str, count: int = 20) -> dict[str, Any]:
        session = self._require_session()
        text = format_address(address) if isinstance(address, int) else str(address)
        return session.disassemble(text, count=count)

    def read_memory(self, address: int | str, length: int) -> dict[str, Any]:
        return self._require_session().memory_read(address, length)

    def write_memory(self, address: int | str, data_hex: str) -> dict[str, Any]:
        return self._require_session().memory_write(address, data_hex)

    def console_tail(self, lines: int = 200) -> list[str]:
        client = self._require_session().client
        result = client.call_tool("console_read", lines=lines, timeout=10.0)
        return [
            str(entry.get("text", ""))
            for entry in result.get("entries", [])
            if isinstance(entry, dict)
        ]

    def log_tail(self, lines: int = 200) -> list[str]:
        client = self._require_session().client
        result = client.call_tool("log_read", lines=lines, timeout=10.0)
        return [
            str(entry.get("text", ""))
            for entry in result.get("entries", [])
            if isinstance(entry, dict)
        ]

    # -- execution and breakpoints ------------------------------------

    def set_breakpoint(
        self,
        address: int | str,
        *,
        type: str = "exec",
        condition: str | None = None,
        once: bool = True,
        quiet: bool = False,
        count: int | None = None,
    ) -> dict[str, Any]:
        command = build_breakpoint_command(
            address,
            type=type,
            condition=condition,
            once=once,
            quiet=quiet,
            count=count,
        )
        self._emit("info", f"breakpoint command: {command.command}")
        session = self._require_session()
        return session.breakpoint_set(
            address,
            type=type,
            condition=condition,
            once=once,
            quiet=quiet,
            count=count,
        )

    def list_breakpoints(self) -> dict[str, Any]:
        result = self._require_session().breakpoint_list()
        entries = parse_breakpoint_list(str(result.get("raw", "")))
        return {**result, "entries": entries}

    def delete_breakpoint(self, index: int) -> dict[str, Any]:
        return self._require_session().breakpoint_delete(index)

    def run_to(self, address: int | str, *, timeout: float = 15.0) -> dict[str, Any]:
        session = self._require_session()
        session.ensure_prompt(timeout=12.0)
        self.enable_exception_debugger()
        self.set_breakpoint(address, once=True, quiet=False)
        session.continue_run()
        status = self.wait_until_stopped(timeout=timeout)
        return {"address": str(address), "status": status, "timed_out": bool(status.get("timed_out"))}

    def step(self, count: int = 1) -> dict[str, Any]:
        session = self._require_session()
        result = session.step(count)
        return result

    def step_over(self) -> dict[str, Any]:
        return self._require_session().step_over()

    def continue_run(self) -> dict[str, Any]:
        return self._require_session().continue_run()

    # -- state ---------------------------------------------------------

    def write_register(self, register: str, value: int | str) -> dict[str, Any]:
        session = self._require_session()
        return session.client.call_tool(
            "dbg_registers_set", register=register, value=value, timeout=10.0
        )

    def save_state(self, path: str | Path) -> dict[str, Any]:
        return self._require_session().snapshot(path)

    def load_state(self, path: str | Path) -> dict[str, Any]:
        return self._require_session().load_snapshot(path)

    def screenshot(self, path: str | Path, *, format: str = "bmp") -> dict[str, Any]:
        session = self._require_session()
        target = Path(path).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        return session.client.call_tool(
            "screen_frame_capture",
            path=str(target),
            format=format,
            timeout=20.0,
        )

    def raw_command(self, command: str) -> dict[str, Any]:
        session = self._require_session()
        return session.client.call_tool("dbg_command", command=command, timeout=30.0)

    # -- crash triage --------------------------------------------------

    def capture_triage(self, reason: str = "manual capture", *, screenshot: bool = True) -> dict[str, Any]:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
        evidence: dict[str, Any] = {
            "captured_at": timestamp,
            "reason": reason,
            "program": None,
            "tos": None,
            "status": {},
            "exception": None,
            "registers": {},
            "disassembly": [],
            "stack": None,
            "backtrace": None,
            "console_tail": [],
            "log_tail": [],
            "screenshot": None,
            "errors": [],
        }
        try:
            session = self._require_session()
        except BaseException as exc:
            evidence["errors"].append(f"MCP session: {exc}")
            bundle = write_triage_bundle(self.state_dir, evidence, timestamp=timestamp)
            return {**bundle, "evidence": evidence}

        if session.current_prg is not None:
            evidence["program"] = str(session.current_prg)
        tos = self.tos or paths.tos_for_machine(self.machine)
        if tos is not None:
            evidence["tos"] = str(tos)

        try:
            evidence["status"] = session.status()
        except BaseException as exc:
            evidence["errors"].append(f"status: {exc}")
        try:
            evidence["exception"] = session.exception().get("exception")
        except BaseException as exc:
            evidence["errors"].append(f"exception: {exc}")

        registers: dict[str, Any] = {}
        try:
            result = session.registers()
            registers = result.get("registers") or {}
            evidence["registers"] = registers
        except BaseException as exc:
            evidence["errors"].append(f"registers: {exc}")

        pc = registers.get("pc")
        if isinstance(pc, int):
            try:
                result = session.disassemble(format_address(pc), count=16)
                evidence["disassembly"] = list(result.get("instructions") or [])
            except BaseException as exc:
                evidence["errors"].append(f"disassembly: {exc}")

        try:
            evidence["stack"] = session.stack(words=48)
        except BaseException as exc:
            evidence["errors"].append(f"stack: {exc}")
        try:
            evidence["backtrace"] = session.client.call_tool(
                "dbg_backtrace", max_frames=16, timeout=10.0
            )
        except BaseException as exc:
            evidence["errors"].append(f"backtrace: {exc}")
        try:
            evidence["console_tail"] = self.console_tail(200)
        except BaseException as exc:
            evidence["errors"].append(f"console: {exc}")
        try:
            evidence["log_tail"] = self.log_tail(200)
        except BaseException as exc:
            evidence["errors"].append(f"log: {exc}")

        if screenshot:
            target = self.state_dir / "screenshots" / f"{timestamp}-triage.bmp"
            try:
                captured = self.screenshot(target, format="bmp")
                evidence["screenshot"] = str(captured.get("path") or target)
            except BaseException as exc:
                evidence["errors"].append(f"screenshot: {exc}")

        bundle = write_triage_bundle(self.state_dir, evidence, timestamp=timestamp)
        return {**bundle, "evidence": evidence}

    def __enter__(self) -> "CockpitController":
        self.connect()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()
