"""Catalog filtering and PRG selection tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from crash_detective.discovery import catalog_roots, filter_entries, make_entry, prg_target


@pytest.fixture
def entries(tmp_path: Path):
    root = tmp_path / "games"
    root.mkdir()
    files = {
        "Alpha (1990)(Publisher A)(Action).st": b"a",
        "Beta (1995)(Publisher B)(Puzzle).msa": b"bb",
        "Gamma (1990)(Publisher A)(Action)(Disk 2 of 2).st": b"ccc",
        "Delta (19xx)(Publisher C)(Racing)[a].zip": b"dddd",
    }
    for name, data in files.items():
        (root / name).write_bytes(data)
    return [make_entry(root / name, root) for name in files]


def test_filter_entries_query_year_publisher_genre_and_disk(entries) -> None:
    assert len(filter_entries(entries)) == 4

    alpha = filter_entries(entries, "alpha")
    assert [entry.title for entry in alpha] == ["Alpha"]

    by_publisher = filter_entries(entries, publisher="publisher a")
    assert {entry.title for entry in by_publisher} == {"Alpha", "Gamma"}

    by_genre = filter_entries(entries, genre="action")
    assert {entry.title for entry in by_genre} == {"Alpha", "Gamma"}

    assert [entry.title for entry in filter_entries(entries, year=1995)] == ["Beta"]
    assert [entry.title for entry in filter_entries(entries, min_year=1990, max_year=1995)][0] == "Alpha"
    assert [entry.title for entry in filter_entries(entries, disk=2)] == ["Gamma"]
    assert filter_entries(entries, "publisher b puzzle")[0].title == "Beta"
    assert filter_entries(entries, "no-such-term") == []


def test_filter_entries_sort_and_limit(entries) -> None:
    titles = [entry.title for entry in filter_entries(entries, sort="title")]
    assert titles == ["Alpha", "Beta", "Delta", "Gamma"]

    by_year = filter_entries(entries, sort="year")
    assert by_year[0].year is None
    assert by_year[-1].title == "Beta"

    limited = filter_entries(entries, sort="title", limit=2)
    assert [entry.title for entry in limited] == ["Alpha", "Beta"]


def test_prg_target_and_catalog_roots(tmp_path: Path) -> None:
    prg = tmp_path / "Crash Test (1992)(Demo).prg"
    prg.write_bytes(b"\x60\x1a" + b"\0" * 30)
    target = prg_target(prg)
    assert target.path == prg.resolve()
    assert target.title.startswith("Crash Test")
    assert target.size == prg.stat().st_size
    assert len(target.sha256) == 64

    text_file = tmp_path / "not-a-prg.txt"
    text_file.write_text("not a program", encoding="utf-8")
    with pytest.raises(ValueError):
        prg_target(text_file)

    roots = catalog_roots([tmp_path, tmp_path])
    assert roots[0] == roots[0].resolve()
    assert roots.count(tmp_path.resolve()) == 1
