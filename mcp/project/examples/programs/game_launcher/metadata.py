"""Local-first metadata resolution with per-field provenance.

TOSEC names are a useful but unreliable starting point.  This module merges
layers over the parsed filename:

1. parsed TOSEC fields (``filename``);
2. optional online enrichment values (``online:<provider>``);
3. the optional KairoST catalog (``kairost:<id>``), including local artwork;
4. hand-maintained local sidecars (``sidecar:<path>``).

Local sidecars win over KairoST, which wins over online values, which win over
filename guesses.  Art lookup is local-first: a sidecar or KairoST path wins
over the state cache, and remote KairoST art is used as a fallback.  A
deterministic placeholder is left to the UI when nothing exists.
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_PROGRAMS = Path(__file__).resolve().parent.parent
if str(_PROGRAMS) not in sys.path:
    sys.path.insert(0, str(_PROGRAMS))

from common import paths  # noqa: E402
from common.art import IMAGE_SUFFIXES  # noqa: E402

from .kairost import KairoSTCatalog  # noqa: E402

MACHINES = ("st", "ste", "tt", "falcon")
_MACHINE_ALIASES = {
    "st": "st",
    "stf": "st",
    "stfm": "st",
    "stfm/ste": "ste",
    "ste": "ste",
    "mega ste": "ste",
    "megaste": "ste",
    "tt": "tt",
    "tt030": "tt",
    "falcon": "falcon",
    "falcon030": "falcon",
    "ct2": "falcon",
}
_RESERVED_KEYS = {"provenance"}
_ONLINE_PREFIXES = ("online:", "http://", "https://", "remote:", "web:", "net:")
_YEAR_RE = re.compile(r"^(19\d{2}|20\d{2})$")
_RESOLUTION_RE = re.compile(r"^(\d{2,5})\s*[x×]\s*(\d{2,5})$", re.IGNORECASE)


@dataclass(frozen=True)
class ResolvedMetadata:
    entry_id: str
    title: str
    year: int | None
    year_raw: str | None
    publisher: str | None
    genre: str | None
    description: str | None
    developer: str | None
    players: str | None
    machine: str | None
    resolution: tuple[int, int] | None
    args: tuple[str, ...]
    art_path: Path | None
    art_source: str | None
    provenance: dict[str, str]
    art_url: str | None = None
    mobygames: int | None = None
    sources: list[dict[str, Any]] = field(default_factory=list)
    sidecars: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def search_text(self) -> str:
        values = (
            self.title,
            self.year or "",
            self.publisher or "",
            self.genre or "",
            self.description or "",
            self.developer or "",
            self.machine or "",
        )
        return " ".join(str(value) for value in values if value).casefold()

    def provenance_summary(self) -> str:
        if not self.provenance:
            return "filename (no sidecars)"
        return ", ".join(
            f"{key}={value}" for key, value in sorted(self.provenance.items())
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "entry_id": self.entry_id,
            "title": self.title,
            "year": self.year,
            "year_raw": self.year_raw,
            "publisher": self.publisher,
            "genre": self.genre,
            "description": self.description,
            "developer": self.developer,
            "players": self.players,
            "machine": self.machine,
            "resolution": list(self.resolution) if self.resolution else None,
            "args": list(self.args),
            "art_path": str(self.art_path) if self.art_path else None,
            "art_source": self.art_source,
            "art_url": self.art_url,
            "provenance": dict(self.provenance),
            "sources": list(self.sources),
            "sidecars": list(self.sidecars),
        }


def _entry_value(entry: Any, key: str, default: Any = None) -> Any:
    if isinstance(entry, dict):
        return entry.get(key, default)
    return getattr(entry, key, default)


def _text(value: Any) -> str | None:
    if isinstance(value, str):
        value = value.strip()
        return value or None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return None


def _normalise_year(value: Any) -> tuple[int | None, str | None]:
    if isinstance(value, bool) or value is None:
        return None, None
    if isinstance(value, int):
        return (value if 1900 <= value <= 2099 else None), str(value)
    text = str(value).strip()
    if not text:
        return None, None
    if _YEAR_RE.match(text):
        return int(text), text
    return None, text


def _normalise_resolution(value: Any) -> tuple[int, int] | None:
    if isinstance(value, (list, tuple)) and len(value) == 2:
        try:
            width, height = int(value[0]), int(value[1])
        except (TypeError, ValueError):
            return None
        return (width, height) if width > 0 and height > 0 else None
    if isinstance(value, dict):
        return _normalise_resolution([value.get("width"), value.get("height")])
    if isinstance(value, str):
        match = _RESOLUTION_RE.match(value.strip())
        if match:
            return int(match.group(1)), int(match.group(2))
    return None


def _normalise_args(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(str(item) for item in value if isinstance(item, (str, int, float)))


def _normalise_machine(value: Any) -> str | None:
    text = _text(value)
    if not text:
        return None
    return _MACHINE_ALIASES.get(text.casefold())


def _is_online_source(source: Any) -> bool:
    if not isinstance(source, str):
        return False
    lowered = source.strip().casefold()
    return any(lowered.startswith(prefix) for prefix in _ONLINE_PREFIXES)


def _normalise_art(value: Any, base_dir: Path) -> Path | None:
    candidate: str | None = None
    if isinstance(value, str):
        candidate = value
    elif isinstance(value, dict):
        for key in ("path", "file", "local"):
            item = value.get(key)
            if isinstance(item, str) and item:
                candidate = item
                break
    if not candidate:
        return None
    path = Path(candidate).expanduser()
    if not path.is_absolute():
        path = base_dir / path
    try:
        return path.resolve() if path.is_file() else None
    except OSError:
        return None


def _normalise_art_url(value: Any) -> str | None:
    candidate: str | None = None
    if isinstance(value, str):
        candidate = value
    elif isinstance(value, dict):
        for key in ("url", "remote", "href"):
            item = value.get(key)
            if isinstance(item, str) and item:
                candidate = item
                break
    if not candidate:
        return None
    candidate = candidate.strip()
    return candidate if candidate.startswith(("http://", "https://")) else None


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def sidecar_paths(entry: Any, metadata_dir: str | Path | None = None) -> list[Path]:
    entry_path = Path(str(_entry_value(entry, "path", "")))
    directory = (
        Path(metadata_dir).expanduser().resolve()
        if metadata_dir
        else paths.metadata_dir()
    )
    entry_id = str(_entry_value(entry, "id", "") or "")
    candidates = [
        entry_path.with_suffix(entry_path.suffix + ".json"),
        entry_path.with_suffix(".json"),
    ]
    if entry_id:
        candidates.append(directory / f"{entry_id}.json")
    return candidates


def _apply_payload(
    data: dict[str, Any],
    *,
    source: str,
    path: Path,
    values: dict[str, Any],
    provenance: dict[str, str],
    raw: dict[str, Any],
    current_art: tuple[Path | None, str | None],
    explicit_art_source: str | None,
) -> tuple[tuple[Path | None, str | None], str | None]:
    art_path, art_source = current_art
    for key, value in data.items():
        if key in _RESERVED_KEYS:
            continue
        if key == "art_source":
            if isinstance(value, str) and value.strip():
                explicit_art_source = value.strip()
            continue
        if key == "art":
            url = _normalise_art_url(value)
            if url is not None:
                values["art_url"] = url
                provenance["art"] = source
                continue
            found = _normalise_art(value, path.parent)
            if found is not None:
                values["art_url"] = None
                art_path, art_source = found, source
                provenance["art"] = source
            continue
        if value is None:
            continue
        raw[key] = value
        if key == "title":
            text = _text(value)
            if text:
                values["title"] = text
                provenance["title"] = source
        elif key == "year":
            year, year_raw = _normalise_year(value)
            values["year"] = year
            values["year_raw"] = year_raw
            provenance["year"] = source
        elif key == "publisher":
            values["publisher"] = _text(value)
            provenance["publisher"] = source
        elif key == "genre":
            values["genre"] = _text(value)
            provenance["genre"] = source
        elif key == "description":
            values["description"] = _text(value)
            provenance["description"] = source
        elif key == "developer":
            values["developer"] = _text(value)
            provenance["developer"] = source
        elif key == "players":
            values["players"] = _text(value)
            provenance["players"] = source
        elif key == "machine":
            machine = _normalise_machine(value)
            if machine:
                values["machine"] = machine
                provenance["machine"] = source
        elif key == "resolution":
            resolution = _normalise_resolution(value)
            if resolution:
                values["resolution"] = resolution
                provenance["resolution"] = source
        elif key == "args":
            args = _normalise_args(value)
            if args:
                values["args"] = args
                provenance["args"] = source
        elif key in {"mobygames", "mobygames_id"}:
            try:
                values["mobygames"] = int(value)
            except (TypeError, ValueError):
                pass
        else:
            values[key] = value
            provenance[key] = source
    return (art_path, art_source), explicit_art_source


class MetadataResolver:
    """Resolve sidecar metadata and cached art for catalog entries."""

    def __init__(
        self,
        *,
        state_dir: str | Path | None = None,
        metadata_dir: str | Path | None = None,
        cache_dir: str | Path | None = None,
        kairost: KairoSTCatalog | None = None,
    ):
        self.state_dir = (
            Path(state_dir).expanduser().resolve() if state_dir else paths.state_dir()
        )
        self.metadata_dir = (
            Path(metadata_dir).expanduser().resolve()
            if metadata_dir
            else self.state_dir / "metadata"
        )
        self.cache_dir = (
            Path(cache_dir).expanduser().resolve()
            if cache_dir
            else self.state_dir / "cache" / "art"
        )
        self.kairost = kairost if kairost is not None else KairoSTCatalog()
        self._cache: dict[str, ResolvedMetadata] = {}

    def invalidate(self, entry_id: str | None = None) -> None:
        if entry_id is None:
            self._cache.clear()
        else:
            self._cache.pop(str(entry_id), None)

    def resolve(self, entry: Any) -> ResolvedMetadata:
        entry_id = str(_entry_value(entry, "id", "") or "")
        cached = self._cache.get(entry_id)
        if cached is not None:
            return cached

        values: dict[str, Any] = {
            "title": str(
                _entry_value(entry, "title", "")
                or _entry_value(entry, "filename", "Untitled")
            ),
            "year": _entry_value(entry, "year"),
            "year_raw": _entry_value(entry, "year_raw"),
            "publisher": _entry_value(entry, "publisher"),
            "genre": _entry_value(entry, "genre"),
            "description": None,
            "developer": None,
            "players": None,
            "machine": None,
            "resolution": None,
            "args": (),
            "art_url": None,
        }
        provenance: dict[str, str] = {}
        for key in ("title", "year", "publisher", "genre"):
            if values.get(key) is not None:
                provenance[key] = "filename"
        raw: dict[str, Any] = {
            "filename": str(_entry_value(entry, "filename", "")),
            "relative": str(_entry_value(entry, "relative", "")),
        }
        sources: list[dict[str, Any]] = []
        sidecars: list[str] = []
        art_path: Path | None = None
        art_source: str | None = None
        explicit_art_source: str | None = None

        payloads: list[tuple[bool, int, Path, dict[str, Any], str]] = []
        for index, candidate in enumerate(sidecar_paths(entry, self.metadata_dir)):
            if not candidate.is_file():
                continue
            data = _load_json(candidate)
            if not data:
                continue
            sidecars.append(str(candidate))
            metadata_provenance = data.get("provenance")
            source_value = (
                metadata_provenance.get("source")
                if isinstance(metadata_provenance, dict)
                else None
            )
            online = _is_online_source(source_value)
            if online:
                label = (
                    source_value
                    if source_value.casefold().startswith("online:")
                    else f"online:{source_value}"
                )
            else:
                label = f"sidecar:{candidate}"
            payloads.append((online, index, candidate, data, label))
            sources.append(
                {
                    "path": str(candidate),
                    "source": label,
                    "online": online,
                    "fields": sorted(key for key in data if key not in _RESERVED_KEYS),
                }
            )

        # Filename first, then online enrichment, then KairoST, then local
        # sidecars.  Within a layer the catalog candidate order is the priority
        # order.
        for online, _index, path, data, label in payloads:
            if not online:
                continue
            (art_path, art_source), explicit_art_source = _apply_payload(
                data,
                source=label,
                path=path,
                values=values,
                provenance=provenance,
                raw=raw,
                current_art=(art_path, art_source),
                explicit_art_source=explicit_art_source,
            )

        game = self.kairost.lookup(entry)
        if game is not None:
            label = f"kairost:{game.game_id}"
            payload = game.metadata()
            local_art = game.local_art()
            if local_art is not None:
                payload["art"] = str(local_art)
                payload["art_source"] = label
            else:
                remote_art = game.remote_art()
                if remote_art and not values.get("art_url"):
                    payload["art"] = remote_art
            (art_path, art_source), explicit_art_source = _apply_payload(
                payload,
                source=label,
                path=self.state_dir,
                values=values,
                provenance=provenance,
                raw=raw,
                current_art=(art_path, art_source),
                explicit_art_source=explicit_art_source,
            )
            sources.append(
                {
                    "path": str(self.kairost.data_dir or ""),
                    "source": label,
                    "online": False,
                    "fields": sorted(payload),
                }
            )

        for online, _index, path, data, label in payloads:
            if online:
                continue
            (art_path, art_source), explicit_art_source = _apply_payload(
                data,
                source=label,
                path=path,
                values=values,
                provenance=provenance,
                raw=raw,
                current_art=(art_path, art_source),
                explicit_art_source=explicit_art_source,
            )

        if art_path is None and entry_id:
            for suffix in IMAGE_SUFFIXES:
                candidate = self.cache_dir / f"{entry_id}{suffix}"
                if candidate.is_file() and candidate.stat().st_size > 0:
                    art_path = candidate.resolve()
                    art_source = f"cache:{candidate}"
                    provenance["art"] = art_source
                    break
        if art_path is not None and explicit_art_source:
            art_source = explicit_art_source
            provenance["art"] = explicit_art_source
        if art_path is None:
            art_source = None
            if not values.get("art_url"):
                provenance.pop("art", None)

        resolved = ResolvedMetadata(
            entry_id=entry_id,
            title=str(values["title"]),
            year=values["year"] if isinstance(values["year"], int) else None,
            year_raw=_text(values.get("year_raw")),
            publisher=_text(values.get("publisher")),
            genre=_text(values.get("genre")),
            description=_text(values.get("description")),
            developer=_text(values.get("developer")),
            players=_text(values.get("players")),
            machine=_normalise_machine(values.get("machine")),
            resolution=_normalise_resolution(values.get("resolution")),
            args=_normalise_args(values.get("args")),
            art_path=art_path,
            art_source=art_source,
            provenance=provenance,
            art_url=_text(values.get("art_url")),
            mobygames=(
                values.get("mobygames")
                if isinstance(values.get("mobygames"), int)
                else None
            ),
            sources=sources,
            sidecars=sidecars,
            raw=raw,
        )
        self._cache[entry_id] = resolved
        return resolved
