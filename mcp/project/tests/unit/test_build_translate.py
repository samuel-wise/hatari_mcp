"""Unit tests for build-path translation, discovery, and compile timeouts."""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

import pytest

from hatari_mcp.errors import HatariError
from hatari_mcp.services import ahcc
from hatari_mcp.tools import build


def test_guest_path_translates_nested_program(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    program = workspace / "src" / "hello.PRG"
    program.parent.mkdir(parents=True)
    program.write_bytes(b"x")

    assert build.guest_relative(workspace, program) == "src\\hello.PRG"
    assert build.guest_path(workspace, program) == "C:\\src\\hello.PRG"
    assert build.guest_path(workspace, program, "d") == "D:\\src\\hello.PRG"


def test_guest_path_rejects_outside_and_bad_drive(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    outside = tmp_path / "outside.PRG"
    outside.write_bytes(b"x")

    with pytest.raises(HatariError) as exc:
        build.guest_path(workspace, outside)
    assert exc.value.code == "path_not_allowed"

    with pytest.raises(HatariError) as exc:
        build.guest_path(workspace, workspace / "x.PRG", "1")
    assert exc.value.code == "bad_option"


def test_writable_locations() -> None:
    repo = ahcc.repo_root()
    assert build.is_writable_location(repo / "mcp" / "project")
    assert build.is_writable_location(Path("/tmp/hatari-mcp-test"))
    assert not build.is_writable_location(repo)
    assert not build.is_writable_location(Path("/var/tmp/hatari-mcp-test"))
    assert not build.is_writable_location(Path("/tmp-evil/thing"))


def test_candidate_programs_filters_sorts_and_skips_symlinks(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    nested = root / "nested"
    nested.mkdir(parents=True)
    (root / "b.TOS").write_bytes(b"b")
    (root / "A.PRG").write_bytes(b"a")
    (nested / "c.prg").write_bytes(b"c")
    (root / "notes.txt").write_text("skip", encoding="utf-8")

    outside = tmp_path / "outside.PRG"
    outside.write_bytes(b"secret")
    os.symlink(outside, root / "escape.PRG")
    os.symlink(tmp_path, root / "escape-dir")

    found = build.candidate_programs(root)
    assert [path.name for path in found] == ["A.PRG", "b.TOS", "c.prg"]
    assert outside not in found

    files, truncated = build._scan_files(root)
    assert not truncated
    assert all(not path.is_symlink() for path in files)
    assert {path.name for path in files} == {"A.PRG", "b.TOS", "c.prg", "notes.txt"}


def test_candidate_programs_is_bounded(tmp_path: Path) -> None:
    root = tmp_path / "many"
    root.mkdir()
    for index in range(6):
        (root / f"{index}.PRG").write_bytes(b"x")
    assert len(build.candidate_programs(root, limit=2)) == 2


def test_discovery_uses_environment_and_reports_availability(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    script = repo / "mcp/project/scripts/ahcc-cli.py"
    script.parent.mkdir(parents=True)
    script.write_text("", encoding="utf-8")

    ahcc_dir = tmp_path / "AHCCST.BIN"
    ahcc_dir.mkdir()
    (ahcc_dir / "AHCCST_P.TTP").write_bytes(b"ttp")
    (ahcc_dir / "AHCCST.PRG").write_bytes(b"prg")

    hatari = tmp_path / "bin/hatari"
    hatari.parent.mkdir()
    hatari.write_bytes(b"elf")
    (hatari.parent / "hatari-prg-args").write_bytes(b"sh")

    tos = tmp_path / "etos.img"
    tos.write_bytes(b"rom")

    monkeypatch.setenv("HATARI_MCP_REPO", str(repo))
    monkeypatch.setenv("HATARI_AHCC_DIR", str(ahcc_dir))
    monkeypatch.setenv("HATARI_BIN", str(hatari))
    monkeypatch.setenv("HATARI_TOS", str(tos))

    paths = ahcc.discover_paths()
    assert paths["repo_root"] == str(repo)
    assert paths["script_available"] is True
    assert paths["compiler_available"] is True
    assert paths["hatari_available"] is True
    assert paths["tos_available"] is True
    assert paths["available"] is True
    assert paths["ahcc_gui_prg"] == str(ahcc_dir / "AHCCST.PRG")

    gui = ahcc.gui_paths()
    assert gui["available"] is True
    assert gui["prg"] == str(ahcc_dir / "AHCCST.PRG")
    assert gui["bin"] == str(ahcc_dir)


def test_gui_paths_missing_ahcc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("HATARI_MCP_REPO", str(repo))
    monkeypatch.delenv("HATARI_AHCC_DIR", raising=False)
    gui = ahcc.gui_paths()
    assert gui == {
        "prg": None,
        "bin": None,
        "prg_available": False,
        "bin_available": False,
        "available": False,
    }


def test_prepare_guest_args_converts_program_relative_files(tmp_path: Path) -> None:
    program = tmp_path / "ws" / "RUN.PRG"
    program.parent.mkdir()
    program.write_bytes(b"x")
    data = program.parent / "input.dat"
    data.write_bytes(b"data")

    work, start = build._prepare_guest_args(program, [str(data), "plain"])
    try:
        payload = (work / "args").read_bytes()
        assert payload == b"C:\\INPUT.DAT plain\0"
        assert start.is_file()
        assert "basepage+0x81" in (work / "basepage.ini").read_text(encoding="latin-1")
    finally:
        import shutil

        shutil.rmtree(work, ignore_errors=True)


def test_prepare_guest_args_rejects_too_long(tmp_path: Path) -> None:
    program = tmp_path / "RUN.PRG"
    program.write_bytes(b"x")
    with pytest.raises(HatariError) as exc:
        build._prepare_guest_args(program, ["x" * 127])
    assert exc.value.code == "bad_option"


def test_compile_timeout_kills_child(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / "repo"
    script = repo / "mcp/project/scripts/ahcc-cli.py"
    script.parent.mkdir(parents=True)
    pid_file = tmp_path / "grandchild.pid"
    script.write_text(
        "import subprocess, sys, time\n"
        "child = subprocess.Popen(\n"
        "    [sys.executable, '-c', 'import time; time.sleep(30)'],\n"
        "    start_new_session=True,\n"
        ")\n"
        f"open({str(pid_file)!r}, 'w').write(str(child.pid))\n"
        "child.wait()\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HATARI_MCP_REPO", str(repo))

    source = tmp_path / "hello.c"
    source.write_text("int main(void){return 0;}\n", encoding="utf-8")
    output = tmp_path / "hello.TOS"

    started = time.monotonic()
    result = asyncio.run(ahcc.compile_c(source, output, timeout=0.5))
    elapsed = time.monotonic() - started

    assert result["ok"] is False
    assert result["timed_out"] is True
    assert result["error"] == "build_timeout"
    assert result["returncode"] is not None
    assert result["exists"] is False
    assert elapsed < 10

    grandchild = int(pid_file.read_text(encoding="utf-8"))
    for _ in range(50):
        try:
            os.kill(grandchild, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    else:
        pytest.fail("the AHCC script's emulator/session child survived the timeout")
