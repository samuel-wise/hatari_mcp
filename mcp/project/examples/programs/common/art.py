"""Local artwork lookup and deterministic placeholders for the Tkinter UIs."""

from __future__ import annotations

import colorsys
import hashlib
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:  # package import
    from . import paths
except ImportError:  # direct script import
    import paths  # type: ignore[no-redef]

IMAGE_SUFFIXES = (".png", ".gif", ".jpg", ".jpeg", ".webp")
MAX_ART_BYTES = 8 * 1024 * 1024
_CONTENT_TYPE_SUFFIX = {
    "image/png": ".png",
    "image/gif": ".gif",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/webp": ".webp",
}


def _is_http_url(value: Any) -> bool:
    return isinstance(value, str) and value.strip().casefold().startswith(
        ("http://", "https://")
    )


def _image_suffix(data: bytes, content_type: str | None, url: str) -> str | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if content_type:
        suffix = _CONTENT_TYPE_SUFFIX.get(content_type.split(";")[0].strip().casefold())
        if suffix:
            return suffix
    suffix = Path(urllib.parse.urlparse(url).path).suffix.casefold()
    return suffix if suffix in IMAGE_SUFFIXES else None


def _entry_value(entry: Any, key: str, default: Any = None) -> Any:
    if isinstance(entry, dict):
        return entry.get(key, default)
    return getattr(entry, key, default)


@dataclass(frozen=True)
class Artwork:
    path: Path
    source: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Placeholder:
    title: str
    year: int | None
    publisher: str | None
    initials: str
    background: str
    foreground: str
    accent: str
    seed: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "year": self.year,
            "publisher": self.publisher,
            "initials": self.initials,
            "background": self.background,
            "foreground": self.foreground,
            "accent": self.accent,
            "seed": self.seed,
        }


def _hex_color(hue: float, lightness: float, saturation: float = 0.55) -> str:
    red, green, blue = colorsys.hls_to_rgb(hue % 1.0, lightness, saturation)
    return f"#{int(red * 255):02x}{int(green * 255):02x}{int(blue * 255):02x}"


def placeholder_for(entry: Any) -> Placeholder:
    """Deterministic colors and initials derived from the entry identity."""
    entry_id = str(_entry_value(entry, "id", "") or "")
    title = str(_entry_value(entry, "title", "") or _entry_value(entry, "filename", "Untitled"))
    year = _entry_value(entry, "year")
    publisher = _entry_value(entry, "publisher")
    seed = entry_id or hashlib.sha1(title.encode("utf-8")).hexdigest()[:16]
    digest = hashlib.sha256(seed.encode("utf-8")).digest()
    hue = digest[0] / 255.0
    words = [word for word in title.replace("-", " ").split() if word]
    initials = "".join(word[0] for word in words[:3]).upper() or "ST"
    return Placeholder(
        title=title,
        year=int(year) if isinstance(year, int) else None,
        publisher=str(publisher) if publisher else None,
        initials=initials[:3],
        background=_hex_color(hue, 0.20, 0.45),
        foreground=_hex_color(hue, 0.92, 0.18),
        accent=_hex_color(hue + 0.08, 0.65, 0.65),
        seed=seed,
    )


class ArtResolver:
    """Find cached/sidecar art without requiring Pillow or Tk."""

    def __init__(self, cache_dir: str | Path | None = None):
        self.cache_dir = (
            Path(cache_dir).expanduser().resolve() if cache_dir else paths.art_dir()
        )

    def candidate_paths(self, entry: Any) -> list[Path]:
        entry_id = str(_entry_value(entry, "id", "") or "")
        metadata = _entry_value(entry, "metadata", {}) or {}
        candidates: list[Path] = []

        art = metadata.get("art") if isinstance(metadata, dict) else None
        if isinstance(art, str):
            if not _is_http_url(art):
                candidates.append(Path(art).expanduser())
        elif isinstance(art, dict):
            for key in ("path", "file", "local"):
                value = art.get(key)
                if isinstance(value, str) and not _is_http_url(value):
                    candidates.append(Path(value).expanduser())

        if entry_id:
            for suffix in IMAGE_SUFFIXES:
                candidates.append(self.cache_dir / f"{entry_id}{suffix}")
        entry_path = _entry_value(entry, "path")
        if entry_path:
            path = Path(entry_path)
            for suffix in IMAGE_SUFFIXES:
                candidates.append(path.with_suffix(suffix))
        return candidates

    def find(self, entry: Any) -> Artwork | None:
        metadata = dict(_entry_value(entry, "metadata", {}) or {})
        for candidate in self.candidate_paths(entry):
            try:
                candidate = candidate.expanduser()
                if candidate.is_file() and candidate.stat().st_size > 0:
                    return Artwork(path=candidate.resolve(), source="cache", metadata=metadata)
            except OSError:
                continue
        return None

    def cached(self, entry_id: str) -> Path | None:
        name = paths.safe_component(entry_id)
        for suffix in IMAGE_SUFFIXES:
            candidate = self.cache_dir / f"{name}{suffix}"
            try:
                if candidate.is_file() and candidate.stat().st_size > 0:
                    return candidate.resolve()
            except OSError:
                continue
        return None

    def download(
        self,
        url: str,
        entry_id: str,
        *,
        timeout: float = 10.0,
        max_bytes: int = MAX_ART_BYTES,
    ) -> Path | None:
        """Fetch remote cover art into the local cache atomically."""
        if not _is_http_url(url) or not entry_id:
            return None
        existing = self.cached(entry_id)
        if existing is not None:
            return existing
        request = urllib.request.Request(
            url,
            headers={"User-Agent": "hatari-mcp-game-launcher/1.0"},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                data = response.read(max_bytes + 1)
                content_type = response.headers.get("Content-Type")
        except (OSError, ValueError, urllib.error.URLError):
            return None
        if not data or len(data) > max_bytes:
            return None
        suffix = _image_suffix(data, content_type, url)
        if suffix is None:
            return None
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        target = self.cache_dir / f"{paths.safe_component(entry_id)}{suffix}"
        temporary = target.with_name(target.name + ".part")
        try:
            temporary.write_bytes(data)
            os.replace(temporary, target)
            for other in IMAGE_SUFFIXES:
                stale = target.with_suffix(other)
                if stale != target:
                    try:
                        stale.unlink()
                    except OSError:
                        pass
            return target.resolve()
        except OSError:
            try:
                temporary.unlink()
            except OSError:
                pass
            return None

    def resolve(self, entry: Any) -> Artwork | Placeholder:
        return self.find(entry) or placeholder_for(entry)

    def load_tk_photo(
        self,
        artwork: Artwork | str | Path,
        *,
        master: Any = None,
        max_width: int | None = None,
        max_height: int | None = None,
    ) -> Any:
        """Return a ``PhotoImage`` or ``None`` when the format needs Pillow.

        PNG and GIF work with stock Tk. JPEG and WebP require Pillow; this
        method deliberately does not import PIL unless needed. Pillow-backed
        images are scaled before conversion so callers do not have to know
        that ``ImageTk.PhotoImage`` lacks Tk's ``subsample`` method.
        """
        path = artwork.path if isinstance(artwork, Artwork) else Path(artwork)
        suffix = path.suffix.lower()
        if suffix in {".jpg", ".jpeg", ".webp"}:
            try:
                from PIL import Image, ImageTk  # type: ignore
            except ImportError:
                return None
            try:
                image = Image.open(path)
                if image.mode not in {"RGB", "RGBA"}:
                    image = image.convert("RGBA")
                if max_width or max_height:
                    size = (
                        max(1, max_width or image.width),
                        max(1, max_height or image.height),
                    )
                    image.thumbnail(size, Image.Resampling.LANCZOS)
                photo = ImageTk.PhotoImage(image, master=master)
            except (OSError, ValueError):
                return None
            # Keep the source image alive alongside the PhotoImage.
            try:
                photo._pil_source = image  # type: ignore[attr-defined]
            except Exception:
                pass
            return photo
        if suffix not in {".png", ".gif"}:
            return None
        try:
            import tkinter
        except ImportError:
            return None
        return tkinter.PhotoImage(file=str(path), master=master)

    def display(self, entry: Any, *, master: Any = None) -> dict[str, Any]:
        """Convenience for UIs: path/photo or placeholder metadata."""
        found = self.find(entry)
        if found is None:
            placeholder = placeholder_for(entry)
            return {"art": None, "photo": None, "placeholder": placeholder.as_dict()}
        return {
            "art": found,
            "photo": self.load_tk_photo(found, master=master),
            "placeholder": None,
        }
