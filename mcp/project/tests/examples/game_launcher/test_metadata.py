"""Metadata sidecar merge, provenance and cached-art tests."""

from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[3]
PROGRAMS = PROJECT / "examples" / "programs"
if str(PROGRAMS) not in sys.path:
    sys.path.insert(0, str(PROGRAMS))

from common.catalog import DiskEntry  # noqa: E402
from game_launcher.metadata import MetadataResolver  # noqa: E402


def _write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_sidecar_merge_precedence_and_provenance(tmp_path: Path) -> None:
    root = tmp_path / "games"
    root.mkdir()
    disk = root / "Speedball 2 (1990)(Image Works)(Sports).zip"
    disk.write_bytes(b"PK" + b"\0" * 32)
    entry = DiskEntry.from_path(disk, root)
    state = tmp_path / "state"
    metadata_dir = state / "metadata"

    art = root / "speedball2.png"
    art.write_bytes(b"fake-png")
    online = {
        "publisher": "Online Publisher",
        "year": 1991,
        "description": "online description",
        "machine": "ste",
        "provenance": {"source": "online:test"},
    }
    local_primary = {
        "title": "Speedball 2 HD",
        "genre": "Action",
        "art": "speedball2.png",
        "art_source": "local box scan",
        "resolution": [320, 200],
        "args": ["--sound", "off"],
    }
    local_secondary = {"publisher": "Second Local Publisher"}
    _write(metadata_dir / f"{entry.id}.json", online)
    _write(disk.with_suffix(disk.suffix + ".json"), local_primary)
    _write(disk.with_suffix(".json"), local_secondary)

    resolver = MetadataResolver(
        state_dir=state,
        metadata_dir=metadata_dir,
        cache_dir=state / "cache" / "art",
    )
    resolved = resolver.resolve(entry)

    assert resolved.title == "Speedball 2 HD"
    assert resolved.year == 1991
    assert resolved.publisher == "Second Local Publisher"
    assert resolved.genre == "Action"
    assert resolved.description == "online description"
    assert resolved.machine == "ste"
    assert resolved.resolution == (320, 200)
    assert resolved.args == ("--sound", "off")
    assert resolved.art_path == art.resolve()
    assert resolved.art_source == "local box scan"

    assert resolved.provenance["title"] == f"sidecar:{disk.with_suffix(disk.suffix + '.json')}"
    assert resolved.provenance["publisher"] == f"sidecar:{disk.with_suffix('.json')}"
    assert resolved.provenance["year"] == "online:test"
    assert resolved.provenance["description"] == "online:test"
    assert resolved.provenance["machine"] == "online:test"
    assert resolved.provenance["art"] == "local box scan"
    assert len(resolved.sidecars) == 3
    assert {source["source"] for source in resolved.sources} == {
        "online:test",
        f"sidecar:{disk.with_suffix(disk.suffix + '.json')}",
        f"sidecar:{disk.with_suffix('.json')}",
    }


def test_local_metadata_beats_online_and_filename(tmp_path: Path) -> None:
    root = tmp_path / "games"
    root.mkdir()
    disk = root / "Zarch (1987)(Superior)(Action).st"
    disk.write_bytes(b"ST" * 20)
    entry = DiskEntry.from_path(disk, root)
    state = tmp_path / "state"
    _write(
        state / "metadata" / f"{entry.id}.json",
        {"publisher": "Online", "genre": "OnlineGenre", "provenance": {"source": "online:x"}},
    )
    _write(
        disk.with_suffix(disk.suffix + ".json"),
        {"publisher": "Local", "genre": "Puzzle"},
    )
    resolver = MetadataResolver(state_dir=state)
    resolved = resolver.resolve(entry)
    assert resolved.publisher == "Local"
    assert resolved.genre == "Puzzle"
    assert resolved.year == 1987
    assert resolved.provenance["publisher"].startswith("sidecar:")
    # A field only supplied by the filename keeps filename provenance.
    assert resolved.provenance["year"] == "filename"


def test_cached_art_is_reported_with_source(tmp_path: Path) -> None:
    root = tmp_path / "games"
    root.mkdir()
    disk = root / "No Art Game (1992)(Nobody).st"
    disk.write_bytes(b"ST" * 20)
    entry = DiskEntry.from_path(disk, root)
    state = tmp_path / "state"
    cache = state / "cache" / "art"
    cache.mkdir(parents=True)
    cached = cache / f"{entry.id}.png"
    cached.write_bytes(b"cached")

    resolver = MetadataResolver(state_dir=state, cache_dir=cache)
    resolved = resolver.resolve(entry)
    assert resolved.art_path == cached.resolve()
    assert resolved.art_source == f"cache:{cached}"
    assert resolved.art_source.startswith("cache:")
    assert resolved.provenance["art"] == resolved.art_source
