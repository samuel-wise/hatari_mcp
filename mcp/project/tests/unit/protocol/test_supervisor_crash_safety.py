"""Supervisor heartbeat, crash classification and bounded recovery."""

from __future__ import annotations

import asyncio
import ctypes
import os
import signal
import sys
import time
from pathlib import Path

import pytest

from hatari_mcp.config import Settings
from hatari_mcp.errors import HatariError
from hatari_mcp.state import State
from hatari_mcp.supervisor import (
    EXCEPTION_RE,
    RestartPolicy,
    Supervisor,
    _set_parent_death_signal,
    atomic_write_bytes,
)


def settings(tmp_path: Path) -> Settings:
    return Settings(
        repo_root=tmp_path,
        hatari_bin=None,
        default_tos=None,
        profiles_dir=tmp_path,
        runtime_root=tmp_path,
    )


class FakeProc:
    pid = 4242

    def __init__(self, exit_code: int | None = None) -> None:
        self.returncode = None
        self._exit_code = exit_code
        self.terminated = False

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15

    def kill(self) -> None:
        self.returncode = -9

    async def wait(self) -> int:
        if self._exit_code is not None and self.returncode is None:
            self.returncode = self._exit_code
        while self.returncode is None:
            await asyncio.sleep(0.01)
        return self.returncode


async def wait_until(predicate, timeout: float = 3.0) -> None:
    async def poll() -> None:
        while not predicate():
            await asyncio.sleep(0.01)

    await asyncio.wait_for(poll(), timeout)


def test_heartbeat_marks_unresponsive_after_threshold(tmp_path: Path) -> None:
    async def main() -> None:
        sup = Supervisor(
            settings(tmp_path),
            heartbeat_interval=0.01,
            heartbeat_probe_interval=0.01,
            heartbeat_timeout=0.01,
            heartbeat_misses=3,
        )
        sup.proc = FakeProc()
        sup.state = State.RUNNING

        async def down() -> bool:
            return False

        sup._probe_liveness = down
        task = asyncio.create_task(sup._heartbeat_loop())
        try:
            await wait_until(lambda: sup.state is State.UNRESPONSIVE)
            assert sup.heartbeat_misses == 3
            assert sup.unresponsive_since is not None
            assert sup.status()["unresponsive"] is True
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(main())


def test_heartbeat_recovers_miss_counter_on_pong(tmp_path: Path) -> None:
    async def main() -> None:
        sup = Supervisor(
            settings(tmp_path),
            heartbeat_interval=0.01,
            heartbeat_probe_interval=0.01,
            heartbeat_timeout=0.01,
            heartbeat_misses=3,
        )
        sup.proc = FakeProc()
        sup.state = State.RUNNING
        results = [False, False, True]

        async def probe() -> bool:
            return results.pop(0) if results else True

        sup._probe_liveness = probe
        task = asyncio.create_task(sup._heartbeat_loop())
        try:
            await wait_until(lambda: sup.last_pong is not None)
            assert sup.state is State.RUNNING
            assert sup.heartbeat_misses == 0
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(main())


def test_exit_classification(tmp_path: Path) -> None:
    sup = Supervisor(settings(tmp_path))
    sup.state = State.RUNNING
    assert sup._classify_exit(0) == "process_exit"
    assert sup._classify_exit(3) == "crash"
    assert sup._classify_exit(-9) == "host_kill"
    assert sup._classify_exit(-15) == "terminated"
    sup.program_exit_code = 0
    assert sup._classify_exit(0) == "guest_exit"
    sup.program_exit_code = None
    sup._requested_stop = True
    assert sup._classify_exit(3) == "requested_stop"


def test_requested_stop_never_schedules_restart(tmp_path: Path) -> None:
    sup = Supervisor(settings(tmp_path), restart_policy=RestartPolicy(enabled=True, backoff_seconds=(0,)))
    sup._requested_stop = True
    sup.exit_cause = "crash"
    sup._schedule_restart()
    assert sup._restart_task is None
    assert sup.restart_count == 0


def test_crash_loop_budget_is_bounded(tmp_path: Path) -> None:
    async def main() -> None:
        sup = Supervisor(
            settings(tmp_path),
            restart_policy=RestartPolicy(enabled=True, max_restarts=1, backoff_seconds=(0,)),
        )
        sup._launch_kwargs = {}
        sup.state = State.CRASHED
        sup.started_at = 1.0
        sup.exit_cause = "crash"
        sup._schedule_restart()
        assert sup._restart_task is not None
        sup._restart_task.cancel()
        await asyncio.gather(sup._restart_task, return_exceptions=True)
        sup._restart_task = None
        sup.exit_cause = "crash"
        sup._schedule_restart()
        assert sup.crash_loop is True
        assert sup.exit_cause == "crash_loop"
        assert sup._restart_task is None

    asyncio.run(main())


def test_bounded_restart_invokes_start_once(tmp_path: Path) -> None:
    async def main() -> None:
        sup = Supervisor(
            settings(tmp_path),
            restart_policy=RestartPolicy(enabled=True, max_restarts=1, backoff_seconds=(0,)),
        )
        calls = []

        async def fake_start(**kwargs):
            calls.append(kwargs)
            sup.state = State.RUNNING
            return sup.status()

        sup._launch_kwargs = {"tos": None}
        sup._start_locked = fake_start
        sup.state = State.CRASHED
        sup.started_at = 1.0
        sup.exit_cause = "crash"
        sup._schedule_restart()
        assert sup._restart_task is not None
        await sup._restart_task
        assert len(calls) == 1
        assert sup.restart_count == 1

    asyncio.run(main())


def test_atomic_state_write_and_mode(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "state.bin"
    atomic_write_bytes(target, b"snapshot", mode=0o600)
    assert target.read_bytes() == b"snapshot"
    assert (target.stat().st_mode & 0o777) == 0o600
    assert list(target.parent.glob(".state.bin.*.tmp")) == []


def test_autosave_replaces_target_atomically(tmp_path: Path) -> None:
    async def main() -> None:
        sup = Supervisor(settings(tmp_path))

        async def fake_debug(command, timeout_ms=None):
            path = Path(command.split('"')[1])
            path.write_bytes(b"state-data")
            return {"ok": True, "raw": ""}

        sup.debug = fake_debug
        target = tmp_path / "snapshots" / "state.bin"
        result = await sup.autosave_state(target)
        assert result["ok"] is True
        assert target.read_bytes() == b"state-data"
        assert list(target.parent.glob(".*.tmp")) == []

    asyncio.run(main())


def test_parent_death_helper_is_safe_to_call() -> None:
    _set_parent_death_signal()
    if os.name == "posix":
        assert callable(_set_parent_death_signal)


def test_unresponsive_is_not_a_one_way_door(tmp_path: Path) -> None:
    async def main() -> None:
        sup = Supervisor(
            settings(tmp_path),
            heartbeat_interval=0.01,
            heartbeat_probe_interval=0.01,
            heartbeat_timeout=0.01,
            heartbeat_misses=3,
        )
        sup.proc = FakeProc()
        sup.state = State.RUNNING
        results = [False, False, False, True]

        async def probe() -> bool:
            return results.pop(0) if results else True

        sup._probe_liveness = probe
        task = asyncio.create_task(sup._heartbeat_loop())
        try:
            await wait_until(lambda: sup.state is State.UNRESPONSIVE)
            await wait_until(lambda: sup.state is State.RUNNING)
            assert sup.heartbeat_misses == 0
            assert sup.unresponsive_since is None
            assert sup.last_pong is not None
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(main())


def test_skipped_probe_is_not_counted_as_pong_or_miss(tmp_path: Path) -> None:
    async def main() -> None:
        sup = Supervisor(
            settings(tmp_path),
            heartbeat_interval=0.01,
            heartbeat_probe_interval=0.01,
            heartbeat_timeout=0.01,
            heartbeat_misses=1,
        )
        sup.proc = FakeProc()
        sup.state = State.RUNNING

        async def skipped() -> None:
            return None

        sup._probe_liveness = skipped
        task = asyncio.create_task(sup._heartbeat_loop())
        try:
            await asyncio.sleep(0.08)
            assert sup.state is State.RUNNING
            assert sup.heartbeat_misses == 0
            assert sup.last_pong is None
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(main())


def test_invalid_protocol_env_never_spawns(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HATARI_MCP_PROTOCOL", "bogus")
    sup = Supervisor(settings(tmp_path))
    with pytest.raises(Exception) as excinfo:
        asyncio.run(sup.start())
    assert getattr(excinfo.value, "code", None) == "bad_option"
    assert sup.proc is None
    assert sup.runtime is None
    assert sup.state is State.STOPPED


def test_early_child_exit_fails_startup_without_full_timeout(tmp_path: Path) -> None:
    import stat
    import time

    script = tmp_path / "hatari-exit"
    script.write_text("#!/bin/sh\nexit 3\n")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    tos = tmp_path / "tos.img"
    tos.write_bytes(b"tos")
    cfg = Settings(
        repo_root=tmp_path,
        hatari_bin=script,
        default_tos=tos,
        profiles_dir=tmp_path,
        runtime_root=tmp_path,
    )

    async def main() -> None:
        sup = Supervisor(cfg, heartbeat_interval=0.05, heartbeat_probe_interval=0.05)
        started = time.monotonic()
        with pytest.raises(HatariError) as excinfo:
            await sup.start(startup_timeout=10)
        elapsed = time.monotonic() - started
        assert excinfo.value.code == "startup_failed"
        assert elapsed < 5
        assert sup.state in (State.CRASHED, State.FAILED)
        assert sup.proc is not None and sup.proc.returncode == 3
        await sup.shutdown()

    asyncio.run(main())


def test_stop_proc_less_starting_state_becomes_terminal(tmp_path: Path) -> None:
    async def main() -> None:
        sup = Supervisor(settings(tmp_path))
        sup.state = State.STARTING
        result = await sup.stop()
        assert result["state"] == "stopped"
        assert sup.runtime is None

    asyncio.run(main())


def test_forged_banner_and_natfeats_state_are_not_trusted(tmp_path: Path) -> None:
    sup = Supervisor(settings(tmp_path))
    sup.state = State.STARTING
    sup._banner_nonce = "goodnonce"
    sup._banner_window_open = True

    sup._ingest_stderr("HATARI-MCP-PROTOCOL 1 capabilities=framed nonce=wrong")
    assert sup.protocol_banner is None
    assert sup.protocol_banner_error

    # A new launch gets a new nonce/first-banner window.
    sup._banner_seen = False
    sup.protocol_banner_error = None
    sup._ingest_stderr("HATARI-MCP-PROTOCOL 1 capabilities=framed nonce=goodnonce")
    assert sup.protocol_banner is not None

    # Only the first banner is considered.
    first_raw = sup.protocol_banner_raw
    sup._ingest_stderr("HATARI-MCP-PROTOCOL 1 capabilities=framed nonce=goodnonce")
    assert sup.protocol_banner_raw == first_raw

    sup.state = State.RUNNING
    sup._natfeats_enabled = True
    sup._ingest_stderr("You have entered debug mode")
    assert sup.state is State.RUNNING
    sup._natfeats_enabled = False
    sup._ingest_stderr("You have entered debug mode")
    assert sup.state is State.AT_PROMPT


def test_legacy_probe_can_observe_unresponsive_recovery(tmp_path: Path) -> None:
    async def main() -> None:
        sup = Supervisor(settings(tmp_path))
        sup.proc = FakeProc()
        sup.state = State.UNRESPONSIVE
        sup._socket_writer = object()

        async def healthy(timeout: float) -> bool:
            return True

        sup._probe_socket_echo = healthy
        assert await sup._probe_legacy_liveness() is True

    asyncio.run(main())


def test_parent_death_signal_uses_sigkill(monkeypatch) -> None:
    calls: list[tuple] = []

    class FakeLibc:
        def prctl(self, *args) -> int:
            calls.append(args)
            return 0

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(ctypes, "CDLL", lambda *args, **kwargs: FakeLibc())
    _set_parent_death_signal()
    assert calls
    assert calls[0][1] == int(signal.SIGKILL)


def test_late_returning_to_emulation_cannot_revive_stopping_or_terminal(tmp_path: Path) -> None:
    sup = Supervisor(settings(tmp_path))
    sup.state = State.STOPPING
    sup._stopping = True
    sup._ingest_stderr("Returning to emulation")
    assert sup.state is State.STOPPING

    sup.state = State.EXITED
    sup._stopping = False
    sup._ingest_stderr("Returning to emulation")
    assert sup.state is State.EXITED

    sup.state = State.CRASHED
    sup._ingest_stderr("Returning to emulation")
    assert sup.state is State.CRASHED


def test_requested_stop_is_clean_even_with_nonzero_exit(tmp_path: Path) -> None:
    async def main() -> None:
        sup = Supervisor(settings(tmp_path))
        sup.proc = FakeProc(exit_code=3)
        sup.state = State.RUNNING
        sup._requested_stop = True
        sup._stopping = True
        await sup._wait_child()
        assert sup.state is State.EXITED
        assert sup.exit_cause == "requested_stop"
        assert sup.last_error is None
        await asyncio.sleep(0.25)

    asyncio.run(main())


def test_requested_stop_returns_exited_zero(tmp_path: Path) -> None:
    async def main() -> None:
        sup = Supervisor(settings(tmp_path))
        sup.proc = FakeProc(exit_code=0)
        sup.state = State.RUNNING
        result = await sup.stop(force=True)
        assert result["state"] == "exited"
        assert result["returncode"] == 0
        assert result["last_error"] is None

    asyncio.run(main())


def test_invalid_banner_does_not_consume_the_window(tmp_path: Path) -> None:
    sup = Supervisor(settings(tmp_path))
    sup.state = State.STARTING
    sup._banner_nonce = "goodnonce"
    sup._banner_window_open = True
    sup._banner_deadline = time.monotonic() + 5

    sup._ingest_stderr("HATARI-MCP-PROTOCOL 1 capabilities=framed nonce=wrong")
    assert sup.protocol_banner is None
    assert sup.protocol_banner_error
    assert sup._banner_seen is False

    sup._ingest_stderr("HATARI-MCP-PROTOCOL 1 capabilities=framed nonce=goodnonce")
    assert sup.protocol_banner is not None
    assert sup.protocol_banner_error is None
    assert sup._banner_seen is True


def test_delayed_banner_is_accepted_while_running_inside_window(tmp_path: Path) -> None:
    sup = Supervisor(settings(tmp_path))
    sup.state = State.RUNNING
    sup._banner_nonce = "goodnonce"
    sup._banner_window_open = True
    sup._banner_deadline = time.monotonic() + 5

    sup._ingest_stderr("HATARI-MCP-PROTOCOL 1 capabilities=framed nonce=goodnonce")
    assert sup.protocol_banner is not None
    assert sup.protocol_banner_error is None


def test_banner_after_window_is_diagnosed_not_accepted(tmp_path: Path) -> None:
    sup = Supervisor(settings(tmp_path))
    sup.state = State.RUNNING
    sup._banner_nonce = "goodnonce"
    sup._banner_window_open = True
    sup._banner_deadline = time.monotonic() - 1

    sup._ingest_stderr("HATARI-MCP-PROTOCOL 1 capabilities=framed nonce=goodnonce")
    assert sup.protocol_banner is None
    assert sup.protocol_banner_error and "window" in sup.protocol_banner_error
    assert sup._banner_seen is False


def test_child_exit_after_socket_connect_is_not_reported_running(tmp_path: Path) -> None:
    script = tmp_path / "fake-hatari.py"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import socket, sys, time\n"
        "args = sys.argv\n"
        "path = args[args.index('--control-socket') + 1]\n"
        "sock = socket.socket(socket.AF_UNIX)\n"
        "deadline = time.time() + 5\n"
        "while time.time() < deadline:\n"
        "    try:\n"
        "        sock.connect(path)\n"
        "        break\n"
        "    except (FileNotFoundError, ConnectionRefusedError):\n"
        "        time.sleep(0.01)\n"
        "sock.close()\n"
        "sys.exit(3)\n"
    )
    script.chmod(0o755)
    tos = tmp_path / "tos.img"
    tos.write_bytes(b"tos")
    cfg = Settings(
        repo_root=tmp_path,
        hatari_bin=script,
        default_tos=tos,
        profiles_dir=tmp_path,
        runtime_root=tmp_path,
    )

    async def main() -> None:
        sup = Supervisor(cfg, heartbeat_interval=0.05, heartbeat_probe_interval=0.05)

        async def wait_until_reaped(self) -> None:
            deadline = time.monotonic() + 5
            while (
                self.proc is not None
                and self.proc.returncode is None
                and time.monotonic() < deadline
            ):
                await asyncio.sleep(0.01)

        sup._enforce_protocol_mode = wait_until_reaped.__get__(sup, Supervisor)
        with pytest.raises(HatariError) as excinfo:
            await sup.start(startup_timeout=10)
        assert excinfo.value.code == "startup_failed"
        assert sup.state is not State.RUNNING
        await sup.shutdown()

    asyncio.run(main())


def test_exception_regex_matches_hatari_bus_error_reading() -> None:
    """Hatari's memory-access warning must be classified as an exception."""
    match = EXCEPTION_RE.search(
        "WARN : Bus Error reading at address $ffff8240, PC=$1382a"
    )
    assert match is not None
    assert match.group(1).lower() == "bus error"

    match = EXCEPTION_RE.search("Bus error exception at 0x0001382a!")
    assert match is not None
    assert match.group(1).lower() == "bus error"
