from __future__ import annotations

import hashlib
import json

import pytest

from hatari_mcp.services.scenario import (
    ArtifactBundle,
    ArtifactQuotaExceeded,
    ScenarioError,
)


def test_bundle_layout_manifest_and_checksums(tmp_path):
    bundle = ArtifactBundle.create(tmp_path, "run1", "case/one")
    bundle.write_text("logs/stdout.log", "hello\n")
    bundle.write_bytes("screenshots/a.bmp", b"BMfake")
    final = bundle.finalize("passed", {"scenario": "demo"})

    assert final.name == "attempt-1"
    assert (final / ".complete").is_file()
    assert (final / "manifest.json").is_file()
    manifest = json.loads((final / "manifest.json").read_text())
    assert manifest["schema"] == "hatari-mcp.manifest/1"
    assert manifest["status"] == "passed"
    assert manifest["scenario"] == "demo"
    paths = {item["path"]: item for item in manifest["artifacts"]}
    assert paths["logs/stdout.log"]["size"] == 6
    assert paths["screenshots/a.bmp"]["sha256"] == hashlib.sha256(b"BMfake").hexdigest()

    checksum_lines = (final / "checksums.sha256").read_text().splitlines()
    checked = {line.split("  ", 1)[1]: line.split("  ", 1)[0] for line in checksum_lines}
    for rel, digest in checked.items():
        assert hashlib.sha256((final / rel).read_bytes()).hexdigest() == digest
    assert not bundle.staging.exists()


def test_bundle_quota_is_enforced_but_still_finalizes(tmp_path):
    bundle = ArtifactBundle.create(tmp_path, "run1", "case", max_bytes=10)
    bundle.write_bytes("small.txt", b"12345")
    with pytest.raises(ArtifactQuotaExceeded):
        bundle.write_bytes("large.txt", b"x" * 20)
    final = bundle.finalize("error", {"quota": True})
    assert (final / ".complete").is_file()
    manifest = json.loads((final / "manifest.json").read_text())
    assert manifest["status"] == "error"
    assert not (final / "large.txt").exists()


def test_bundle_rejects_path_escape(tmp_path):
    bundle = ArtifactBundle.create(tmp_path, "run1", "case")
    with pytest.raises(ScenarioError):
        bundle.write_bytes("../escape.txt", b"x")
    assert not (tmp_path / "escape.txt").exists()
