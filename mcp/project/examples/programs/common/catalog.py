"""Lazy, background-friendly discovery of the Atari game corpus.

Tk must never wait for a walk of 13,000 files. ``Catalog`` scans on a daemon
thread in small batches, exposes the entries discovered so far, and has simple
pagination/search helpers. Sidecar metadata is loaded during discovery; online
enrichment is an optional callback that runs after discovery and never blocks
application startup.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

try:  # package import
    from . import config as config_module
    from . import paths
except ImportError:  # direct script import
    import config as config_module  # type: ignore[no-redef]
    import paths  # type: ignore[no-redef]

DISK_SUFFIXES = {".st", ".msa", ".stx", ".zip"}
GENRES = {
    "action",
    "adventure",
    "arcade",
    "board",
    "card",
    "compilation",
    "demo",
    "educational",
    "gambling",
    "game",
    "music",
    "puzzle",
    "racing",
    "rpg",
    "simulation",
    "sports",
    "strategy",
    "utility",
    "misc",
}
_TECH_GROUP_RE = re.compile(
    r"^(?:disk|part|side)\b|^(?:st|ste|stf|stfm|ste|tt|falcon|aga|ecs|ocs|ntsc|pal)$|"
    r"^(?:en|de|fr|es|it|nl|se|sf|sg|uk|us|cz|pl)$|^v?\d+(?:\.\d+)*$|"
    r"^(?:a|a\d+|alt|cr|m|f|h|t|b|p)\b",
    re.IGNORECASE,
)
_YEAR_RE = re.compile(r"\((19\d{2}|20\d{2}|19xx|\?\?\?\?)\)", re.IGNORECASE)
_GROUP_RE = re.compile(r"\(([^()]*)\)")
_BRACKET_RE = re.compile(r"\[([^\]]*)\]")
_DISK_RE = re.compile(r"\bdisk\s+(\d+)\s+of\s+(\d+)\b", re.IGNORECASE)
_DISK_SINGLE_RE = re.compile(r"\bdisk\s+(\d+)\b", re.IGNORECASE)
_PART_RE = re.compile(r"\bpart\s+([A-Za-z0-9]+)\b", re.IGNORECASE)


@dataclass(frozen=True)
class TosecName:
    title: str
    year: int | None = None
    year_raw: str | None = None
    publisher: str | None = None
    genre: str | None = None
    disk: int | None = None
    disk_total: int | None = None
    part: str | None = None
    tags: tuple[str, ...] = ()


def _clean_title(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip(" .-_")
    return text or "Untitled"


def parse_tosec_name(filename: str | Path) -> TosecName:
    """Parse a TOSEC-style filename into searchable fields.

    Handles menu disks with ``(Disk 1 of 3)(Part A)``, no-year entries,
    alternate/cracked/modified bracket tags, and compilations joined by ``&``.
    The title keeps the full compilation name; fields are best-effort.
    """
    raw_name = Path(str(filename)).name
    stem = Path(raw_name).stem if Path(raw_name).suffix else raw_name
    tags = tuple(tag.strip() for tag in _BRACKET_RE.findall(stem) if tag.strip())
    clean = _BRACKET_RE.sub(" ", stem)

    year: int | None = None
    year_raw: str | None = None
    year_match = _YEAR_RE.search(clean)
    if year_match:
        title = _clean_title(clean[: year_match.start()])
        year_raw = year_match.group(1)
        if year_raw.isdigit():
            year = int(year_raw)
        tail = clean[year_match.end() :]
    else:
        title = _clean_title(clean)
        tail = ""

    groups = [group.strip() for group in _GROUP_RE.findall(tail)]
    publisher: str | None = None
    genre: str | None = None
    disk: int | None = None
    disk_total: int | None = None
    part: str | None = None

    for group in groups:
        lowered = group.casefold()
        disk_match = _DISK_RE.search(group)
        if disk_match:
            disk = int(disk_match.group(1))
            disk_total = int(disk_match.group(2))
            continue
        single = _DISK_SINGLE_RE.search(group)
        if single and not lowered.startswith("disk of"):
            disk = int(single.group(1))
            continue
        part_match = _PART_RE.search(group)
        if part_match:
            part = part_match.group(1)
            continue
        if lowered in GENRES:
            genre = group
            continue
        if _TECH_GROUP_RE.match(group):
            continue
        if publisher is None:
            publisher = group

    return TosecName(
        title=title,
        year=year,
        year_raw=year_raw,
        publisher=publisher,
        genre=genre,
        disk=disk,
        disk_total=disk_total,
        part=part,
        tags=tags,
    )


def stable_id(path: str | Path) -> str:
    """Deterministic 16-hex ID derived from the resolved host path."""
    resolved = str(Path(path).expanduser().resolve()).casefold()
    return hashlib.sha1(resolved.encode("utf-8")).hexdigest()[:16]


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


@dataclass
class DiskEntry:
    id: str
    path: Path
    root: Path
    relative: str
    filename: str
    title: str
    year: int | None
    year_raw: str | None
    publisher: str | None
    genre: str | None
    disk: int | None
    disk_total: int | None
    part: str | None
    tags: tuple[str, ...]
    size: int
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_path(cls, path: Path, root: Path) -> "DiskEntry":
        resolved = path.resolve()
        parsed = parse_tosec_name(path.name)
        try:
            relative = str(resolved.relative_to(root.resolve()))
        except ValueError:
            relative = path.name
        entry = cls(
            id=stable_id(resolved),
            path=resolved,
            root=root.resolve(),
            relative=relative,
            filename=path.name,
            title=parsed.title,
            year=parsed.year,
            year_raw=parsed.year_raw,
            publisher=parsed.publisher,
            genre=parsed.genre,
            disk=parsed.disk,
            disk_total=parsed.disk_total,
            part=parsed.part,
            tags=parsed.tags,
            size=resolved.stat().st_size if resolved.is_file() else 0,
        )
        return entry

    @property
    def display_name(self) -> str:
        name = self.title
        if self.disk is not None and self.disk_total:
            name += f" (Disk {self.disk}/{self.disk_total})"
        elif self.part:
            name += f" (Part {self.part})"
        if self.year:
            name += f" ({self.year})"
        return name

    @property
    def search_text(self) -> str:
        return " ".join(
            str(value)
            for value in (
                self.title,
                self.year or "",
                self.publisher or "",
                self.genre or "",
                self.filename,
                self.relative,
            )
            if value
        ).casefold()

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "path": str(self.path),
            "root": str(self.root),
            "relative": self.relative,
            "filename": self.filename,
            "title": self.title,
            "year": self.year,
            "year_raw": self.year_raw,
            "publisher": self.publisher,
            "genre": self.genre,
            "disk": self.disk,
            "disk_total": self.disk_total,
            "part": self.part,
            "tags": list(self.tags),
            "size": self.size,
            "metadata": self.metadata,
        }


class Catalog:
    """Discover disk images under one or more roots without blocking Tk."""

    def __init__(
        self,
        roots: Iterable[str | Path] | None = None,
        *,
        state_dir: str | Path | None = None,
        metadata_dir: str | Path | None = None,
        enrich: Callable[[DiskEntry], dict[str, Any] | None] | None = None,
        batch_size: int = 200,
    ):
        self.roots = (
            [Path(root).expanduser().resolve() for root in roots]
            if roots is not None
            else paths.game_roots()
        )
        self.state_dir = (
            Path(state_dir).expanduser().resolve() if state_dir else paths.state_dir()
        )
        self.metadata_dir = (
            Path(metadata_dir).expanduser().resolve()
            if metadata_dir
            else self.state_dir / "metadata"
        )
        self.enrich = enrich
        self.batch_size = max(1, int(batch_size))
        self._lock = threading.RLock()
        self._entries: list[DiskEntry] = []
        self._by_id: dict[str, DiskEntry] = {}
        self._seen_paths: set[Path] = set()
        self._scan_thread: threading.Thread | None = None
        self._enrich_thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._done = threading.Event()
        self._error: str | None = None
        self._scanned_files = 0

    # -- discovery -----------------------------------------------------

    def start_scan(self) -> "Catalog":
        with self._lock:
            if self._scan_thread is not None and self._scan_thread.is_alive():
                return self
            if self._done.is_set():
                return self
            self._scan_thread = threading.Thread(
                target=self._scan_worker, name="example-catalog-scan", daemon=True
            )
            self._scan_thread.start()
            return self

    def scan(self, *, timeout: float | None = None) -> "Catalog":
        """Run discovery synchronously; mostly useful for tests and CLI tools."""
        self.start_scan()
        self._done.wait(timeout)
        return self

    def wait(self, timeout: float | None = None) -> bool:
        return self._done.wait(timeout)

    def stop(self) -> None:
        self._stop.set()
        for thread in (self._scan_thread, self._enrich_thread):
            if thread is not None and thread.is_alive():
                thread.join(timeout=2.0)

    def _scan_worker(self) -> None:
        try:
            since_yield = 0
            for root in self.roots:
                if self._stop.is_set():
                    break
                if not root.is_dir():
                    continue
                for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
                    if self._stop.is_set():
                        break
                    dirnames.sort()
                    for filename in sorted(filenames):
                        suffix = Path(filename).suffix.lower()
                        if suffix not in DISK_SUFFIXES:
                            continue
                        path = Path(dirpath) / filename
                        try:
                            resolved = path.resolve()
                        except OSError:
                            continue
                        with self._lock:
                            if resolved in self._seen_paths:
                                continue
                            self._seen_paths.add(resolved)
                        try:
                            entry = DiskEntry.from_path(path, root)
                        except OSError:
                            continue
                        entry.metadata = self._load_metadata(entry)
                        with self._lock:
                            if entry.id in self._by_id:
                                continue
                            self._entries.append(entry)
                            self._by_id[entry.id] = entry
                            self._scanned_files += 1
                        since_yield += 1
                        if since_yield >= self.batch_size:
                            since_yield = 0
                            time.sleep(0)
        except BaseException as exc:  # noqa: BLE001 - reported to the UI
            self._error = f"{type(exc).__name__}: {exc}"
        finally:
            self._done.set()
            if self.enrich is not None and not self._stop.is_set():
                self.start_enrichment()

    # -- metadata ------------------------------------------------------

    def _metadata_candidates(self, entry: DiskEntry) -> list[Path]:
        return [
            entry.path.with_suffix(entry.path.suffix + ".json"),
            entry.path.with_suffix(".json"),
            self.metadata_dir / f"{entry.id}.json",
        ]

    def _load_metadata(self, entry: DiskEntry) -> dict[str, Any]:
        merged: dict[str, Any] = {}
        try:
            self.metadata_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        for candidate in self._metadata_candidates(entry):
            if candidate.is_file():
                merged.update(_load_json(candidate))
        return merged

    def record_enrichment(
        self,
        entry_or_id: DiskEntry | str,
        values: dict[str, Any],
        *,
        source: str = "online",
    ) -> dict[str, Any]:
        """Persist enrichment with provenance; safe to call from any thread."""
        entry = entry_or_id if isinstance(entry_or_id, DiskEntry) else self.get(str(entry_or_id))
        if entry is None:
            raise KeyError(f"unknown catalog entry: {entry_or_id}")
        payload = dict(values)
        provenance = dict(payload.get("provenance") or {})
        provenance.setdefault("source", source)
        provenance.setdefault("fetched_at", int(time.time()))
        payload["provenance"] = provenance
        self.metadata_dir.mkdir(parents=True, exist_ok=True)
        target = self.metadata_dir / f"{entry.id}.json"
        existing = _load_json(target)
        existing.update(payload)
        config_module.atomic_write_json(target, existing)
        with self._lock:
            entry.metadata.update(payload)
        return existing

    def start_enrichment(
        self, enricher: Callable[[DiskEntry], dict[str, Any] | None] | None = None
    ) -> "Catalog":
        if enricher is not None:
            self.enrich = enricher
        if self.enrich is None:
            return self
        with self._lock:
            if self._enrich_thread is not None and self._enrich_thread.is_alive():
                return self
            self._enrich_thread = threading.Thread(
                target=self._enrich_worker, name="example-catalog-enrich", daemon=True
            )
            self._enrich_thread.start()
        return self

    def _enrich_worker(self) -> None:
        assert self.enrich is not None
        for entry in self.iter_entries():
            if self._stop.is_set():
                return
            if entry.metadata.get("provenance"):
                continue
            try:
                values = self.enrich(entry)
            except BaseException:
                continue
            if isinstance(values, dict) and values:
                with contextlib.suppress(Exception):
                    self.record_enrichment(entry, values)

    # -- querying ------------------------------------------------------

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def __iter__(self) -> Iterator[DiskEntry]:
        return self.iter_entries()

    @property
    def error(self) -> str | None:
        return self._error

    @property
    def scanning(self) -> bool:
        thread = self._scan_thread
        return bool(thread is not None and thread.is_alive())

    @property
    def ready(self) -> bool:
        return self._done.is_set()

    @property
    def scanned_files(self) -> int:
        with self._lock:
            return self._scanned_files

    def get(self, entry_id: str) -> DiskEntry | None:
        with self._lock:
            return self._by_id.get(entry_id)

    def snapshot(self) -> list[DiskEntry]:
        with self._lock:
            return list(self._entries)

    def iter_entries(self, query: str | None = None) -> Iterator[DiskEntry]:
        terms = [term for term in (query or "").casefold().split() if term]
        for entry in self.snapshot():
            if not terms or all(term in entry.search_text for term in terms):
                yield entry

    def search(self, query: str = "", *, limit: int | None = None) -> list[DiskEntry]:
        results: list[DiskEntry] = []
        for entry in self.iter_entries(query):
            results.append(entry)
            if limit is not None and len(results) >= limit:
                break
        return results

    def get_page(
        self,
        offset: int = 0,
        limit: int = 50,
        *,
        query: str | None = None,
        sort: str = "title",
    ) -> list[DiskEntry]:
        offset = max(0, int(offset))
        limit = max(0, int(limit))
        entries = list(self.iter_entries(query))
        key = {
            "title": lambda item: (item.title.casefold(), item.year or 0, item.filename.casefold()),
            "year": lambda item: (item.year or 0, item.title.casefold()),
            "path": lambda item: item.relative.casefold(),
        }.get(sort)
        if key is not None:
            entries.sort(key=key)
        return entries[offset : offset + limit]

    def count(self, query: str | None = None) -> int:
        return sum(1 for _ in self.iter_entries(query))


def discover(
    roots: Iterable[str | Path] | None = None,
    *,
    wait: bool = False,
    timeout: float | None = None,
    **kwargs: Any,
) -> Catalog:
    catalog = Catalog(roots, **kwargs)
    catalog.start_scan()
    if wait:
        catalog.wait(timeout)
    return catalog
