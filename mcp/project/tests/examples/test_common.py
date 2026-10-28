"""Tests for the shared example-program foundation."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

PROJECT = Path(__file__).resolve().parents[2]
PROGRAMS = PROJECT / "examples" / "programs"
if str(PROGRAMS) not in sys.path:
    sys.path.insert(0, str(PROGRAMS))

from common import catalog, config, mcp_client, paths  # noqa: E402
from common.game_runner import GameSession  # noqa: E402


def test_tosec_parsing() -> None:
    menu = catalog.parse_tosec_name(
        "Automation Menu Disk 270 (1990)(Automation)[STE fixed].zip"
    )
    assert menu.title == "Automation Menu Disk 270"
    assert menu.year == 1990
    assert menu.publisher == "Automation"
    assert menu.tags == ("STE fixed",)
    assert menu.genre is None

    multi = catalog.parse_tosec_name(
        "Superior Menu Disk 058 (1991)(Superior)(Disk 1 of 3)(Part A)[a].zip"
    )
    assert multi.title == "Superior Menu Disk 058"
    assert multi.publisher == "Superior"
    assert multi.disk == 1
    assert multi.disk_total == 3
    assert multi.part == "A"
    assert multi.tags == ("a",)

    unknown = catalog.parse_tosec_name("Mystery Disk (19xx)(Some Publisher)(Puzzle).st")
    assert unknown.year is None
    assert unknown.year_raw == "19xx"
    assert unknown.publisher == "Some Publisher"
    assert unknown.genre == "Puzzle"

    compilation = catalog.parse_tosec_name(
        "Pandora (1987)(Firebird)[cr Union] & Peter Beardsley's International Football "
        "(1988)(Grandslam).st"
    )
    assert compilation.title.startswith("Pandora")
    assert compilation.year == 1987
    assert compilation.publisher == "Firebird"


def test_catalog_discovery_sidecars_and_pagination(tmp_path: Path) -> None:
    root = tmp_path / "games"
    root.mkdir()
    (root / "Alpha (1990)(Pub)(Action).st").write_bytes(b"ST" * 10)
    (root / "Beta (19xx)(Other)(Disk 2 of 3).msa").write_bytes(b"MSA" * 10)
    (root / "Gamma (1995)(Pub).stx").write_bytes(b"STX" * 10)
    (root / "Delta (1993)(Pub).zip").write_bytes(b"PK" + b"\0" * 20)
    (root / "notes.txt").write_text("ignore me")
    (tmp_path / "state" / "metadata").mkdir(parents=True)

    alpha = root / "Alpha (1990)(Pub)(Action).st"
    alpha_id = catalog.stable_id(alpha)
    (root / f"{alpha.name}.json").write_text(
        json.dumps({"publisher": "Sidecar Publisher", "genre": "Action"}),
        encoding="utf-8",
    )
    (tmp_path / "state" / "metadata" / f"{alpha_id}.json").write_text(
        json.dumps({"art": {"path": "/tmp/alpha.png"}}), encoding="utf-8"
    )

    found = catalog.Catalog(
        [root], state_dir=tmp_path / "state", metadata_dir=tmp_path / "state" / "metadata"
    )
    found.scan(timeout=10)
    assert found.ready
    assert found.error is None
    assert len(found) == 4
    assert found.count("alpha") == 1

    entry = found.get(alpha_id)
    assert entry is not None
    assert entry.metadata["publisher"] == "Sidecar Publisher"
    assert entry.metadata["art"]["path"] == "/tmp/alpha.png"
    assert entry.id == catalog.stable_id(alpha)

    page = found.get_page(offset=1, limit=2, sort="title")
    assert len(page) == 2
    assert found.get_page(query="other", limit=10)[0].title == "Beta"

    second = catalog.Catalog(
        [root], state_dir=tmp_path / "state", metadata_dir=tmp_path / "state" / "metadata"
    )
    second.scan(timeout=10)
    assert {item.id for item in second} == {item.id for item in found}


def test_config_roundtrip_and_atomic_concurrent_writes(tmp_path: Path) -> None:
    path = tmp_path / "state" / "config.json"
    cfg = config.Config(path)
    cfg.add_game_dir(tmp_path / "games")
    cfg.ui_set("machine", "ste")
    cfg.record_launch(
        "game-1",
        machine="st",
        profile="st",
        tos="/tmp/tos.img",
        args=["--fast-boot", "on"],
        resolution=(320, 200),
        status="success",
        success=True,
    )
    cfg.record_launch(
        "game-1",
        machine="st",
        profile="st",
        tos="/tmp/tos.img",
        status="failure",
        failure_reason="timeout",
        success=False,
    )
    cfg.save()

    loaded = config.Config(path)
    assert loaded.game_dirs == [str(tmp_path / "games")]
    assert loaded.ui_get("machine") == "ste"
    record = loaded.get_game("game-1")
    assert record["status"] == "failure"
    assert record["failure_reason"] == "timeout"
    assert record["last_success"] is not None
    assert record["attempts"] == 2
    assert loaded.learned_launch("game-1")["machine"] == "st"

    def writer(index: int) -> None:
        for iteration in range(20):
            cfg.update_game(f"thread-{index}", {"iteration": iteration})

    threads = [threading.Thread(target=writer, args=(index,)) for index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    raw = path.read_text(encoding="utf-8")
    parsed = json.loads(raw)
    assert len(parsed["games"]) == 9
    assert not list(path.parent.glob("*.tmp"))
    assert '"iteration": 19' in raw


def test_config_recovers_from_corrupt_json(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text("{not json", encoding="utf-8")
    cfg = config.Config(path)
    assert cfg.load_error
    assert cfg.game_dirs == []
    backups = list(tmp_path.glob("config.json.corrupt-*"))
    assert backups


def test_space_free_symlink_and_zip_extraction(tmp_path: Path) -> None:
    source_dir = tmp_path / "game dir"
    source_dir.mkdir()
    source = source_dir / "My Game (1990)(Publisher).st"
    source.write_bytes(b"disk-data")
    dest = tmp_path / "state"
    link = paths.make_space_free(source, dest)
    assert " " not in str(link)
    assert link.is_symlink()
    assert link.resolve() == source.resolve()
    assert link.read_bytes() == b"disk-data"

    copied = paths.make_space_free(source, dest / "copies", mode="copy")
    assert " " not in str(copied)
    assert not copied.is_symlink()
    assert copied.read_bytes() == b"disk-data"

    archive = tmp_path / "archive.zip"
    import zipfile

    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("readme.txt", "ignored")
        zf.writestr("Disk Image.st", b"inner-disk")
    extracted = paths.materialize_disk(archive, dest / "extracted")
    assert " " not in str(extracted)
    assert extracted.suffix == ".st"
    assert extracted.read_bytes() == b"inner-disk"


def test_result_normalization() -> None:
    structured = SimpleNamespace(structured_content={"ok": True, "state": "running"})
    assert mcp_client.normalize_tool_result(structured, tool="emu_status") == {
        "ok": True,
        "state": "running",
    }

    json_text = SimpleNamespace(
        structured_content=None,
        content=[SimpleNamespace(type="text", text='{"ok": true, "count": 2}')],
        is_error=False,
    )
    assert mcp_client.normalize_tool_result(json_text, tool="x")["count"] == 2

    plain = SimpleNamespace(
        structured_content=None,
        content=[SimpleNamespace(type="text", text="hello")],
        is_error=False,
    )
    assert mcp_client.normalize_tool_result(plain, tool="x") == {"ok": True, "text": "hello"}

    error = SimpleNamespace(
        structured_content=None,
        content=[
            SimpleNamespace(
                type="text",
                text="not_running: Hatari is not running. Hint: Call emu_start first.",
            )
        ],
        is_error=True,
    )
    with pytest.raises(mcp_client.MCPToolError) as raised:
        mcp_client.normalize_tool_result(error, tool="emu_status")
    assert raised.value.code == "not_running"
    assert raised.value.hint == "Call emu_start first." and "emu_status" in str(raised.value)


def test_import_does_not_create_tk_root() -> None:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(PROGRAMS)
    env.pop("DISPLAY", None)
    env.pop("WAYLAND_DISPLAY", None)
    code = (
        "import tkinter, common, common.ui, common.game_runner, common.debug_session;"
        "print(tkinter._default_root)"
    )
    completed = subprocess.run(
        [sys.executable, "-c", code],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip().endswith("None")


def _hatari_and_tos() -> tuple[Path | None, Path | None]:
    repo = PROJECT.parents[1]
    candidates = [
        repo / "build-install" / "bin" / "hatari",
        repo / "build" / "src" / "hatari",
        Path(shutil.which("hatari")) if shutil.which("hatari") else None,
    ]
    hatari = next((item for item in candidates if item and Path(item).is_file()), None)
    return hatari, paths.find_tos("emutos")


HATARI, EMUTOS = _hatari_and_tos()


@pytest.mark.skipif(
    HATARI is None or EMUTOS is None, reason="Hatari or EmuTOS is unavailable"
)
def test_live_emutos_smoke(tmp_path: Path) -> None:
    frame = tmp_path / "live.bmp"
    with mcp_client.MCPClient(call_timeout=60.0, shutdown_timeout=30.0) as client:
        tools = client.list_tools()
        assert len(tools) >= 100
        status = client.call_tool("emu_status", timeout=10.0)
        assert status["state"] == "stopped"

        session = GameSession(client)
        result = session.start_emutos(tos=EMUTOS, timeout=60.0)
        assert result.ok, result.evidence
        assert result.resolution is not None

        capture = client.call_tool(
            "screen_frame_capture", path=str(frame), format="bmp", timeout=20.0
        )
        assert Path(capture["path"]).is_file()
        assert capture["width"] and capture["height"]

        stopped = session.stop()
        assert stopped.get("state") in {"exited", "stopped"}
        assert client.call_tool("emu_status", timeout=10.0)["state"] in {
            "exited",
            "stopped",
        }
