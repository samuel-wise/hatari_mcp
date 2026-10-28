"""AHCC compile/build tools and GEMDOS program launch."""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any, Sequence

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from ..app import App
from ..errors import error
from ..services import ahcc
from ..state import ALIVE
from ._common import guarded

_CANDIDATE_SUFFIXES = {".prg", ".tos"}
_SCAN_LIMIT = 50_000
_CANDIDATE_LIMIT = 100
_CLEANUP_TASKS: set[asyncio.Task[Any]] = set()


def guest_relative(workspace: str | Path, program: str | Path) -> str:
    """Return a workspace-relative Windows-style guest path component."""
    ws = Path(workspace).expanduser().resolve()
    prg = Path(program).expanduser().resolve()
    try:
        relative = prg.relative_to(ws)
    except ValueError:
        raise error(
            "path_not_allowed",
            f"Program is outside the workspace: {prg}",
            "Pass workspace=<host dir> containing the program, or omit workspace to use its parent.",
        ) from None
    if not relative.parts:
        raise error("bad_option", "The program path must name a file inside the workspace.", "Check the path.")
    return "\\".join(relative.parts)


def guest_path(workspace: str | Path, program: str | Path, drive: str = "C") -> str:
    """Translate a host program path into a GEMDOS guest path."""
    letter = drive.strip().upper()
    if len(letter) != 1 or letter not in "CDEFGHIJKLMNOPQRSTUVWXYZ":
        raise error("bad_option", f"Invalid GEMDOS drive: {drive!r}", "Use C-Z.")
    return f"{letter}:\\{guest_relative(workspace, program)}"


def is_writable_location(path: str | Path) -> bool:
    """True only for the repository's mcp/ tree and the system temp root."""
    resolved = Path(path).expanduser().resolve()
    repo = ahcc.repo_root()
    mcp_root = (repo / "mcp").resolve()
    temp_root = Path(tempfile.gettempdir()).resolve()
    return (
        resolved == mcp_root
        or mcp_root in resolved.parents
        or resolved == temp_root
        or temp_root in resolved.parents
    )


def _scan_files(root: Path, limit: int = _SCAN_LIMIT) -> tuple[list[Path], bool]:
    files: list[Path] = []
    truncated = False
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(name for name in dirnames if not (Path(dirpath) / name).is_symlink())
        for name in sorted(filenames):
            path = Path(dirpath) / name
            if path.is_symlink():
                continue
            if len(files) >= limit:
                truncated = True
                break
            files.append(path)
        if truncated:
            break
    return files, truncated


def candidate_programs(root: str | Path, limit: int = _CANDIDATE_LIMIT) -> list[Path]:
    """Return bounded, sorted .PRG/.TOS candidates without following symlinks."""
    base = Path(root).expanduser().resolve()
    files, _ = _scan_files(base)
    matches = [path for path in files if path.suffix.lower() in _CANDIDATE_SUFFIXES]
    return sorted(matches, key=lambda path: str(path).lower())[: max(1, limit)]


def _default_output(source: Path) -> Path:
    env_dir = os.environ.get("HATARI_MCP_BUILD_DIR")
    base = Path(env_dir).expanduser() if env_dir else Path(tempfile.gettempdir()) / "hatari-mcp-build"
    base = base.resolve()
    if not is_writable_location(base):
        raise error(
            "path_not_allowed",
            f"Default build directory is not writable: {base}",
            "Set HATARI_MCP_BUILD_DIR to a path under mcp/ or /tmp.",
        )
    base.mkdir(parents=True, exist_ok=True)
    return base / f"{source.stem}.TOS"


def _prepare_guest_args(
    program: Path,
    args: Sequence[str],
) -> tuple[Path, Path]:
    """Create the basepage/args parse scripts used by hatari-prg-args."""
    work = Path(tempfile.mkdtemp(prefix="hatari-mcp-args-"))
    prefix = program.parent

    converted: list[str] = []
    try:
        for arg in args:
            text = str(arg)
            candidate = Path(text).expanduser()
            try:
                is_file = candidate.is_file()
            except OSError:
                is_file = False
            if is_file and candidate.is_absolute():
                resolved = candidate.resolve()
                try:
                    relative = resolved.relative_to(prefix)
                except ValueError:
                    converted.append(text)
                else:
                    guest = "C:\\" + "\\".join(relative.parts)
                    converted.append(guest.upper())
            else:
                converted.append(text)
        payload = " ".join(converted).encode("latin-1", "replace")
        if len(payload) > 126:
            raise error(
                "bad_option",
                f"Guest argument list is too long ({len(payload)} bytes).",
                "The Atari basepage limit is 126 characters.",
            )
        args_file = work / "args"
        args_file.write_bytes(payload + b"\0")

        basepage_file = work / "basepage.ini"
        basepage_file.write_text(
            "setopt dec\n"
            f"w 'basepage+0x80' {len(payload)}\n"
            f"l {args_file} 'basepage+0x81'\n",
            encoding="latin-1",
        )
        start_file = work / "start.ini"
        start_file.write_text(
            f"b pc = text :once :trace :quiet :file {basepage_file}\n",
            encoding="latin-1",
        )
    except Exception:
        shutil.rmtree(work, ignore_errors=True)
        raise
    return work, start_file


def _schedule_cleanup(app: App, path: Path) -> None:
    async def cleanup() -> None:
        while app.supervisor.state in ALIVE:
            await asyncio.sleep(1)
        shutil.rmtree(path, ignore_errors=True)

    task = asyncio.create_task(cleanup())
    _CLEANUP_TASKS.add(task)
    task.add_done_callback(_CLEANUP_TASKS.discard)


def _tail_text(entries: Sequence[Any], kinds: set[str] | None = None) -> str:
    return "\n".join(entry.text for entry in entries if kinds is None or entry.kind in kinds)


def register(server: MCPServer, app: App) -> None:
    @server.tool(
        name="ahcc_compile",
        description=(
            "Compile one C file with AHCC 6.0 in an isolated headless Hatari instance. "
            "Runs mcp/project/scripts/ahcc-cli.py as a child process; the MCP supervisor is not used. "
            "The output must resolve under mcp/ or the system temp root. "
            "HATARI_MCP_BUILD_DIR only selects the default directory; it does not lift confinement."
        ),
        annotations=ToolAnnotations(destructiveHint=True, openWorldHint=False),
    )
    @guarded
    async def ahcc_compile(
        source: str,
        output: str | None = None,
        tos: str | None = None,
        includes: list[str] | None = None,
        defines: list[str] | None = None,
        cflags: list[str] | None = None,
        libs: list[str] | None = None,
        timeout_ms: int = 120000,
        keep: bool = False,
    ) -> dict[str, Any]:
        if timeout_ms <= 0:
            raise error("bad_option", f"timeout_ms must be positive: {timeout_ms}", "Use e.g. 120000.")
        source_path = Path(source).expanduser().resolve()
        if not source_path.is_file():
            raise error("invalid_path", f"Source does not exist: {source_path}", "Check the source path.")
        if source_path.suffix.lower() != ".c":
            raise error("bad_option", f"Source is not a .c file: {source_path}", "Pass a C source file.")

        if output is None:
            output_path = _default_output(source_path)
        else:
            output_path = Path(output).expanduser().resolve()
            if not is_writable_location(output_path):
                raise error(
                    "path_not_allowed",
                    f"Output must be under {ahcc.repo_root() / 'mcp'} or /tmp: {output_path}",
                    "Choose a writable output path; external corpus trees are read-only.",
                )

        discovered = ahcc.discover_paths()
        if not discovered["compiler_available"]:
            raise error(
                "ahcc_unavailable",
                "AHCC, Hatari, or hatari-prg-args is missing.",
                "Unpack AHCC under mcp/project/tools/ahcc and build Hatari, or check HATARI_AHCC_DIR/HATARI_BIN.",
            )
        if tos:
            tos_path = Path(tos).expanduser().resolve()
            if not tos_path.is_file():
                raise error("missing_tos", f"TOS image does not exist: {tos_path}", "Pass a readable TOS/EmuTOS image.")
        elif not discovered["tos_available"]:
            raise error(
                "missing_tos",
                "No TOS/EmuTOS image was found.",
                "Set HATARI_TOS or pass tos=<image>.",
            )

        include_paths: list[Path] = []
        for include in includes or []:
            include_path = Path(include).expanduser().resolve()
            if not include_path.is_dir():
                raise error(
                    "invalid_path",
                    f"Include directory does not exist: {include_path}",
                    "Check the include path.",
                )
            include_paths.append(include_path)

        result = await ahcc.compile_c(
            source_path,
            output_path,
            tos=tos,
            includes=include_paths,
            defines=defines or [],
            cflags=cflags or [],
            libs=libs or [],
            timeout=timeout_ms / 1000.0,
            keep=keep,
        )
        return result

    @server.tool(
        name="ahcc_status",
        description="Report discovered AHCC compiler, Hatari, TOS and GUI paths with availability flags.",
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False),
    )
    @guarded
    async def ahcc_status() -> dict[str, Any]:
        return {"ok": True, **ahcc.discover_paths(), "gui": ahcc.gui_paths()}

    @server.tool(
        name="ahcc_gui_start",
        description=(
            "Start AHCC's graphical shell (AHCCST.PRG) in a writable copy of AHCCST.BIN. "
            "Requires DISPLAY or WAYLAND_DISPLAY; returns unsupported_headless otherwise. "
            "Refuses to disturb an already running Hatari instance."
        ),
        annotations=ToolAnnotations(destructiveHint=False, openWorldHint=False),
    )
    @guarded
    async def ahcc_gui_start() -> dict[str, Any]:
        if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
            raise error(
                "unsupported_headless",
                "AHCC's GUI needs an X11/Wayland display.",
                "Run the MCP server in a graphical session or use ahcc_compile for headless builds.",
            )
        if app.supervisor.state in ALIVE:
            raise error(
                "already_running",
                "A Hatari instance is already running.",
                "Stop it with emu_stop first; ahcc_gui_start never kills an existing instance.",
            )
        paths = ahcc.gui_paths()
        if not paths["available"]:
            raise error(
                "ahcc_gui_unavailable",
                "AHCCST.PRG or the AHCCST.BIN directory is missing.",
                "Unpack the AHCC 6.0 distribution under mcp/project/tools/ahcc/ahcc-68000.",
            )

        source_dir = Path(paths["bin"])
        workspace = Path(
            tempfile.mkdtemp(prefix="hatari-mcp-ahcc-gui-", dir=str(app.settings.runtime_root))
        )
        try:
            shutil.copytree(source_dir, workspace, dirs_exist_ok=True, symlinks=False)
            autostart = workspace / "AHCCST.PRG"
            if not autostart.is_file():
                raise error(
                    "ahcc_gui_unavailable",
                    "AHCCST.PRG was not copied into the GUI workspace.",
                    "Check the AHCCST.BIN directory contents.",
                )
            result = await app.start(
                workspace=str(workspace),
                autostart=str(autostart),
                display=True,
            )
        except Exception:
            shutil.rmtree(workspace, ignore_errors=True)
            raise
        return {
            **result,
            "ok": True,
            "workspace": str(workspace),
            "autostart": str(autostart),
            "gui_prg": paths["prg"],
            "display_required": True,
        }

    @server.tool(
        name="ahcc_gui_status",
        description=(
            "Return the supervisor status for the AHCC GUI instance plus a screenshot path when available. "
            "Never sends debugger commands while Hatari is stopped at the prompt."
        ),
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False),
    )
    @guarded
    async def ahcc_gui_status() -> dict[str, Any]:
        status = app.supervisor.status()
        runtime = app.supervisor.runtime
        screenshot = runtime.path / "screen.png" if runtime else None
        note: str | None = None
        if status.get("at_prompt"):
            note = "screenshot skipped: Hatari is at the debugger prompt"
        elif runtime and status["state"] in {"running", "remote_paused"}:
            target = runtime.path / "screen.png"
            try:
                await app.supervisor.debug(f'screenshot "{target}"', timeout_ms=3000)
                screenshot = target
            except Exception as exc:  # best effort; GUI tooling must not fail status
                note = f"screenshot unavailable: {exc}"
        return {
            **status,
            "ok": True,
            "screenshot": str(screenshot) if screenshot and screenshot.is_file() else None,
            "screenshot_note": note,
        }

    @server.tool(
        name="run_prg",
        description=(
            "Run a PRG/TOS through the supervisor with its parent directory (or workspace) mapped as C:. "
            "Autostarts by relative guest path. Only NatFeats NF_EXIT is detected; Pterm/Pterm0 "
            "auto-exit is not enabled, so non-NatFeats programs report timeout rather than a fake exit code. "
            "External corpus directories are mounted write-protected."
        ),
        annotations=ToolAnnotations(destructiveHint=True, openWorldHint=False),
    )
    @guarded
    async def run_prg(
        program: str,
        workspace: str | None = None,
        args: list[str] | None = None,
        timeout_ms: int = 30000,
        wait: bool = True,
        display: bool = False,
        tos: str | None = None,
    ) -> dict[str, Any]:
        if timeout_ms <= 0:
            raise error("bad_option", f"timeout_ms must be positive: {timeout_ms}", "Use e.g. 30000.")
        program_path = Path(program).expanduser()
        if not program_path.is_absolute():
            base = Path(workspace).expanduser() if workspace else Path.cwd()
            program_path = base / program_path
        program_path = program_path.resolve()
        if not program_path.is_file():
            raise error("invalid_path", f"Program does not exist: {program_path}", "Check the program path.")

        ws = Path(workspace).expanduser().resolve() if workspace else program_path.parent
        if not ws.is_dir():
            raise error("invalid_path", f"Workspace does not exist: {ws}", "Pass an existing host directory.")
        # The workspace is mounted read-write as a GEMDOS drive.  Never expose a
        # corpus/repository tree (including this repository's mcp/ tree) that
        # way; stage the program into a private temp workspace instead.
        from ..services.scenario import _reject_protected_path

        _reject_protected_path(ws, "run_prg workspace")
        relative = guest_relative(ws, program_path)
        guest = f"C:\\{relative}"
        writable = is_writable_location(ws)
        write_protect = "off" if writable else "on"

        guest_args = [str(item) for item in args or []]
        arg_work: Path | None = None
        launch_args: list[str] = []
        if guest_args:
            arg_work, parse_file = _prepare_guest_args(program_path, guest_args)
            launch_args = ["--parse", f"prg:{parse_file}"]

        try:
            started = await app.start(
                args=launch_args,
                tos=tos,
                display=display,
                workspace=str(ws),
                autostart=str(program_path),
                gemdos_drive="C",
                write_protect=write_protect,
                natfeats=True,
            )
        except Exception:
            if arg_work:
                shutil.rmtree(arg_work, ignore_errors=True)
            raise

        result: dict[str, Any] = {
            "ok": True,
            "waited": bool(wait),
            "exited": False,
            "timed_out": False,
            "program": str(program_path),
            "workspace": str(ws),
            "guest_path": guest,
            "guest_args": guest_args,
            "write_protect": write_protect,
            "read_only": not writable,
            "emulator": started,
            "exit_detection": "natfeats",
            "pterm_auto_exit": False,
            "hint": (
                "Only NatFeats NF_EXIT is detected; Pterm/Pterm0 auto-exit is not enabled. "
                "Programs that terminate without NF_EXIT return exit_source=timeout."
            ),
        }
        if not wait:
            if arg_work:
                _schedule_cleanup(app, arg_work)
            return result

        try:
            exit_info = await app.supervisor.wait_for_exit(timeout_ms)
            supervisor = app.supervisor
            guest_exit = supervisor.program_exit_code
            timed_out = bool(exit_info["timed_out"])
            if timed_out:
                exit_source = "timeout"
            elif guest_exit is not None:
                exit_source = supervisor.exit_reason or "natfeats"
            elif exit_info["exited"]:
                exit_source = "process_exit"
            else:
                exit_source = "unknown"
            result.update(
                {
                    "exited": bool(exit_info["exited"]),
                    "timed_out": timed_out,
                    "exit_code": guest_exit,
                    "exit_source": exit_source,
                    "emulator_returncode": supervisor.returncode,
                    "duration_ms": exit_info["duration_ms"],
                    "console": _tail_text(supervisor.stdout_ring.tail(500), {"console"}),
                    "stderr_tail": _tail_text(supervisor.stderr_ring.tail(200)),
                    "screenshot": None,
                }
            )
            return result
        finally:
            if arg_work:
                shutil.rmtree(arg_work, ignore_errors=True)

    @server.tool(
        name="workspace_info",
        description=(
            "Describe a host workspace without modifying it: guest drive mapping, bounded file count, "
            "and candidate .PRG/.TOS files with their C: guest paths."
        ),
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False),
    )
    @guarded
    async def workspace_info(path: str) -> dict[str, Any]:
        root = Path(path).expanduser().resolve()
        if not root.exists():
            raise error("invalid_path", f"Workspace does not exist: {root}", "Check the directory path.")
        if not root.is_dir():
            raise error("invalid_path", f"Workspace is not a directory: {root}", "Pass a host directory.")
        files, truncated = _scan_files(root)
        candidates = [
            file for file in files if file.suffix.lower() in _CANDIDATE_SUFFIXES
        ]
        candidates.sort(key=lambda file: str(file).lower())
        programs = []
        for file in candidates[:_CANDIDATE_LIMIT]:
            relative = file.relative_to(root)
            separator = "\\"
            programs.append(
                {
                    "path": str(file),
                    "relative": relative.as_posix(),
                    "guest_path": "C:\\" + separator.join(relative.parts),
                    "size": file.stat().st_size,
                }
            )
        writable = is_writable_location(root)
        return {
            "ok": True,
            "host_path": str(root),
            "guest_drive": "C",
            "guest_root": "C:\\",
            "writable": writable,
            "read_only": not writable,
            "file_count": len(files),
            "file_count_truncated": truncated,
            "candidate_count": len(candidates),
            "candidates_truncated": len(candidates) > _CANDIDATE_LIMIT,
            "programs": programs,
        }
