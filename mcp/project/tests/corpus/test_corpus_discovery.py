from __future__ import annotations

import json
from pathlib import Path

import pytest

from hatari_mcp.corpus import (
    discover_corpus,
    is_within_corpus,
    stage_files,
    tree_signature,
)


def make_fake_corpus(tmp_path: Path) -> dict[str, Path]:
    okami = tmp_path / "Okami-Shell"
    (okami / "Files").mkdir(parents=True)
    (okami / "Files" / "GEM.PRG").write_bytes(b"GEM")
    (okami / "README.md").write_text("okami", encoding="utf-8")

    freemint = tmp_path / "freemint"
    (freemint / "cflib").mkdir(parents=True)
    (freemint / "cflib" / "smoke.prg").write_bytes(b"CFLIB")
    for name in ("gemlib", "libcmini", "mintlib"):
        (freemint / name).mkdir()

    godlib = tmp_path / "godlib"
    (godlib / "godlib").mkdir(parents=True)
    (godlib / "godlib.spl" / "input").mkdir(parents=True)
    (godlib / "godlib.spl" / "input" / "INPUT.PRG").write_bytes(b"INPUT")
    return {"okami": okami, "freemint": freemint, "godlib": godlib}


def test_discovery_from_env_roots_and_skip_reasons(tmp_path):
    roots = make_fake_corpus(tmp_path)
    env = {
        "OKAMI_ROOT": str(roots["okami"]),
        "FREEMINT_ROOT": str(roots["freemint"]),
        "GODLIB_ROOT": str(roots["godlib"]),
        "HATARI_TOS": str(tmp_path / "missing.img"),
    }

    data = discover_corpus(env)

    assert data["read_only"] is True
    assert data["projects"]["okami"]["available"] is True
    assert data["projects"]["okami"]["prebuilt"]
    assert data["projects"]["freemint"]["subprojects"]["cflib"]["available"] is True
    assert data["projects"]["godlib"]["available"] is True
    assert data["projects"]["okami"]["license_note"]


def test_absent_roots_skip_with_exact_reason(tmp_path):
    env = {
        "OKAMI_ROOT": str(tmp_path / "no-okami"),
        "FREEMINT_ROOT": str(tmp_path / "no-freemint"),
        "GODLIB_ROOT": str(tmp_path / "no-godlib"),
    }
    data = discover_corpus(env)
    for name in ("okami", "freemint", "godlib"):
        project = data["projects"][name]
        assert project["available"] is False
        assert project["skip_reason"]
        assert "missing" in project["skip_reason"]


def test_staging_is_read_only_and_records_manifest(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    source = root / "data.bin"
    source.write_bytes(b"payload")
    before = tree_signature(root)
    workspace = tmp_path / "workspace"

    manifest = stage_files(root, [{"path": "data.bin", "to": "staged/data.bin"}], workspace)
    after = tree_signature(root)

    assert before == after
    assert (workspace / "staged" / "data.bin").read_bytes() == b"payload"
    assert (workspace / ".stage-manifest.json").is_file()
    assert manifest["read_only_source"] is True
    assert manifest["entries"][0]["sha256"]


def test_staging_rejects_escape(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    (tmp_path / "outside.bin").write_bytes(b"outside")
    with pytest.raises(ValueError):
        stage_files(root, ["../outside.bin"], tmp_path / "workspace")


def test_is_within_corpus(tmp_path):
    roots = make_fake_corpus(tmp_path)
    env = {
        "OKAMI_ROOT": str(roots["okami"]),
        "FREEMINT_ROOT": str(roots["freemint"]),
        "GODLIB_ROOT": str(roots["godlib"]),
    }
    assert is_within_corpus(roots["okami"] / "Files" / "GEM.PRG", env)
    assert not is_within_corpus(tmp_path / "elsewhere.bin", env)


def test_local_corpus_discovery_shape():
    data = discover_corpus()
    assert set(data["projects"]) == {"okami", "freemint", "godlib"}
    for project in data["projects"].values():
        assert "root" in project
        assert "skip_reason" in project
        assert project["read_only"] is True


def test_stage_manifest_is_json(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    (root / "a").write_text("a", encoding="utf-8")
    stage_files(root, ["a"], tmp_path / "ws")
    payload = json.loads((tmp_path / "ws" / ".stage-manifest.json").read_text())
    assert payload["entries"][0]["to"] == "a"
