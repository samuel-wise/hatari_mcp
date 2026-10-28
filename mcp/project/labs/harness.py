"""Minimal stock-Hatari harness for the P8 spikes.

The harness deliberately avoids production ``hatari_mcp`` code. It launches one
headless Hatari process, owns its control socket and pipes, and uses the same
stderr sentinel convention as the server so measurements reflect the stock
control path.
"""

from __future__ import annotations

import hashlib
import os
import queue
import re
import resource
import shutil
import signal
import socket
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import require_labs

require_labs()

LABS_DIR = Path(__file__).resolve().parent
PROJECT_DIR = LABS_DIR.parent
REPO_DIR = PROJECT_DIR.parent.parent

CONFIG_TEXT = """[Log]
nTextLogLevel = 3
nAlertDlgLogLevel = 1
bConfirmQuit = FALSE
bConsoleWindow = FALSE

[Debugger]
nNumberBase = 16
nSymbolLines = 100000
nMemdumpLines = 100000
nFindLines = 100000
nDisasmLines = 100000
nBacktraceLines = 1000
nDisasmOptions = 103
"""

TOS_DIR = Path(os.environ.get("HATARI_TOS_DIR", str(Path.home() / "Projects/Hatari/tos"))).expanduser()

PROFILES: dict[str, dict[str, Any]] = {
    "st": {
        "machine": "st",
        "memsize": "1",
        "tos": TOS_DIR / "emutos/etos192us.img",
        "extra": [],
    },
    "ste": {
        "machine": "ste",
        "memsize": "2",
        "tos": TOS_DIR / "tos1x/roms/tos162us.img",
        "extra": ["--blitter", "on"],
    },
    "falcon": {
        "machine": "falcon",
        "memsize": "4",
        "tos": TOS_DIR / "tos3x/roms/tos404.img",
        "extra": ["--dsp", "emu"],
    },
}

VALUE_HEX_RE = re.compile(r"\$([0-9a-fA-F]+)\s*\(hex\)")


def find_hatari() -> Path:
    env = os.environ.get("HATARI_BIN")
    candidates = [
        Path(env).expanduser() if env else None,
        REPO_DIR / "build-install/bin/hatari",
        REPO_DIR / "build/src/hatari",
    ]
    for candidate in candidates:
        if candidate and candidate.is_file():
            return candidate.resolve()
    raise FileNotFoundError("Hatari binary not found; set HATARI_BIN")


def profile_config(name: str, *, tos: Path | None = None) -> dict[str, Any]:
    if name not in PROFILES:
        raise ValueError(f"unknown profile {name!r}; choose {sorted(PROFILES)}")
    config = dict(PROFILES[name])
    config["name"] = name
    if tos is not None:
        config["tos"] = tos
    tos_path = Path(config["tos"]).expanduser()
    if not tos_path.is_file():
        raise FileNotFoundError(f"TOS/EmuTOS image missing for {name}: {tos_path}")
    config["tos"] = tos_path.resolve()
    return config


def sha256_file(path: Path, limit: int | None = None) -> str:
    digest = hashlib.sha256()
    remaining = limit
    with path.open("rb") as handle:
        while True:
            size = 1 << 20 if remaining is None else min(1 << 20, remaining)
            if size <= 0:
                break
            chunk = handle.read(size)
            if not chunk:
                break
            digest.update(chunk)
            if remaining is not None:
                remaining -= len(chunk)
    return digest.hexdigest()


def write_json(path: Path, data: Any) -> None:
    import json

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def make_out_dir(label: str) -> Path:
    base = Path(tempfile.gettempdir()) / f"hatari-mcp-p8-{label}"
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    path = base / stamp
    path.mkdir(parents=True, exist_ok=True)
    return path


@dataclass
class RunMetrics:
    profile: str
    connect_s: float
    total_s: float
    peak_rss_kb: int
    returncode: int | None
    cpu_s: float = 0.0
    command_ms: dict[str, list[float]] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "profile": self.profile,
            "connect_s": self.connect_s,
            "total_s": self.total_s,
            "peak_rss_kb": self.peak_rss_kb,
            "returncode": self.returncode,
            "cpu_s": self.cpu_s,
            "command_ms": self.command_ms,
        }


class Hatari:
    def __init__(
        self,
        profile: str = "st",
        *,
        tos: Path | None = None,
        extra_args: list[str] | None = None,
        runtime_root: Path | None = None,
    ):
        self.config = profile_config(profile, tos=tos)
        self.profile = profile
        self.extra_args = list(extra_args or [])
        self.runtime_root = runtime_root or Path(tempfile.gettempdir())
        self.runtime: Path | None = None
        self.socket_path: Path | None = None
        self.config_path: Path | None = None
        self.proc: subprocess.Popen[bytes] | None = None
        self.conn: socket.socket | None = None
        self.server: socket.socket | None = None
        self.spawn_s = 0.0
        self.connect_s = 0.0
        self._queue: queue.Queue[str] = queue.Queue()
        self._lines: list[str] = []
        self._threads: list[threading.Thread] = []
        self._closed = False

    def __enter__(self) -> "Hatari":
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()

    @property
    def hatari(self) -> Path:
        return find_hatari()

    def _prepare_runtime(self) -> None:
        self.runtime = Path(tempfile.mkdtemp(prefix="hmcp-p8-", dir=str(self.runtime_root)))
        os.chmod(self.runtime, 0o700)
        self.socket_path = self.runtime / "control.sock"
        self.config_path = self.runtime / "hatari.cfg"
        self.config_path.write_text(CONFIG_TEXT, encoding="utf-8")

    def _base_args(self) -> list[str]:
        args = [
            str(self.hatari),
            "--control-socket",
            str(self.socket_path),
            "--configfile",
            str(self.config_path),
            "--machine",
            str(self.config["machine"]),
            "--memsize",
            str(self.config["memsize"]),
            "--tos",
            str(self.config["tos"]),
            "--sound",
            "off",
            "--confirm-quit",
            "off",
            "--statusbar",
            "off",
            "--drive-led",
            "off",
            "--fast-boot",
            "on",
            "--timer-d",
            "on",
            "--rtc-year",
            "1990",
            "--log-level",
            "info",
        ]
        args.extend(self.config.get("extra", []))
        args.extend(self.extra_args)
        return args

    def start(
        self,
        *,
        timeout: float = 30.0,
        run_vbls: int | None = None,
        benchmark: bool = False,
        trace_flags: str | None = None,
        trace_file: Path | None = None,
        parse_file: Path | None = None,
        harddrive: Path | None = None,
        autostart: str | None = None,
    ) -> dict[str, Any]:
        self._prepare_runtime()
        assert self.socket_path and self.config_path
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(str(self.socket_path))
        self.server.listen(1)
        self.server.settimeout(timeout)

        args = self._base_args()
        if run_vbls is not None:
            args.extend(["--run-vbls", str(run_vbls)])
        if benchmark:
            args.append("--benchmark")
        if trace_flags:
            args.extend(["--trace", trace_flags])
        if trace_file:
            args.extend(["--trace-file", str(trace_file)])
        if parse_file:
            args.extend(["--parse", str(parse_file)])
        if harddrive:
            args.extend(["--harddrive", str(harddrive), "--gemdos-drive", "C"])
            if autostart:
                args.extend(["--auto", autostart])

        env = os.environ.copy()
        env["SDL_VIDEODRIVER"] = "dummy"
        env["SDL_AUDIODRIVER"] = "dummy"
        env["HATARI_MCP"] = "1"
        self.spawn_s = time.perf_counter()
        self.proc = subprocess.Popen(
            args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            start_new_session=True,
        )
        self._start_readers()
        try:
            self.conn, _ = self.server.accept()
            self.conn.settimeout(1.0)
        except (TimeoutError, OSError) as exc:
            if self.proc.poll() is None:
                self.stop()
            raise RuntimeError(f"Hatari did not connect to the control socket: {exc}") from exc
        self.connect_s = time.perf_counter() - self.spawn_s
        return {"connect_s": self.connect_s, "pid": self.proc.pid}

    def _start_readers(self) -> None:
        assert self.proc and self.proc.stdout and self.proc.stderr
        stderr_thread = threading.Thread(target=self._read_stderr, daemon=True)
        stdout_thread = threading.Thread(target=self._read_stdout, daemon=True)
        stderr_thread.start()
        stdout_thread.start()
        self._threads.extend([stderr_thread, stdout_thread])

    def _read_stdout(self) -> None:
        assert self.proc and self.proc.stdout
        while True:
            try:
                chunk = self.proc.stdout.read1(4096)
            except (OSError, ValueError):
                break
            if not chunk:
                break

    def _read_stderr(self) -> None:
        assert self.proc and self.proc.stderr
        pending = ""
        while True:
            try:
                chunk = self.proc.stderr.read1(4096)
            except (OSError, ValueError):
                break
            if not chunk:
                break
            pending += chunk.decode("utf-8", "replace")
            while "\n" in pending:
                line, pending = pending.split("\n", 1)
                line = line.rstrip("\r")
                self._lines.append(line)
                self._queue.put(line)
        if pending:
            self._lines.append(pending.rstrip("\r"))
            self._queue.put(pending.rstrip("\r"))

    def drain(self) -> None:
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break

    def _drain(self) -> None:
        self.drain()

    def wait_for_line(self, needle: str, *, timeout: float = 15.0) -> str:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if self.proc and self.proc.poll() is not None and self._queue.empty():
                break
            try:
                line = self._queue.get(timeout=0.1)
            except queue.Empty:
                continue
            if needle in line:
                return line
        raise TimeoutError(f"stderr line not seen: {needle}")

    def command(self, command: str, *, timeout: float = 5.0) -> list[str]:
        if not self.conn:
            raise RuntimeError("control socket is not connected")
        self._drain()
        token = f"__P8_{os.getpid()}_{time.time_ns()}__"
        payload = f"hatari-debug {command}\nhatari-debug echo {token}\n"
        self.conn.sendall(payload.encode("latin-1"))
        end = time.monotonic() + timeout
        lines: list[str] = []
        while time.monotonic() < end:
            if self.proc and self.proc.poll() is not None and self._queue.empty():
                break
            try:
                line = self._queue.get(timeout=0.1)
            except queue.Empty:
                continue
            if token in line:
                return lines
            lines.append(line)
        raise TimeoutError(f"command timed out: {command}; partial={lines[-5:]}")

    def send_terminal(self, command: str) -> None:
        if not self.conn:
            raise RuntimeError("control socket is not connected")
        self.conn.sendall(f"hatari-debug {command}\n".encode("latin-1"))

    def send_control(self, command: str) -> None:
        if not self.conn:
            raise RuntimeError("control socket is not connected")
        self.conn.sendall((command + "\n").encode("latin-1"))

    def send_stdin(self, command: str) -> None:
        if not self.proc or not self.proc.stdin:
            raise RuntimeError("stdin pipe is not available")
        self.proc.stdin.write((command + "\n").encode("latin-1"))
        self.proc.stdin.flush()

    def eval_value(self, expression: str, *, timeout: float = 5.0) -> int:
        lines = self.command(f"e {expression}", timeout=timeout)
        match = VALUE_HEX_RE.search("\n".join(lines))
        if not match:
            raise ValueError(f"could not parse evaluate output: {lines[-4:]}")
        return int(match.group(1), 16)

    def vbl(self) -> int:
        return self.eval_value("($462).l")

    def wait_vbl(self, target: int, *, timeout: float = 60.0, poll_s: float = 0.02) -> int:
        end = time.monotonic() + timeout
        last = 0
        while time.monotonic() < end:
            last = self.vbl()
            if last >= target:
                return last
            time.sleep(poll_s)
        raise TimeoutError(f"VBL {target} not reached; last={last}")

    def screenshot(self, path: Path) -> float:
        start = time.perf_counter()
        self.command(f'screenshot "{path}"', timeout=10.0)
        elapsed = time.perf_counter() - start
        if not path.is_file():
            raise FileNotFoundError(f"screenshot was not written: {path}")
        return elapsed

    def statesave(self, path: Path) -> float:
        start = time.perf_counter()
        self.command(f'statesave "{path}"', timeout=30.0)
        elapsed = time.perf_counter() - start
        if not path.is_file():
            raise FileNotFoundError(f"state was not written: {path}")
        return elapsed

    def stateload(self, path: Path) -> float:
        start = time.perf_counter()
        self.command(f'stateload "{path}"', timeout=30.0)
        return time.perf_counter() - start

    def stateload_observable(
        self, path: Path, *, expected_vbl: int | None = None, timeout: float = 10.0
    ) -> dict[str, float | int | None]:
        """Load a state and time until the restored VBL is observable.

        The command round-trip only proves that the debugger echoed the
        command. When ``expected_vbl`` is known, poll the guest VBL counter
        until it reflects the loaded snapshot (within a small window, because
        emulation continues while the debugger answers). ``observable_s`` is
        the honest restore-completion latency; ``command_s`` is the round-trip.
        """
        start = time.perf_counter()
        self.command(f'stateload "{path}"', timeout=30.0)
        command_s = time.perf_counter() - start
        restored = None
        observable_s = command_s
        if expected_vbl is not None:
            end = time.monotonic() + timeout
            while time.monotonic() < end:
                current = self.vbl()
                restored = current
                if current >= expected_vbl and current <= expected_vbl + 10:
                    observable_s = time.perf_counter() - start
                    break
                time.sleep(0.001)
        return {
            "command_s": command_s,
            "observable_s": observable_s,
            "expected_vbl": expected_vbl,
            "restored_vbl": restored,
        }

    def guest_cpu_time_s(self) -> float:
        """Host CPU seconds consumed by the emulator process so far.

        Reading ``/proc/<pid>/stat`` and differencing two samples measures the
        host CPU cost of an explicit entry->exit window rather than the
        boot-dominated wall time of the whole process.
        """
        if not self.proc:
            return 0.0
        try:
            fields = Path(f"/proc/{self.proc.pid}/stat").read_text(encoding="utf-8").split()
            # fields[13] and [14] are utime/stime in clock ticks (1-based split).
            ticks = int(fields[13]) + int(fields[14])
        except (OSError, IndexError, ValueError):
            return 0.0
        try:
            hz = os.sysconf("SC_CLK_TCK")
        except (ValueError, OSError):
            hz = 100
        return ticks / hz

    def savebin(self, path: Path, address: int, size: int) -> None:
        self.command(f"savebin {path} ${address:x} {size}", timeout=10.0)
        if not path.is_file():
            raise FileNotFoundError(f"memory dump was not written: {path}")

    def peak_rss_kb(self) -> int:
        if not self.proc:
            return 0
        try:
            text = Path(f"/proc/{self.proc.pid}/status").read_text(encoding="utf-8")
        except OSError:
            return 0
        match = re.search(r"VmHWM:\s*(\d+)\s*kB", text)
        return int(match.group(1)) if match else 0

    def wait(self, timeout: float = 60.0) -> int:
        if not self.proc:
            raise RuntimeError("Hatari has not been started")
        return self.proc.wait(timeout=timeout)

    def poll(self) -> int | None:
        return self.proc.poll() if self.proc else None

    def stop(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self.proc and self.proc.poll() is None:
            if self.conn:
                try:
                    self.conn.sendall(b"hatari-debug quit\n")
                except OSError:
                    pass
            if self.proc.stdin:
                try:
                    self.proc.stdin.write(b"quit\n")
                    self.proc.stdin.flush()
                except (OSError, ValueError):
                    pass
            try:
                self.proc.wait(timeout=3.0)
            except subprocess.TimeoutExpired:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=3.0)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
                    self.proc.wait(timeout=3.0)
        if self.conn:
            try:
                self.conn.close()
            except OSError:
                pass
            self.conn = None
        if self.server:
            try:
                self.server.close()
            except OSError:
                pass
            self.server = None
        if self.runtime:
            shutil.rmtree(self.runtime, ignore_errors=True)
            self.runtime = None


def _rss_monitor(proc: subprocess.Popen[bytes], result: dict[str, int], stop: threading.Event) -> None:
    peak = 0
    while not stop.is_set() and proc.poll() is None:
        try:
            text = Path(f"/proc/{proc.pid}/status").read_text(encoding="utf-8")
        except OSError:
            break
        match = re.search(r"VmHWM:\s*(\d+)\s*kB", text)
        if match:
            peak = max(peak, int(match.group(1)))
        time.sleep(0.005)
    result["peak_rss_kb"] = max(peak, result.get("peak_rss_kb", 0))


def _duty_pause(proc: subprocess.Popen[bytes], stop: threading.Event, pause_ms: float, period_ms: float) -> None:
    while not stop.wait(period_ms / 1000.0):
        if proc.poll() is not None:
            return
        try:
            os.kill(proc.pid, signal.SIGSTOP)
        except ProcessLookupError:
            return
        stop.wait(pause_ms / 1000.0)
        try:
            os.kill(proc.pid, signal.SIGCONT)
        except ProcessLookupError:
            return


def run_fixed_workload(
    profile: str = "st",
    *,
    run_vbls: int = 500,
    benchmark: bool = True,
    trace_flags: str | None = None,
    trace_file: Path | None = None,
    harddrive: Path | None = None,
    autostart: str | None = None,
    extra_args: list[str] | None = None,
    pause_duty: tuple[float, float] | None = None,
) -> RunMetrics:
    runner = Hatari(profile, extra_args=extra_args)
    start = time.perf_counter()
    runner.start(
        run_vbls=run_vbls,
        benchmark=benchmark,
        trace_flags=trace_flags,
        trace_file=trace_file,
        harddrive=harddrive,
        autostart=autostart,
    )
    rss: dict[str, int] = {"peak_rss_kb": runner.peak_rss_kb()}
    stop = threading.Event()
    rss_thread = threading.Thread(target=_rss_monitor, args=(runner.proc, rss, stop), daemon=True)
    rss_thread.start()
    duty_thread = None
    if pause_duty:
        duty_thread = threading.Thread(
            target=_duty_pause, args=(runner.proc, stop, pause_duty[0], pause_duty[1]), daemon=True
        )
        duty_thread.start()
    usage_before = resource.getrusage(resource.RUSAGE_CHILDREN)
    returncode = runner.wait(timeout=max(30.0, run_vbls / 20.0))
    usage_after = resource.getrusage(resource.RUSAGE_CHILDREN)
    cpu_s = (usage_after.ru_utime + usage_after.ru_stime) - (
        usage_before.ru_utime + usage_before.ru_stime
    )
    total = time.perf_counter() - start
    stop.set()
    peak = rss.get("peak_rss_kb", 0) or runner.peak_rss_kb()
    connect = runner.connect_s
    runner.stop()
    return RunMetrics(
        profile=profile,
        connect_s=connect,
        total_s=total,
        peak_rss_kb=peak,
        returncode=returncode,
        cpu_s=cpu_s,
    )
