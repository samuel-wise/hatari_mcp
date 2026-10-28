"""Offline tests for the on-demand genre cache."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

PROJECT = Path(__file__).resolve().parents[3]
PROGRAMS = PROJECT / "examples" / "programs"
if str(PROGRAMS) not in sys.path:
    sys.path.insert(0, str(PROGRAMS))

from game_launcher import genres  # noqa: E402
from game_launcher.genres import GenreResolver  # noqa: E402

ENTRY = SimpleNamespace(id="game-1", title="Defender of the Crown")
METADATA = SimpleNamespace(title="Defender of the Crown", genre=None)


def test_genre_lookup_is_cached(tmp_path: Path, monkeypatch) -> None:
    calls: list[str] = []

    def fake_lookup(title: str, *, timeout: float = 8.0) -> str:
        calls.append(title)
        return "Strategy / Simulation"

    monkeypatch.setattr(genres, "_wikipedia_genre", fake_lookup)
    resolver = GenreResolver(tmp_path)

    assert resolver.lookup(ENTRY, METADATA) == ("Strategy / Simulation", "wikipedia")
    assert calls == ["Defender of the Crown"]
    assert resolver.cached("game-1") == "Strategy / Simulation"

    reloaded = GenreResolver(tmp_path)
    assert reloaded.lookup(ENTRY, METADATA) == ("Strategy / Simulation", "wikipedia")
    assert calls == ["Defender of the Crown"]
    assert reloaded.cached("other-disk", "Defender of the Crown") == (
        "Strategy / Simulation"
    )
    other = SimpleNamespace(id="game-2", title="Defender of the Crown")
    assert reloaded.lookup(other, METADATA) == ("Strategy / Simulation", "wikipedia")
    assert calls == ["Defender of the Crown"]


def test_mobygames_preferred_when_api_key_is_set(tmp_path: Path, monkeypatch) -> None:
    calls: list[tuple[int, str]] = []

    def fake_moby(game_id: int, api_key: str, *, timeout: float = 8.0) -> str:
        calls.append((game_id, api_key))
        return "Strategy / Tactics"

    def fail(*_args, **_kwargs):
        raise AssertionError("wikipedia should not run when MobyGames succeeds")

    monkeypatch.setattr(genres, "_mobygames_genre", fake_moby)
    monkeypatch.setattr(genres, "_wikipedia_genre", fail)
    metadata = SimpleNamespace(
        title="Defender of the Crown", genre=None, mobygames=181
    )
    resolver = GenreResolver(tmp_path, mobygames_key="test-key")

    assert resolver.lookup(ENTRY, metadata) == ("Strategy / Tactics", "mobygames")
    assert calls == [(181, "test-key")]


def test_mobygames_requires_an_explicit_key(tmp_path: Path, monkeypatch) -> None:
    def fail(*_args, **_kwargs):
        raise AssertionError("mobygames must not be called without a key")

    monkeypatch.setattr(genres, "_mobygames_genre", fail)
    monkeypatch.setattr(genres, "_wikipedia_genre", lambda *_a, **_k: "Strategy")
    metadata = SimpleNamespace(
        title="Defender of the Crown", genre=None, mobygames=181
    )
    resolver = GenreResolver(tmp_path)

    assert resolver.lookup(ENTRY, metadata) == ("Strategy", "wikipedia")


def test_metadata_genre_skips_lookup(tmp_path: Path, monkeypatch) -> None:
    def fail(*_args, **_kwargs):
        raise AssertionError("lookup should not run")

    monkeypatch.setattr(genres, "_wikipedia_genre", fail)
    resolver = GenreResolver(tmp_path)
    metadata = SimpleNamespace(title="Defender of the Crown", genre="Action")

    assert resolver.lookup(ENTRY, metadata) is None


def test_genre_cleaning_and_splitting() -> None:
    assert genres._split_genres("[[Strategy game|Strategy]] and [[Simulation]]") == [
        "Strategy",
        "Simulation",
    ]
    assert genres._split_genres("Action, Shooter; Racing") == [
        "Action",
        "Shooter",
        "Racing",
    ]
