#!/usr/bin/env python3
"""Compile an Atari ST C program with AHCC under headless Hatari."""

from __future__ import annotations

import argparse
import os
import queue
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path


def repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def default_tos() -> Path:
    env = os.environ.get("HATARI_TOS")
    if env:
        return Path(env).expanduser().resolve()
    for path in (
        Path.home() / "Projects/Hatari/tos/emutos/etos192us.img",
        Path.home() / "Projects/Hatari/tos/tos3x/roms/tos306us.img",
        Path.home() / "Projects/Hatari/tos/tos1x/roms/tos104us.img",
    ):
        if path.is_file():
            return path.resolve()
    raise FileNotFoundError(
        "no TOS image found; pass --tos or set HATARI_TOS"
    )


def terminate(proc: subprocess.Popen[str]) -> None:
    if proc.poll() is not None:
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        proc.wait(timeout=5)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass


def build_project(
    work: Path,
    source: Path,
    include_dirs: list[Path],
    defines: list[str],
    cflags: list[str],
    libs: list[str],
    startup: str,
) -> Path:
    project = work / "MCPOUT.PRJ"
    shutil.copyfile(source, work / "SOURCE.C")

    localinc = work / "LOCALINC"
    localinc.mkdir()
    for header in source.parent.iterdir():
        if header.is_file() and header.suffix.lower() == ".h":
            shutil.copy2(header, localinc / header.name)

    options = ["-Iinclude", "-Isinclude", "-ILOCALINC"]
    for index, include_dir in enumerate(include_dirs, start=1):
        dest = work / f"INC{index}"
        shutil.copytree(include_dir, dest)
        options.append(f"-I{dest.name}")
    options.extend(f"-D{define}" for define in defines)
    options.extend(cflags)

    lines = [
        "MCPOUT.TOS",
        "=",
        startup,
        "SOURCE.C",
        *libs,
        ".C [" + " ".join(options) + "]",
        "",
    ]
    project.write_text("\n".join(lines), encoding="latin-1")
    return project


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compile a C file with AHCC 6.0 in headless Hatari.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Example:\n"
            "  ahcc-cli.py hello.c -o hello.tos\n"
            "  ahcc-cli.py hello.c --define DEBUG=1 --include inc\n\n"
            "Extra Hatari options can be passed with --hatari-arg, e.g.\n"
            "  --hatari-arg=--memsize --hatari-arg=4\n"
        ),
    )
    parser.add_argument("source", type=Path, help="C source file")
    parser.add_argument("-o", "--output", type=Path, help="output TOS file")
    parser.add_argument("--tos", type=Path, help="TOS/EmuTOS ROM image")
    parser.add_argument(
        "--ahcc-dir",
        type=Path,
        default=repo_root()
        / "mcp/project/tools/ahcc/ahcc-68000/AHCCST.BIN",
        help="AHCCST.BIN directory",
    )
    parser.add_argument(
        "--hatari-bin",
        type=Path,
        default=repo_root() / "build-install/bin/hatari",
        help="hatari executable",
    )
    parser.add_argument(
        "--include",
        type=Path,
        action="append",
        default=[],
        help="extra include directory (repeatable)",
    )
    parser.add_argument(
        "--define",
        action="append",
        default=[],
        help="preprocessor define NAME or NAME=VALUE (repeatable)",
    )
    parser.add_argument(
        "--cflag",
        action="append",
        default=[],
        help="raw AHCC compiler flag (repeatable)",
    )
    parser.add_argument(
        "--lib",
        action="append",
        default=[],
        help="extra AHCC library (repeatable)",
    )
    parser.add_argument(
        "--startup",
        default="lib\\ahcstart.o",
        help="AHCC startup object",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=120.0,
        help="overall timeout in seconds (default: 120)",
    )
    parser.add_argument(
        "--keep",
        action="store_true",
        help="keep the temporary build directory",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="show the Hatari command line",
    )
    parser.add_argument(
        "--hatari-arg",
        action="append",
        default=[],
        help="extra Hatari option (repeatable)",
    )
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)

    source = args.source.expanduser().resolve()
    if not source.is_file():
        print(f"error: source not found: {source}", file=sys.stderr)
        return 2
    if source.suffix.lower() != ".c":
        print(f"error: source is not a .c file: {source}", file=sys.stderr)
        return 2

    ahcc_dir = args.ahcc_dir.expanduser().resolve()
    if not (ahcc_dir / "AHCCST_P.TTP").is_file():
        print(f"error: AHCC not found in {ahcc_dir}", file=sys.stderr)
        print(
            "unzip ahcc-68000.zip there first (see mcp/project/tools/ahcc)",
            file=sys.stderr,
        )
        return 2

    hatari = args.hatari_bin.expanduser().resolve()
    if not hatari.is_file():
        print(f"error: hatari not found: {hatari}", file=sys.stderr)
        return 2
    prg_args = hatari.parent / "hatari-prg-args"
    if not prg_args.is_file():
        print(f"error: hatari-prg-args not found: {prg_args}", file=sys.stderr)
        return 2

    if args.tos:
        tos = args.tos.expanduser().resolve()
    else:
        try:
            tos = default_tos()
        except FileNotFoundError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
    if not tos.is_file():
        print(f"error: TOS image not found: {tos}", file=sys.stderr)
        return 2

    output = (
        args.output.expanduser()
        if args.output
        else source.with_suffix(".TOS")
    ).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)

    include_dirs = [p.expanduser().resolve() for p in args.include]
    for include_dir in include_dirs:
        if not include_dir.is_dir():
            print(f"error: include directory not found: {include_dir}", file=sys.stderr)
            return 2

    libs = ["ahccstdi.lib", *args.lib]
    work = Path(tempfile.mkdtemp(prefix="ahcc-cli-"))
    listener: socket.socket | None = None
    proc: subprocess.Popen[str] | None = None
    conn: socket.socket | None = None

    try:
        shutil.copytree(ahcc_dir, work, dirs_exist_ok=True)
        project = build_project(
            work,
            source,
            include_dirs,
            args.define,
            args.cflag,
            libs,
            args.startup,
        )

        sock_path = work / "control.sock"
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(sock_path))
        listener.listen(1)
        listener.settimeout(min(30.0, args.timeout))

        cmd = [
            str(prg_args),
            "-q",
            "--control-socket",
            str(sock_path),
            "--tos",
            str(tos),
            "--conout",
            "2",
            "--fast-boot",
            "on",
            "--fast-forward",
            "on",
            *args.hatari_arg,
            "--",
            "AHCCST_P.TTP",
            project.name,
        ]
        env = os.environ.copy()
        env["PATH"] = str(hatari.parent) + os.pathsep + env.get("PATH", "")
        env["SDL_VIDEODRIVER"] = "dummy"

        if args.verbose:
            print("command:", " ".join(cmd))
            print("work:", work)

        proc = subprocess.Popen(
            cmd,
            cwd=work,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=env,
            start_new_session=True,
        )
        assert proc.stdout is not None

        try:
            conn, _ = listener.accept()
        except socket.timeout:
            print("error: Hatari did not connect to the control socket", file=sys.stderr)
            terminate(proc)
            return 1

        lines: list[str] = []
        line_queue: queue.Queue[str | None] = queue.Queue()

        def reader() -> None:
            try:
                for line in proc.stdout:
                    line_queue.put(line)
            finally:
                line_queue.put(None)

        threading.Thread(target=reader, daemon=True).start()

        sent_key = False
        timed_out = False
        deadline = time.monotonic() + args.timeout
        while True:
            try:
                item = line_queue.get(timeout=0.2)
            except queue.Empty:
                if time.monotonic() > deadline:
                    print(
                        f"error: compile timed out after {args.timeout:g}s",
                        file=sys.stderr,
                    )
                    terminate(proc)
                    timed_out = True
                    break
                continue

            if item is None:
                break

            lines.append(item)
            sys.stdout.write(item)
            sys.stdout.flush()

            if not sent_key and "press any key" in item.lower():
                conn.sendall(b"hatari-event keypress x\n")
                sent_key = True

        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            terminate(proc)

        if timed_out:
            return 1

        text = "".join(lines)
        failed = bool(re.search(r"(?im)^\s*(?:error|fatal)\b", text))
        built = work / "MCPOUT.TOS"
        if failed:
            print("error: AHCC reported errors; output not copied", file=sys.stderr)
            return 1
        if not built.is_file():
            print("error: AHCC did not produce an executable", file=sys.stderr)
            return 1
        shutil.copyfile(built, output)

        print(f"output: {output} ({output.stat().st_size} bytes)")
        return 0
    finally:
        if conn is not None:
            conn.close()
        if listener is not None:
            listener.close()
        if proc is not None and proc.poll() is None:
            terminate(proc)
        if args.keep:
            print(f"kept: {work}")
        else:
            shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        raise SystemExit(130)
