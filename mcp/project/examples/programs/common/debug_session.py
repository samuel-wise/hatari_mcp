"""Small debugger helper shared by the three debugger example programs."""

from __future__ import annotations

import hashlib
import re
import shutil
import time
from pathlib import Path
from typing import Any

try:  # package import
    from . import paths
    from .game_runner import ALIVE_STATES, TERMINAL_STATES, profile_args
    from .mcp_client import MCPClient
except ImportError:  # direct script import
    import paths  # type: ignore[no-redef]
    from game_runner import ALIVE_STATES, TERMINAL_STATES, profile_args  # type: ignore[no-redef]
    from mcp_client import MCPClient  # type: ignore[no-redef]

BREAKPOINT_ID_RE = re.compile(r"condition breakpoint\s+(\d+)\s+with", re.IGNORECASE)


class DebugSessionError(RuntimeError):
    pass


def _breakpoint_id(raw: str) -> int | None:
    match = BREAKPOINT_ID_RE.search(raw)
    return int(match.group(1)) if match else None


class DebugSession:
    """Drive a Hatari debugger without duplicating MCP client internals."""

    def __init__(
        self,
        client: MCPClient,
        *,
        state_dir: str | Path | None = None,
        machine: str = "st",
        tos: str | Path | None = None,
        poll_interval: float = 0.15,
    ):
        self.client = client
        self.state_dir = Path(state_dir).expanduser().resolve() if state_dir else paths.state_dir()
        self.machine = machine
        self.tos = Path(tos).expanduser() if tos else None
        self.poll_interval = max(0.02, float(poll_interval))
        self.current_prg: Path | None = None

    # -- lifecycle -----------------------------------------------------

    def status(self) -> dict[str, Any]:
        return self.client.call_tool("emu_status", timeout=10.0)

    @property
    def at_prompt(self) -> bool:
        try:
            return bool(self.status().get("at_prompt"))
        except BaseException:
            return False

    def _stop_existing(self) -> None:
        try:
            state = str(self.status().get("state"))
        except BaseException:
            return
        if state in ALIVE_STATES:
            try:
                self.client.call_tool("emu_stop", force=True, timeout=20.0)
            except BaseException:
                pass

    def start_emutos(
        self,
        *,
        machine: str | None = None,
        tos: str | Path | None = None,
        args: list[str] | None = None,
        wait: bool = True,
        enter_debugger: bool = False,
        timeout: float = 45.0,
    ) -> dict[str, Any]:
        self._stop_existing()
        machine = (machine or self.machine or "st").lower()
        tos_path = Path(tos).expanduser() if tos else self.tos or paths.tos_for_machine(machine)
        launch = profile_args(machine) + list(args or [])
        self.client.call_tool(
            "emu_start",
            tos=str(tos_path) if tos_path else None,
            args=launch,
            display=False,
            timeout=max(60.0, timeout + 15.0),
        )
        status = self.status()
        if wait:
            status = self.wait_running(timeout=timeout)
        if enter_debugger:
            status = self.ensure_prompt(timeout=10.0)
        return status

    def load_prg(
        self,
        prg: str | Path,
        *,
        workspace: str | Path | None = None,
        machine: str | None = None,
        tos: str | Path | None = None,
        args: list[str] | None = None,
        wait: bool = True,
        enter_debugger: bool = False,
        debug_exceptions: str | None = None,
        timeout: float = 45.0,
    ) -> dict[str, Any]:
        """Load a PRG through a GEMDOS workspace and optional autostart."""
        source = Path(prg).expanduser().resolve()
        if not source.is_file():
            raise DebugSessionError(f"PRG does not exist: {source}")

        if workspace is not None:
            ws = Path(workspace).expanduser().resolve()
            try:
                relative = source.relative_to(ws)
            except ValueError as exc:
                raise DebugSessionError(
                    f"PRG is outside the requested workspace: {source}"
                ) from exc
        else:
            digest = hashlib.sha1(str(source).encode("utf-8")).hexdigest()[:8]
            ws = self.state_dir / "run" / f"prg-{digest}"
            ws.mkdir(parents=True, exist_ok=True)
            stage = paths.make_space_free(
                source, ws, mode="copy"
            )
            if paths.safe_component(source.name) != stage.name and stage.suffix.lower() != source.suffix.lower():
                adjusted = ws / f"{paths.safe_component(source.stem)}{source.suffix}"
                if stage != adjusted:
                    shutil.copy2(stage, adjusted)
                    stage = adjusted
            relative = stage.relative_to(ws)

        self._stop_existing()
        machine = (machine or self.machine or "st").lower()
        tos_path = Path(tos).expanduser() if tos else self.tos or paths.tos_for_machine(machine)
        launch = profile_args(machine)
        if debug_exceptions:
            launch += ["--debug-except", debug_exceptions]
        launch += list(args or [])
        self.client.call_tool(
            "emu_start",
            tos=str(tos_path) if tos_path else None,
            args=launch,
            workspace=str(ws),
            autostart=str(relative),
            display=False,
            # Pass explicitly for compatibility with older MCP servers whose
            # default ("off") is rejected by Hatari 2.6.
            gemdos_time="atari",
            timeout=max(60.0, timeout + 15.0),
        )
        self.current_prg = source
        status = self.status()
        if wait:
            status = self.wait_running(timeout=timeout)
        if enter_debugger:
            status = self.ensure_prompt(timeout=10.0)
        return status

    def wait_running(self, *, timeout: float = 45.0) -> dict[str, Any]:
        deadline = time.monotonic() + max(1.0, timeout)
        while time.monotonic() < deadline:
            status = self.status()
            state = str(status.get("state"))
            if state in ALIVE_STATES and state != "at_prompt":
                return status
            if state == "at_prompt":
                return status
            if state in TERMINAL_STATES:
                raise DebugSessionError(f"emulator stopped while waiting to run: {status}")
            time.sleep(self.poll_interval)
        raise DebugSessionError(f"emulator did not start within {timeout:.1f}s")

    def wait_prompt(self, *, timeout: float = 10.0) -> dict[str, Any]:
        deadline = time.monotonic() + max(0.2, timeout)
        while time.monotonic() < deadline:
            status = self.status()
            if status.get("at_prompt"):
                return status
            if str(status.get("state")) in TERMINAL_STATES:
                raise DebugSessionError(f"emulator exited before the prompt: {status}")
            time.sleep(self.poll_interval)
        raise DebugSessionError(f"debugger prompt did not appear within {timeout:.1f}s")

    def ensure_prompt(self, *, timeout: float = 10.0) -> dict[str, Any]:
        """Enter the debugger at the next instruction.

        A ``:once`` conditional breakpoint on ``pc > $0`` is true on the next
        emulated instruction, which is a reliable software break-in without a
        private control channel.
        """
        status = self.status()
        if status.get("at_prompt"):
            return status
        result = self.client.call_tool(
            "dbg_breakpoint_set",
            address="0",
            type="reg",
            condition="pc > $0",
            once=True,
            timeout=10.0,
        )
        try:
            return self.wait_prompt(timeout=timeout)
        except BaseException:
            identifier = _breakpoint_id(str(result.get("raw", "")))
            if identifier is not None:
                try:
                    self.client.call_tool(
                        "dbg_breakpoint_delete", index=identifier, timeout=5.0
                    )
                except BaseException:
                    pass
            raise

    def stop(self, *, force: bool = False) -> dict[str, Any]:
        try:
            return self.client.call_tool("emu_stop", force=force, timeout=20.0)
        except BaseException:
            try:
                return self.client.call_tool("emu_stop", force=True, timeout=20.0)
            except BaseException as exc:
                return {"ok": False, "error": str(exc)}

    # -- debugger reads ------------------------------------------------

    def registers(self) -> dict[str, Any]:
        return self.client.call_tool("dbg_registers_get", timeout=10.0)

    def memory_read(self, address: int | str, length: int) -> dict[str, Any]:
        return self.client.call_tool(
            "dbg_memory_read", address=address, length=length, timeout=15.0
        )

    def memory_write(self, address: int | str, data_hex: str) -> dict[str, Any]:
        return self.client.call_tool(
            "dbg_memory_write", address=address, data_hex=data_hex, timeout=15.0
        )

    def disassemble(self, address: int | str, count: int = 10) -> dict[str, Any]:
        text = hex(address) if isinstance(address, int) else str(address)
        return self.client.call_tool(
            "dbg_disassemble", address=text, count=count, timeout=10.0
        )

    def stack(self, words: int = 32, *, address: int | str | None = None) -> dict[str, Any]:
        args: dict[str, Any] = {"words": words}
        if address is not None:
            args["address"] = address
        return self.client.call_tool("dbg_stack_dump", timeout=15.0, **args)

    def exception(self) -> dict[str, Any]:
        return self.client.call_tool("dbg_exception_get", timeout=10.0)

    # -- breakpoints, stepping, state ----------------------------------

    def breakpoint_set(
        self,
        address: int | str,
        *,
        type: str = "exec",
        condition: str | None = None,
        once: bool = True,
        quiet: bool = False,
        count: int | None = None,
    ) -> dict[str, Any]:
        args: dict[str, Any] = {
            "address": hex(address) if isinstance(address, int) else str(address),
            "type": type,
            "once": once,
            "quiet": quiet,
        }
        if condition is not None:
            args["condition"] = condition
        if count is not None:
            args["count"] = count
        return self.client.call_tool("dbg_breakpoint_set", timeout=10.0, **args)

    def breakpoint_list(self) -> dict[str, Any]:
        return self.client.call_tool("dbg_breakpoint_list", timeout=10.0)

    def breakpoint_delete(self, index: int) -> dict[str, Any]:
        return self.client.call_tool("dbg_breakpoint_delete", index=index, timeout=10.0)

    def step(self, count: int = 1) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for _ in range(max(1, int(count))):
            result = self.client.call_tool("dbg_step", timeout=10.0)
        return result

    def step_over(self) -> dict[str, Any]:
        return self.client.call_tool("dbg_step_over", timeout=10.0)

    def continue_run(self) -> dict[str, Any]:
        return self.client.call_tool("dbg_continue", timeout=10.0)

    def snapshot(self, path: str | Path | None = None) -> dict[str, Any]:
        """Save emulator state plus a text snapshot of the debugger context."""
        if path is None:
            directory = self.state_dir / "snapshots"
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / f"snapshot-{int(time.time() * 1000)}.snap"
        target = Path(path).expanduser()
        saved = self.client.call_tool("dbg_state_save", path=str(target), timeout=30.0)
        context: dict[str, Any] = {"path": str(target), "saved": saved}
        for key, call in (
            ("registers", self.registers),
            ("exception", self.exception),
            ("status", self.status),
        ):
            try:
                context[key] = call()
            except BaseException as exc:
                context[key] = {"error": str(exc)}
        return context

    def load_snapshot(self, path: str | Path) -> dict[str, Any]:
        return self.client.call_tool("dbg_state_load", path=str(path), timeout=30.0)

    def __enter__(self) -> "DebugSession":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.stop()
