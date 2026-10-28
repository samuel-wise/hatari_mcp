"""Headless AHCC compilation through the proven standalone CLI script.

The MCP tool and ``scripts/ahcc-cli.py`` must not drift apart, so this module
runs the script as a child process instead of copying its Hatari/AHCC protocol
implementation.  It deliberately has no dependency on the server supervisor:
a compile gets its own short-lived Hatari instance.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
import sys
import time
from contextlib import suppress
from pathlib import Path
from typing import Any, Sequence

from ..errors import error

_TOS_CANDIDATES = (
    "Projects/Hatari/tos/emutos/etos192us.img",
    "Projects/Hatari/tos/tos3x/roms/tos306us.img",
    "Projects/Hatari/tos/tos1x/roms/tos104us.img",
)
_AHCC_RELATIVE = Path("mcp/project/tools/ahcc/ahcc-68000/AHCCST.BIN")
_SCRIPT_RELATIVE = Path("mcp/project/scripts/ahcc-cli.py")


def repo_root() -> Path:
    """Return the Hatari source/checkout root without importing server config."""
    env = os.environ.get("HATARI_MCP_REPO")
    if env:
        return Path(env).expanduser().resolve()
    for parent in Path(__file__).resolve().parents:
        if (parent / _SCRIPT_RELATIVE).is_file() or (parent / "src/sdl/main_sdl.c").is_file():
            return parent
    return Path.cwd().resolve()


def script_path() -> Path:
    return repo_root() / _SCRIPT_RELATIVE


def find_ahcc_dir() -> Path | None:
    env = os.environ.get("HATARI_AHCC_DIR")
    if env:
        candidate = Path(env).expanduser().resolve()
        if candidate.is_dir():
            return candidate
    candidate = repo_root() / _AHCC_RELATIVE
    if candidate.is_dir():
        return candidate.resolve()
    return None


def find_hatari() -> Path | None:
    env = os.environ.get("HATARI_BIN")
    if env:
        return Path(env).expanduser().resolve()
    repo = repo_root()
    for candidate in (repo / "build-install/bin/hatari", repo / "build/src/hatari"):
        if candidate.is_file():
            return candidate.resolve()
    which = shutil.which("hatari")
    return Path(which).resolve() if which else None


def find_tos() -> Path | None:
    env = os.environ.get("HATARI_TOS")
    if env:
        return Path(env).expanduser().resolve()
    home = Path.home()
    for relative in _TOS_CANDIDATES:
        candidate = home / relative
        if candidate.is_file():
            return candidate.resolve()
    return None


def gui_paths() -> dict[str, Any]:
    """Return AHCCST.PRG and the AHCCST.BIN directory when present."""
    ahcc_dir = find_ahcc_dir()
    prg = ahcc_dir / "AHCCST.PRG" if ahcc_dir else None
    prg_ok = bool(prg and prg.is_file())
    bin_ok = bool(ahcc_dir and ahcc_dir.is_dir())
    return {
        "prg": str(prg) if prg_ok else None,
        "bin": str(ahcc_dir) if bin_ok else None,
        "prg_available": prg_ok,
        "bin_available": bin_ok,
        "available": prg_ok and bin_ok,
    }


def discover_paths() -> dict[str, Any]:
    """Discover the standalone script, AHCC, Hatari, and a default TOS image."""
    repo = repo_root()
    script = script_path()
    ahcc_dir = find_ahcc_dir()
    hatari = find_hatari()
    tos = find_tos()
    cli = ahcc_dir / "AHCCST_P.TTP" if ahcc_dir else None
    prg = ahcc_dir / "AHCCST.PRG" if ahcc_dir else None
    prg_args = hatari.parent / "hatari-prg-args" if hatari else None

    script_ok = script.is_file()
    ahcc_ok = bool(ahcc_dir and cli and cli.is_file())
    hatari_ok = bool(hatari and hatari.is_file())
    prg_args_ok = bool(prg_args and prg_args.is_file())
    tos_ok = bool(tos and tos.is_file())
    return {
        "repo_root": str(repo),
        "script": str(script),
        "script_available": script_ok,
        "ahcc_dir": str(ahcc_dir) if ahcc_dir else None,
        "ahcc_cli": str(cli) if ahcc_ok else None,
        "ahcc_gui_prg": str(prg) if prg and prg.is_file() else None,
        "hatari": str(hatari) if hatari_ok else None,
        "hatari_prg_args": str(prg_args) if prg_args_ok else None,
        "tos": str(tos) if tos_ok else None,
        "compiler_available": script_ok and ahcc_ok and hatari_ok and prg_args_ok,
        "hatari_available": hatari_ok,
        "tos_available": tos_ok,
        "gui_available": bool(prg and prg.is_file() and ahcc_dir and ahcc_dir.is_dir()),
        "available": script_ok and ahcc_ok and hatari_ok and prg_args_ok and tos_ok,
    }


def _descendants(pid: int) -> list[int]:
    """Return all live descendants of *pid* using /proc (Linux only)."""
    proc_root = Path("/proc")
    if not proc_root.is_dir():
        return []
    parents: dict[int, int] = {}
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / "stat").read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        marker = stat.rfind(")")
        if marker < 0:
            continue
        fields = stat[marker + 2 :].split()
        if len(fields) < 2:
            continue
        try:
            parents[int(entry.name)] = int(fields[1])
        except ValueError:
            continue

    children: dict[int, list[int]] = {}
    for child, parent in parents.items():
        children.setdefault(parent, []).append(child)

    found: list[int] = []
    stack = list(children.get(pid, []))
    while stack:
        child = stack.pop()
        if child in found:
            continue
        found.append(child)
        stack.extend(children.get(child, []))
    return found


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


async def _terminate(proc: asyncio.subprocess.Process) -> None:
    """Terminate the script and every Hatari/helper descendant.

    ``scripts/ahcc-cli.py`` starts Hatari in a new session, so killing only the
    script's process group would leave the emulator orphaned.  Kill the whole
    descendant tree instead.
    """
    if proc.returncode is not None:
        return
    pids = _descendants(proc.pid)
    # Give the script a chance to run its own cleanup once the emulator dies.
    for pid in reversed(pids):
        with suppress(ProcessLookupError, PermissionError):
            os.kill(pid, signal.SIGTERM)
    try:
        await asyncio.wait_for(proc.wait(), 2)
    except asyncio.TimeoutError:
        with suppress(ProcessLookupError, PermissionError):
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        try:
            await asyncio.wait_for(proc.wait(), 2)
        except asyncio.TimeoutError:
            pass

    for pid in pids:
        if _alive(pid):
            with suppress(ProcessLookupError, PermissionError):
                os.kill(pid, signal.SIGKILL)
    if proc.returncode is None:
        with suppress(ProcessLookupError, PermissionError):
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        with suppress(Exception):
            await asyncio.wait_for(proc.wait(), 3)


async def _drain(stream: asyncio.StreamReader | None, sink: list[bytes]) -> None:
    if stream is None:
        return
    while True:
        chunk = await stream.read(65536)
        if not chunk:
            break
        sink.append(chunk)


def _build_argv(
    *,
    source: Path,
    output: Path,
    tos: str | Path | None,
    includes: Sequence[str | Path],
    defines: Sequence[str],
    cflags: Sequence[str],
    libs: Sequence[str],
    startup: str | None,
    timeout: float,
    keep: bool,
    extra_args: Sequence[str],
) -> list[str]:
    argv = [sys.executable, str(script_path()), str(source), "-o", str(output)]
    if tos:
        argv += ["--tos", str(tos)]
    for include in includes:
        argv += ["--include", str(include)]
    for define in defines:
        argv += ["--define", str(define)]
    for cflag in cflags:
        argv += ["--cflag", str(cflag)]
    for lib in libs:
        argv += ["--lib", str(lib)]
    if startup:
        argv += ["--startup", str(startup)]
    argv += ["--timeout", f"{timeout:g}"]
    if keep:
        argv.append("--keep")
    argv += [str(item) for item in extra_args]
    return argv


async def compile_c(
    source: str | Path,
    output: str | Path,
    *,
    tos: str | Path | None = None,
    includes: Sequence[str | Path] = (),
    defines: Sequence[str] = (),
    cflags: Sequence[str] = (),
    libs: Sequence[str] = (),
    startup: str | None = None,
    timeout: float = 120,
    keep: bool = False,
    extra_args: Sequence[str] = (),
) -> dict[str, Any]:
    """Compile one C file with AHCC in an isolated headless Hatari instance.

    The child is started in its own session so a timeout or cancellation kills
    the script and the Hatari instance it spawned.
    """
    source_path = Path(source).expanduser().resolve()
    output_path = Path(output).expanduser().resolve()
    script = script_path()

    if not script.is_file():
        raise error(
            "ahcc_unavailable",
            f"AHCC standalone script is missing: {script}",
            "Restore mcp/project/scripts/ahcc-cli.py or set HATARI_MCP_REPO.",
        )
    if not source_path.is_file():
        raise error("invalid_path", f"C source does not exist: {source_path}", "Check the source path.")
    if source_path.suffix.lower() != ".c":
        raise error("bad_option", f"Source is not a .c file: {source_path}", "Pass a C source file.")
    if timeout <= 0:
        raise error("bad_option", f"Timeout must be positive: {timeout}", "Use seconds, e.g. 120.")
    output_path.parent.mkdir(parents=True, exist_ok=True)

    argv = _build_argv(
        source=source_path,
        output=output_path,
        tos=tos,
        includes=includes,
        defines=defines,
        cflags=cflags,
        libs=libs,
        startup=startup,
        timeout=float(timeout),
        keep=keep,
        extra_args=extra_args,
    )

    started = time.monotonic()
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    stdout_chunks: list[bytes] = []
    stderr_chunks: list[bytes] = []
    out_task = asyncio.create_task(_drain(proc.stdout, stdout_chunks))
    err_task = asyncio.create_task(_drain(proc.stderr, stderr_chunks))
    timed_out = False
    try:
        try:
            await asyncio.wait_for(proc.wait(), float(timeout))
        except asyncio.TimeoutError:
            timed_out = True
            await _terminate(proc)
    except asyncio.CancelledError:
        await _terminate(proc)
        for task in (out_task, err_task):
            task.cancel()
        with suppress(Exception):
            await asyncio.gather(out_task, err_task, return_exceptions=True)
        raise
    finally:
        try:
            await asyncio.wait_for(asyncio.gather(out_task, err_task), 5)
        except asyncio.TimeoutError:
            for task in (out_task, err_task):
                task.cancel()
            with suppress(Exception):
                await asyncio.gather(out_task, err_task, return_exceptions=True)

    stdout = b"".join(stdout_chunks).decode("utf-8", "replace")
    stderr = b"".join(stderr_chunks).decode("utf-8", "replace")
    returncode = proc.returncode
    exists = output_path.is_file()
    size = output_path.stat().st_size if exists else 0
    kept_dir = None
    for line in stdout.splitlines():
        if line.startswith("kept:"):
            kept_dir = line.split(":", 1)[1].strip()

    ok = returncode == 0 and not timed_out and exists and size > 0
    result: dict[str, Any] = {
        "ok": ok,
        "returncode": returncode,
        "stdout": stdout,
        "stderr": stderr,
        "output": str(output_path),
        "size": size,
        "exists": exists,
        "timed_out": timed_out,
        "duration_ms": int((time.monotonic() - started) * 1000),
        "source": str(source_path),
        "tos": str(Path(tos).expanduser().resolve()) if tos else None,
        "kept": kept_dir,
    }
    if timed_out:
        result["error"] = "build_timeout"
        result["hint"] = f"AHCC compile did not finish within {float(timeout):g}s."
    elif returncode != 0:
        result["error"] = "build_failed"
        result["hint"] = "Inspect stdout/stderr for AHCC diagnostics."
    elif not exists or size == 0:
        result["error"] = "no_output"
        result["hint"] = "AHCC reported success but produced no executable."
    return result
