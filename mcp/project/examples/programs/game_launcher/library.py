"""Filtering, paging and facets over the shared background catalog."""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

_PROGRAMS = Path(__file__).resolve().parent.parent
if str(_PROGRAMS) not in sys.path:
    sys.path.insert(0, str(_PROGRAMS))

from common import catalog as catalog_module  # noqa: E402

from . import profiles  # noqa: E402
from .metadata import MetadataResolver, ResolvedMetadata  # noqa: E402

GENRE_LABELS = {genre.casefold(): genre for genre in sorted(catalog_module.GENRES)}


@dataclass(frozen=True)
class FilterState:
    query: str = ""
    machine: str | None = None
    genre: str | None = None
    decade: str | None = None

    def is_empty(self) -> bool:
        return not self.query and not self.machine and not self.genre and not self.decade

    def describe(self) -> str:
        parts: list[str] = []
        if self.query:
            parts.append(f"search={self.query!r}")
        if self.machine:
            parts.append(f"machine={self.machine}")
        if self.genre:
            parts.append(f"genre={self.genre}")
        if self.decade:
            parts.append(f"year={self.decade}")
        return ", ".join(parts) or "no filters"


def year_decade(year: int | None) -> str:
    if not year:
        return "Unknown"
    return f"{(year // 10) * 10}s"


def launch_status(records: list[dict[str, Any]]) -> str:
    """Classify records as success, failed, regressed (was green) or untested."""
    observations: list[tuple[int, bool]] = []
    has_success = False
    for record in records:
        if not isinstance(record, dict):
            continue
        if record.get("last_success") or record.get("last_success_config"):
            has_success = True
        last_result = record.get("last_result")
        if not isinstance(last_result, dict):
            continue
        at = last_result.get("at")
        try:
            timestamp = int(at) if at is not None else 0
        except (TypeError, ValueError):
            timestamp = 0
        ok = bool(last_result.get("ok"))
        observations.append((timestamp, ok))
        if ok:
            has_success = True
    if observations:
        observations.sort(key=lambda item: item[0])
        if observations[-1][1]:
            return "success"
        return "regressed" if has_success else "failed"
    return "success" if has_success else "untested"


def _entry_value(entry: Any, key: str, default: Any = None) -> Any:
    if isinstance(entry, dict):
        return entry.get(key, default)
    return getattr(entry, key, default)


def sort_entries(entries: list[Any], sort: str = "title") -> list[Any]:
    if sort == "year":
        key: Callable[[Any], Any] = lambda item: (
            _entry_value(item, "year") or 0,
            str(_entry_value(item, "title", "")).casefold(),
        )
    elif sort == "path":
        key = lambda item: str(_entry_value(item, "relative", "")).casefold()
    else:
        key = lambda item: (
            str(_entry_value(item, "title", "")).casefold(),
            _entry_value(item, "year") or 0,
            str(_entry_value(item, "filename", "")).casefold(),
        )
    return sorted(entries, key=key)


_DISK_MARKER_RE = re.compile(r"\((?:Disk|Part)\s+[^)]*\)", re.IGNORECASE)


def _disk_order(entry: Any) -> tuple[int, str, str]:
    return (
        int(_entry_value(entry, "disk") or 0),
        str(_entry_value(entry, "part") or "").casefold(),
        str(_entry_value(entry, "filename", "")).casefold(),
    )


def _release_suffix(entry: Any) -> str:
    filename = str(_entry_value(entry, "filename", "") or "")
    match = _DISK_MARKER_RE.search(filename)
    return filename[match.end() :].strip().casefold() if match else ""


def _group_key(entry: Any, title_of: Callable[[Any], str] | None = None) -> str:
    disk = _entry_value(entry, "disk")
    part = _entry_value(entry, "part")
    if disk is None and part is None:
        return f"single:{_entry_value(entry, 'id', '')}"
    title = title_of(entry) if title_of else _entry_value(entry, "title", "")
    values = (
        str(_entry_value(entry, "root", "")),
        str(title).casefold(),
        str(_entry_value(entry, "year") or ""),
        str(_entry_value(entry, "publisher", "") or "").casefold(),
        _release_suffix(entry),
    )
    return "group:" + "|".join(values)


def _normalise_title(value: Any) -> str:
    text = re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold())
    return " ".join(text.split())


_VARIANT_SKIP = {"one disk"}


def variant_label(group: "GameGroup") -> str:
    """Return meaningful release tags, e.g. ``[cr BOSS]`` or ``[tr fr partial]``."""
    tags = getattr(group.primary, "tags", ()) or ()
    labels = [
        f"[{str(tag).strip()}]"
        for tag in tags
        if str(tag).strip() and str(tag).strip().casefold() not in _VARIANT_SKIP
    ]
    return " ".join(labels) or "original"


def _release_order(group: "GameGroup") -> tuple[int, str]:
    tags = getattr(group.primary, "tags", ()) or ()
    return (0 if not tags else 1, variant_label(group).casefold())


@dataclass(frozen=True)
class GameGroup:
    key: str
    title: str
    year: int | None
    entries: tuple[Any, ...]
    primary: Any
    multi: bool

    @property
    def disk_count(self) -> int:
        return len(self.entries)

    @property
    def disk_total(self) -> int:
        expected = max(
            (int(_entry_value(entry, "disk_total") or 0) for entry in self.entries),
            default=0,
        )
        return max(len(self.entries), expected, 1)

    @property
    def sort_entry(self) -> Any:
        return self.primary


@dataclass(frozen=True)
class TitleGroup:
    key: str
    title: str
    year: int | None
    releases: tuple[GameGroup, ...]
    primary_release: GameGroup
    multi: bool

    @property
    def primary(self) -> Any:
        return self.primary_release.primary

    @property
    def release_count(self) -> int:
        return len(self.releases)

    @property
    def entries(self) -> tuple[Any, ...]:
        return tuple(entry for release in self.releases for entry in release.entries)


def title_key(title: Any) -> str:
    return "title:" + _normalise_title(title)


def group_titles(releases: list[GameGroup]) -> list[TitleGroup]:
    """Collapse all releases of the same title into one navigation group."""
    buckets: dict[str, list[GameGroup]] = {}
    order: list[str] = []
    for release in releases:
        key = title_key(release.title)
        if key not in buckets:
            buckets[key] = []
            order.append(key)
        buckets[key].append(release)

    groups: list[TitleGroup] = []
    for key in order:
        items = sorted(buckets[key], key=_release_order)
        primary_release = items[0]
        groups.append(
            TitleGroup(
                key=key,
                title=primary_release.title,
                year=primary_release.year,
                releases=tuple(items),
                primary_release=primary_release,
                multi=len(items) > 1,
            )
        )
    return groups


def group_entries(
    entries: list[Any],
    *,
    title_of: Callable[[Any], str] | None = None,
) -> list[GameGroup]:
    """Group multiple disks of one release while leaving singles untouched.

    Entries keep the catalog sort order; disks inside a group are ordered by
    disk number and part, then filename.  ``title_of`` lets callers group by
    resolved/KairoST titles instead of the raw filename title.
    """
    buckets: dict[str, list[Any]] = {}
    order: list[str] = []
    for entry in entries:
        key = _group_key(entry, title_of)
        if key not in buckets:
            buckets[key] = []
            order.append(key)
        buckets[key].append(entry)

    groups: list[GameGroup] = []
    for key in order:
        items = buckets[key]
        if len(items) > 1:
            items = sorted(items, key=_disk_order)
        primary = items[0]
        expected = int(_entry_value(primary, "disk_total") or 0)
        multi = len(items) > 1 or expected > 1
        groups.append(
            GameGroup(
                key=key,
                title=str(
                    title_of(primary) if title_of else _entry_value(primary, "title", "")
                ),
                year=_entry_value(primary, "year"),
                entries=tuple(items),
                primary=primary,
                multi=multi,
            )
        )
    return groups


class GameLibrary:
    """A filterable view over ``Catalog`` with cached sidecar resolution."""

    def __init__(
        self,
        catalog: catalog_module.Catalog,
        *,
        metadata: MetadataResolver | None = None,
        config: Any = None,
    ):
        self.catalog = catalog
        self.metadata = metadata or MetadataResolver()
        self.config = config
        self._resolved: dict[str, ResolvedMetadata] = {}
        self._machines: dict[str, tuple[str, str]] = {}
        self._entries_by_id: dict[str, Any] = {}
        self._groups_by_key: dict[str, GameGroup] = {}
        self._titles_by_key: dict[str, TitleGroup] = {}

    # -- metadata ------------------------------------------------------

    def resolved(self, entry: Any) -> ResolvedMetadata:
        entry_id = str(_entry_value(entry, "id", "") or "")
        value = self._resolved.get(entry_id)
        if value is None:
            value = self.metadata.resolve(entry)
            self._resolved[entry_id] = value
        return value

    def invalidate(self, entry_id: str | None = None) -> None:
        self.metadata.invalidate(entry_id)
        if entry_id is None:
            self._resolved.clear()
            self._machines.clear()
            self._groups_by_key.clear()
            self._titles_by_key.clear()
        else:
            self._resolved.pop(str(entry_id), None)
            self._machines.pop(str(entry_id), None)

    def machine(self, entry: Any) -> tuple[str, str]:
        entry_id = str(_entry_value(entry, "id", "") or "")
        cached = self._machines.get(entry_id)
        if cached is not None:
            return cached
        resolved = self.resolved(entry)
        value = profiles.detect_machine(entry, resolved.machine)
        self._machines[entry_id] = value
        return value

    def genre(self, entry: Any) -> str | None:
        resolved = self.resolved(entry)
        if resolved.genre:
            return resolved.genre
        return None

    def year(self, entry: Any) -> int | None:
        return self.resolved(entry).year

    # -- querying ------------------------------------------------------

    def _all(self) -> list[Any]:
        entries = self.catalog.snapshot()
        self._entries_by_id = {
            str(_entry_value(entry, "id", "") or ""): entry for entry in entries
        }
        return entries

    def filter_entries(
        self,
        state: FilterState | None = None,
        *,
        sort: str = "title",
    ) -> list[Any]:
        state = state or FilterState()
        # Catalog.search is the shared, background-safe query path.
        entries = list(self.catalog.search(state.query or ""))
        self._entries_by_id = {
            str(_entry_value(entry, "id", "") or ""): entry for entry in entries
        }
        if state.machine:
            wanted = state.machine.casefold()
            entries = [entry for entry in entries if self.machine(entry)[0] == wanted]
        if state.genre:
            wanted = state.genre.casefold()
            entries = [
                entry
                for entry in entries
                if (self.genre(entry) or "").casefold() == wanted
            ]
        if state.decade:
            entries = [
                entry
                for entry in entries
                if year_decade(self.year(entry)) == state.decade
            ]
        return sort_entries(entries, sort)

    def page(
        self,
        state: FilterState | None = None,
        *,
        offset: int = 0,
        limit: int = 100,
        sort: str = "title",
    ) -> tuple[list[Any], int]:
        entries = self.filter_entries(state, sort=sort)
        offset = max(0, int(offset))
        limit = max(0, int(limit))
        return entries[offset : offset + limit], len(entries)

    def count(self, state: FilterState | None = None) -> int:
        return len(self.filter_entries(state))

    def filter_groups(
        self,
        state: FilterState | None = None,
        *,
        sort: str = "title",
    ) -> list[GameGroup]:
        groups = group_entries(
            self.filter_entries(state, sort=sort),
            title_of=lambda entry: self.resolved(entry).title,
        )
        self._groups_by_key = {group.key: group for group in groups}
        return groups

    def page_groups(
        self,
        state: FilterState | None = None,
        *,
        offset: int = 0,
        limit: int = 100,
        sort: str = "title",
    ) -> tuple[list[GameGroup], int]:
        groups = self.filter_groups(state, sort=sort)
        offset = max(0, int(offset))
        limit = max(0, int(limit))
        return groups[offset : offset + limit], len(groups)

    def group(self, key: str) -> GameGroup | None:
        return self._groups_by_key.get(key)

    def filter_titles(
        self,
        state: FilterState | None = None,
        *,
        sort: str = "title",
    ) -> list[TitleGroup]:
        titles = group_titles(self.filter_groups(state, sort=sort))
        self._titles_by_key = {group.key: group for group in titles}
        return titles

    def page_titles(
        self,
        state: FilterState | None = None,
        *,
        offset: int = 0,
        limit: int = 100,
        sort: str = "title",
    ) -> tuple[list[TitleGroup], int]:
        titles = self.filter_titles(state, sort=sort)
        offset = max(0, int(offset))
        limit = max(0, int(limit))
        return titles[offset : offset + limit], len(titles)

    def title_group(self, key: str) -> TitleGroup | None:
        return self._titles_by_key.get(key)

    def get(self, entry_id: str) -> Any | None:
        return self._entries_by_id.get(str(entry_id))

    def facets(self, state: FilterState | None = None) -> dict[str, list[str]]:
        """Available filter values across the currently scanned catalog."""
        base = FilterState(query=state.query if state else "")
        entries = self.filter_entries(base)
        machines: set[str] = set()
        genres: set[str] = set()
        decades: set[str] = set()
        for entry in entries:
            machines.add(self.machine(entry)[0])
            genre = self.genre(entry)
            if genre:
                genres.add(genre)
            decades.add(year_decade(self.year(entry)))
        return {
            "machine": [item for item in profiles.MACHINES if item in machines],
            "genre": sorted(genres, key=str.casefold),
            "decade": sorted(decades, key=lambda value: (value == "Unknown", value)),
        }


def filters_from_values(
    query: str = "",
    machine: str | None = None,
    genre: str | None = None,
    decade: str | None = None,
) -> FilterState:
    def clean(value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value or value.casefold() in {"any", "all", "auto"}:
            return None
        return value

    return FilterState(
        query=query.strip(),
        machine=clean(machine),
        genre=clean(genre),
        decade=clean(decade),
    )
