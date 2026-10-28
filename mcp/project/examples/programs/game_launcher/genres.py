"""Best-effort genre lookup with a small persistent cache.

KairoST does not publish genres and MobyGames blocks unauthenticated access.
Wikipedia's infobox is a keyless source for many Atari ST titles, so the
launcher looks a selected title up on demand, caches the result, and leaves the
field untouched when the lookup fails.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from common.config import atomic_write_json

_WIKI_API = "https://en.wikipedia.org/w/api.php"
_MOBY_API = "https://api.mobygames.com/v1/games/{game_id}"
_USER_AGENT = "hatari-mcp-game-launcher/1.0 (genre lookup)"
_REF_RE = re.compile(r"<ref[^>]*>.*?</ref>|<ref[^>]*/>", re.IGNORECASE | re.DOTALL)
_LINK_RE = re.compile(r"\[\[([^|\]]+)(?:\|([^\]]+))?\]\]")
_TAG_RE = re.compile(r"<[^>]+>")
_GENRE_RE = re.compile(r"^\s*\|\s*genres?\s*=\s*(.+?)\s*$", re.IGNORECASE)
_EMPTY_VALUES = {"", "-", "n/a", "unknown", "none"}


def _title_key(title: str) -> str:
    text = re.sub(r"[^a-z0-9]+", " ", str(title or "").casefold())
    return "title:" + " ".join(text.split())


def _clean(value: str) -> str:
    value = _REF_RE.sub("", value)
    value = _LINK_RE.sub(lambda match: match.group(2) or match.group(1), value)
    value = _TAG_RE.sub(" / ", value)
    value = value.replace("'''", "").replace("''", "").replace("&nbsp;", " ")
    value = re.sub(r"\{\{[^{}]*\}\}", "", value)
    value = value.replace("&amp;", "&")
    value = re.sub(r"\s+", " ", value).strip(" .,;:/")
    return value


def _split_genres(value: str) -> list[str]:
    parts = re.split(r"\s*(?:,|;|\bor\b|\band\b)\s*", value, flags=re.IGNORECASE)
    result: list[str] = []
    for part in parts:
        text = _clean(part)
        if text and text.casefold() not in _EMPTY_VALUES and text not in result:
            result.append(text)
    return result


def _wikipedia_genre(title: str, *, timeout: float = 8.0) -> str | None:
    query = urllib.parse.urlencode(
        {
            "action": "query",
            "prop": "revisions",
            "rvprop": "content",
            "rvslots": "main",
            "format": "json",
            "redirects": "1",
            "titles": title,
        }
    )
    request = urllib.request.Request(
        f"{_WIKI_API}?{query}", headers={"User-Agent": _USER_AGENT}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    pages = payload.get("query", {}).get("pages", {}) if isinstance(payload, dict) else {}
    if not isinstance(pages, dict):
        return None
    for page in pages.values():
        if not isinstance(page, dict):
            continue
        revisions = page.get("revisions")
        if not isinstance(revisions, list) or not revisions:
            continue
        slots = revisions[0].get("slots") if isinstance(revisions[0], dict) else None
        content = slots.get("main", {}).get("*") if isinstance(slots, dict) else None
        if not isinstance(content, str):
            continue
        for line in content.splitlines():
            match = _GENRE_RE.match(line)
            if not match:
                continue
            genres = _split_genres(match.group(1))
            if genres:
                return " / ".join(genres)
    return None


def _mobygames_genre(game_id: int, api_key: str, *, timeout: float = 8.0) -> str | None:
    query = urllib.parse.urlencode({"api_key": api_key})
    request = urllib.request.Request(
        f"{_MOBY_API.format(game_id=game_id)}?{query}",
        headers={"User-Agent": _USER_AGENT},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    genres = payload.get("genres") if isinstance(payload, dict) else None
    if not isinstance(genres, list):
        return None
    names: list[str] = []
    for item in genres:
        if isinstance(item, dict):
            value = item.get("genre_name") or item.get("name")
        else:
            value = item
        text = _clean(str(value or ""))
        if text and text.casefold() not in _EMPTY_VALUES and text not in names:
            names.append(text)
    return " / ".join(names) if names else None


class GenreResolver:
    """Look up missing genres once per title and persist the answer."""

    def __init__(self, state_dir: str | Path, *, mobygames_key: str | None = None):
        self.path = Path(state_dir).expanduser().resolve() / "cache" / "genres.json"
        self.mobygames_key = (
            mobygames_key or os.environ.get("MOBYGAMES_API_KEY") or ""
        ).strip()
        self._lock = threading.RLock()
        self._cache: dict[str, dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text("utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            return
        if isinstance(data, dict):
            self._cache = {
                str(key): value
                for key, value in data.items()
                if isinstance(value, dict) and isinstance(value.get("genre"), str)
            }

    def _record(self, entry_id: str, title: str | None = None) -> dict[str, Any] | None:
        with self._lock:
            value = self._cache.get(str(entry_id))
            if value is None and title:
                value = self._cache.get(_title_key(title))
            return dict(value) if value else None

    def cached(self, entry_id: str, title: str | None = None) -> str | None:
        value = self._record(entry_id, title)
        return str(value["genre"]) if value else None

    def lookup(self, entry: Any, metadata: Any) -> tuple[str, str] | None:
        if getattr(metadata, "genre", None):
            return None
        entry_id = str(_entry_value(entry, "id", "") or "")
        if not entry_id:
            return None
        title = str(
            getattr(metadata, "title", None)
            or _entry_value(entry, "title", "")
            or ""
        ).strip()
        cached = self._record(entry_id, title)
        if cached:
            return str(cached["genre"]), str(cached.get("source") or "cache")
        source = "wikipedia"
        genre = None
        mobygames = getattr(metadata, "mobygames", None)
        if self.mobygames_key and isinstance(mobygames, int) and mobygames > 0:
            genre = _mobygames_genre(mobygames, self.mobygames_key)
            if genre:
                source = "mobygames"
        if not genre and title:
            genre = _wikipedia_genre(title)
            if genre:
                source = "wikipedia"
        if not genre:
            return None
        record = {
            "genre": genre,
            "source": source,
            "title": title,
            "mobygames": mobygames if source == "mobygames" else None,
            "fetched_at": int(time.time()),
        }
        with self._lock:
            self._cache[entry_id] = record
            self._cache[_title_key(title)] = record
            try:
                atomic_write_json(self.path, self._cache)
            except OSError:
                pass
        return genre, source


def _entry_value(entry: Any, key: str, default: Any = None) -> Any:
    if isinstance(entry, dict):
        return entry.get(key, default)
    return getattr(entry, key, default)
