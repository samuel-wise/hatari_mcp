"""Catalog paging/search plus machine/genre/year filter tests."""

from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[3]
PROGRAMS = PROJECT / "examples" / "programs"
if str(PROGRAMS) not in sys.path:
    sys.path.insert(0, str(PROGRAMS))

from common import Catalog  # noqa: E402
from game_launcher.library import (  # noqa: E402
    FilterState,
    GameLibrary,
    filters_from_values,
    year_decade,
)
from game_launcher.metadata import MetadataResolver  # noqa: E402


def _write(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def _library(tmp_path: Path) -> GameLibrary:
    root = tmp_path / "games"
    root.mkdir()
    for name in (
        "Alpha (1990)(Pub)(Action).st",
        "Beta (1995)(Other)(Sports).st",
        "Gamma (19xx)(Pub)(Puzzle).st",
        "Delta (1987)(Pub)(Action).st",
    ):
        (root / name).write_bytes(b"ST" * 20)
    _write(
        root / "Beta (1995)(Other)(Sports).st.json",
        {"genre": "Strategy", "year": 1996, "machine": "ste"},
    )
    _write(
        root / "Gamma (19xx)(Pub)(Puzzle).st.json",
        {"machine": "tt"},
    )
    state = tmp_path / "state"
    catalog = Catalog(
        [root],
        state_dir=state,
        metadata_dir=state / "metadata",
    ).scan(timeout=10)
    resolver = MetadataResolver(
        state_dir=state,
        metadata_dir=state / "metadata",
        cache_dir=state / "cache" / "art",
    )
    return GameLibrary(catalog, metadata=resolver)


def test_search_filters_paging_and_facets(tmp_path: Path) -> None:
    library = _library(tmp_path)

    assert [entry.title for entry in library.filter_entries(FilterState(query="alpha"))] == [
        "Alpha"
    ]
    assert [entry.title for entry in library.filter_entries(FilterState(query="other"))] == [
        "Beta"
    ]

    ste = library.filter_entries(FilterState(machine="ste"))
    assert [entry.title for entry in ste] == ["Beta"]
    assert library.machine(ste[0]) == ("ste", "sidecar")

    puzzle = library.filter_entries(FilterState(genre="Puzzle"))
    assert [entry.title for entry in puzzle] == ["Gamma"]

    nineties = library.filter_entries(FilterState(decade="1990s"))
    assert {entry.title for entry in nineties} == {"Alpha", "Beta"}
    assert year_decade(1987) == "1980s"
    assert year_decade(None) == "Unknown"

    first_page, total = library.page(FilterState(), offset=0, limit=2)
    second_page, total_again = library.page(FilterState(), offset=2, limit=2)
    assert total == total_again == 4
    assert len(first_page) == len(second_page) == 2
    assert {entry.id for entry in first_page}.isdisjoint(
        {entry.id for entry in second_page}
    )

    facets = library.facets()
    assert facets["machine"] == ["st", "ste", "tt"]
    assert facets["genre"] == ["Action", "Puzzle", "Strategy"]
    assert facets["decade"] == ["1980s", "1990s", "Unknown"]

    filters = filters_from_values("  beta ", "Any", "Strategy", "1990s")
    assert filters.query == "beta"
    assert filters.machine is None
    assert filters.genre == "Strategy"
    assert filters.decade == "1990s"
    assert not filters.is_empty()

    # Search is backed by Catalog.search, not a second filename parser.
    assert library.catalog.count("pub") == 3
