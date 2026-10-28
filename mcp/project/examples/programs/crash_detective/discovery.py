"""Small catalog/PRG selection helpers used by the crash detective UI."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

try:  # package import
    from common import paths
    from common.catalog import DiskEntry, parse_tosec_name, stable_id
except ImportError:  # direct script import
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from common import paths  # type: ignore[no-redef]
    from common.catalog import DiskEntry, parse_tosec_name, stable_id  # type: ignore[no-redef]


@dataclass(frozen=True)
class PrgTarget:
    id: str
    path: Path
    title: str
    filename: str
    size: int
    sha256: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "path": str(self.path),
            "title": self.title,
            "filename": self.filename,
            "size": self.size,
            "sha256": self.sha256,
        }


def _value(entry: Any, key: str, default: Any = None) -> Any:
    if isinstance(entry, dict):
        return entry.get(key, default)
    return getattr(entry, key, default)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(256 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def prg_target(path: str | Path) -> PrgTarget:
    source = Path(path).expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(f"PRG does not exist: {source}")
    if source.suffix.lower() != ".prg":
        raise ValueError(f"not a .prg file: {source}")
    parsed = parse_tosec_name(source.name)
    return PrgTarget(
        id=stable_id(source),
        path=source,
        title=parsed.title or source.stem,
        filename=source.name,
        size=source.stat().st_size,
        sha256=_sha256(source),
    )


def filter_entries(
    entries: Iterable[Any],
    query: str = "",
    *,
    year: int | None = None,
    min_year: int | None = None,
    max_year: int | None = None,
    publisher: str | None = None,
    genre: str | None = None,
    disk: int | None = None,
    sort: str = "title",
    limit: int | None = None,
) -> list[Any]:
    """Filter catalog entries for the UI (and for tests).

    Query terms are case-insensitive and must all appear in the entry's
    ``search_text`` (title, year, publisher, genre, filename and relative path).
    The function accepts ``DiskEntry`` objects or plain dictionaries.
    """
    terms = [term for term in (query or "").casefold().split() if term]
    publisher_term = (publisher or "").casefold().strip()
    genre_term = (genre or "").casefold().strip()
    matches: list[Any] = []
    for entry in entries:
        search_text = str(
            _value(entry, "search_text")
            or " ".join(
                str(_value(entry, key) or "")
                for key in ("title", "year", "publisher", "genre", "filename", "relative", "path")
            )
        ).casefold()
        if terms and not all(term in search_text for term in terms):
            continue
        entry_year = _value(entry, "year")
        if year is not None and entry_year != year:
            continue
        if min_year is not None and (not isinstance(entry_year, int) or entry_year < min_year):
            continue
        if max_year is not None and (not isinstance(entry_year, int) or entry_year > max_year):
            continue
        if publisher_term and publisher_term not in str(_value(entry, "publisher") or "").casefold():
            continue
        if genre_term and genre_term not in str(_value(entry, "genre") or "").casefold():
            continue
        if disk is not None and _value(entry, "disk") != disk:
            continue
        matches.append(entry)

    def sort_key(entry: Any) -> Any:
        title = str(_value(entry, "title") or "").casefold()
        entry_year = _value(entry, "year")
        filename = str(_value(entry, "filename") or _value(entry, "path") or "").casefold()
        if sort == "year":
            return (entry_year or 0, title)
        if sort == "path":
            return str(_value(entry, "relative") or _value(entry, "path") or "").casefold()
        return (title, entry_year or 0, filename)

    matches.sort(key=sort_key)
    if limit is not None:
        matches = matches[: max(0, int(limit))]
    return matches


def catalog_roots(extra: Sequence[str | Path] | None = None) -> list[Path]:
    roots: list[Path] = [paths.default_game_root()]
    for item in extra or ():
        try:
            path = Path(item).expanduser().resolve()
        except (OSError, RuntimeError):
            continue
        if path not in roots:
            roots.append(path)
    return roots


def make_entry(path: str | Path, root: str | Path | None = None) -> DiskEntry:
    source = Path(path).expanduser().resolve()
    base = Path(root).expanduser().resolve() if root else source.parent
    return DiskEntry.from_path(source, base)
