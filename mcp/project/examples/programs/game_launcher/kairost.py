"""Read the optional KairoST catalog for canonical metadata and local art.

KairoST publishes a JSON catalog plus webp artwork.  The launcher treats it as
another local, read-only metadata layer: hand-written sidecars still win, but
KairoST is preferred over filename guesses and online enrichment.

Set ``HATARI_MCP_KAIROST_CATALOG`` (or ``KAIROST_CATALOG``) to the repository
root or directly to its ``catalog`` data directory, or pass one or more paths
from the launcher settings.  Missing data is harmless and disables that layer.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

DEFAULT_CATALOG = Path.home() / "Projects" / "Hatari" / "atari_st" / "KairoST" / "catalog"
_ENV_KEYS = ("HATARI_MCP_KAIROST_CATALOG", "KAIROST_CATALOG")
_ART_SUFFIXES = (".webp", ".png", ".gif", ".jpg", ".jpeg")


def _text(value: Any) -> str | None:
    if isinstance(value, str):
        value = value.strip()
        return value or None
    return None


def _normalise(value: Any) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold()).split())


def _entry_value(entry: Any, key: str, default: Any = None) -> Any:
    if isinstance(entry, dict):
        return entry.get(key, default)
    return getattr(entry, key, default)


def _data_dir(value: str | Path | None) -> Path | None:
    if value:
        root = Path(value).expanduser().resolve()
    else:
        root = None
        for key in _ENV_KEYS:
            item = os.environ.get(key)
            if item:
                root = Path(item).expanduser().resolve()
                break
        if root is None:
            root = DEFAULT_CATALOG.expanduser().resolve()
    for candidate in (root, root / "catalog"):
        if (candidate / "research" / "games-v1.json").is_file():
            return candidate
    return None


def _data_dirs(
    value: str | Path | list[Any] | tuple[Any, ...] | None,
) -> list[Path]:
    items = value if isinstance(value, (list, tuple)) else [value]
    found: list[Path] = []
    for item in items:
        data_dir = _data_dir(item)
        if data_dir is not None and data_dir not in found:
            found.append(data_dir)
    return found



@lru_cache(maxsize=4)
def _load_index(data_dir: Path) -> tuple[dict[str, Any], dict[str, str], dict[str, str]]:
    try:
        raw = json.loads((data_dir / "research" / "games-v1.json").read_text("utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return {}, {}, {}
    games = raw.get("games") if isinstance(raw, dict) else None
    if not isinstance(games, dict):
        return {}, {}, {}
    by_archive: dict[str, str] = {}
    by_title: dict[str, str] = {}

    def add_archive(path: Any, game_id: str) -> None:
        text = _text(path)
        if not text:
            return
        if text.startswith("atarist:"):
            text = text[len("atarist:") :]
        name = re.split(r"[/\\]", text)[-1].casefold()
        if name:
            by_archive.setdefault(name, game_id)

    for game_id, game in games.items():
        if not isinstance(game, dict):
            continue
        for source in game.get("sourceGroups") or ():
            add_archive(source, str(game_id))
        for key in ("title",):
            title = _text(game.get(key))
            if title:
                by_title.setdefault(_normalise(title), str(game_id))
        for alias in game.get("aliases") or ():
            title = _text(alias)
            if title:
                by_title.setdefault(_normalise(title), str(game_id))

    try:
        matches = json.loads(
            (data_dir / "research" / "media-matches-v1.json").read_text("utf-8")
        )
    except (OSError, ValueError, json.JSONDecodeError):
        matches = {}
    archives = matches.get("archives") if isinstance(matches, dict) else None
    if isinstance(archives, dict):
        for path, game_id in archives.items():
            if isinstance(game_id, str):
                add_archive(path, game_id)
    return games, by_archive, by_title


@dataclass(frozen=True)
class KairoSTGame:
    data_dir: Path
    game_id: str
    data: dict[str, Any]

    def metadata(self) -> dict[str, Any]:
        values: dict[str, Any] = {}
        for key in ("title", "year", "publisher", "developer", "description"):
            value = self.data.get(key)
            if value not in (None, ""):
                values[key] = value
        mobygames = self.data.get("mobygames")
        if isinstance(mobygames, int) or (
            isinstance(mobygames, str) and mobygames.isdigit()
        ):
            values["mobygames"] = int(mobygames)
        return values

    def local_art(self) -> Path | None:
        base = self.data_dir / "artwork"
        directories: list[Path] = []
        launchbox = _text(str(self.data.get("launchbox") or ""))
        mobygames = _text(str(self.data.get("mobygames") or ""))
        if launchbox:
            directories.append(base / "art" / "catalog" / "atarist" / launchbox)
        if mobygames:
            directories.append(base / "art" / "catalog" / "moby" / mobygames)
        artwork = self.data.get("artwork")
        if isinstance(artwork, dict):
            preview = _text(artwork.get("preview"))
            if preview:
                candidate = base / preview
                directories.insert(0, candidate.parent)
                if candidate.is_file() and candidate.stat().st_size > 0:
                    return candidate.resolve()
        for directory in directories:
            if not directory.is_dir():
                continue
            for pattern in ("box-*", "screenshot-*", "*"):
                for candidate in sorted(directory.glob(pattern)):
                    if candidate.suffix.casefold() not in _ART_SUFFIXES:
                        continue
                    try:
                        if candidate.is_file() and candidate.stat().st_size > 0:
                            return candidate.resolve()
                    except OSError:
                        continue
        return None

    def remote_art(self) -> str | None:
        for key in ("boxArtUrl", "screenshotUrl"):
            value = _text(self.data.get(key))
            if value and value.startswith(("http://", "https://")):
                return value
        artwork = self.data.get("artwork")
        if isinstance(artwork, dict):
            value = _text(artwork.get("previewUrl"))
            if value and value.startswith(("http://", "https://")):
                return value
        return None


class KairoSTCatalog:
    """Case-insensitive lookup of catalog entries and their artwork."""

    def __init__(
        self,
        path: str | Path | list[Any] | tuple[Any, ...] | None = None,
    ):
        self.data_dirs = _data_dirs(path)
        self.data_dir = self.data_dirs[0] if self.data_dirs else None
        self.games: dict[str, Any] = {}
        self.by_archive: dict[str, str] = {}
        self.by_title: dict[str, str] = {}
        self._game_dirs: dict[str, Path] = {}
        for data_dir in self.data_dirs:
            games, by_archive, by_title = _load_index(data_dir)
            for game_id, data in games.items():
                game_id = str(game_id)
                if game_id not in self.games:
                    self.games[game_id] = data
                    self._game_dirs[game_id] = data_dir
            for name, game_id in by_archive.items():
                game_id = str(game_id)
                if name not in self.by_archive and game_id in self.games:
                    self.by_archive[name] = game_id
            for name, game_id in by_title.items():
                game_id = str(game_id)
                if name not in self.by_title and game_id in self.games:
                    self.by_title[name] = game_id

    @property
    def available(self) -> bool:
        return bool(self.data_dirs) and bool(self.games)

    def lookup(self, entry: Any) -> KairoSTGame | None:
        if not self.available:
            return None
        filename = _text(_entry_value(entry, "filename"))
        if not filename:
            filename = Path(str(_entry_value(entry, "path", ""))).name
        game_id = self.by_archive.get(filename.casefold())
        if game_id is None:
            title = _entry_value(entry, "title")
            game_id = self.by_title.get(_normalise(title))
        if game_id is None:
            return None
        data = self.games.get(game_id)
        data_dir = self._game_dirs.get(game_id)
        if not isinstance(data, dict) or data_dir is None:
            return None
        return KairoSTGame(data_dir, game_id, data)
