"""Configurable filesystem paths and launcher options.

The launcher never invents a host-specific path.  A small ``defaults.json``
shipped next to this module supplies the system defaults; users can override
any of them through the Settings dialog, which stores paths under ``paths`` and
launcher options under ``ui``/``online`` in ``config.json``.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:  # package import
    from common import paths
    from .enrichment import DEFAULT_ART_URL_TEMPLATE
except ImportError:  # direct script import
    import paths  # type: ignore[no-redef]

    from enrichment import DEFAULT_ART_URL_TEMPLATE  # type: ignore[no-redef]

DEFAULTS_FILE = Path(__file__).with_name("defaults.json")


def _clean(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    return str(Path(os.path.expandvars(text)).expanduser())


def _clean_list(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = _clean(item)
        if text and text not in seen:
            result.append(text)
            seen.add(text)
    return result


def _clean_url(value: Any) -> str:
    if value is None:
        return ""
    return os.path.expandvars(str(value)).strip()


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().casefold() in {"1", "true", "yes", "on"}
    return bool(value)


@dataclass
class PathSettings:
    """Paths the launcher resolves at runtime."""

    hatari_bin: str = ""
    tos_dirs: list[str] = field(default_factory=list)
    game_dirs: list[str] = field(default_factory=list)
    catalog_dirs: list[str] = field(default_factory=list)
    boot_turbo: bool = True
    online_enrichment: bool = True
    enrichment_url: str = ""
    art_url_template: str = DEFAULT_ART_URL_TEMPLATE

    def as_dict(self) -> dict[str, Any]:
        return {
            "hatari_bin": self.hatari_bin,
            "tos_dirs": list(self.tos_dirs),
            "game_dirs": list(self.game_dirs),
            "catalog_dirs": list(self.catalog_dirs),
        }

    def normalised(self) -> "PathSettings":
        return PathSettings(
            hatari_bin=_clean(self.hatari_bin),
            tos_dirs=_clean_list(self.tos_dirs),
            game_dirs=_clean_list(self.game_dirs),
            catalog_dirs=_clean_list(self.catalog_dirs),
            boot_turbo=bool(self.boot_turbo),
            online_enrichment=bool(self.online_enrichment),
            enrichment_url=_clean_url(self.enrichment_url),
            art_url_template=_clean_url(self.art_url_template),
        )

    @property
    def hatari_path(self) -> Path | None:
        return Path(self.hatari_bin) if self.hatari_bin else None

    @property
    def tos_paths(self) -> list[Path]:
        return [Path(item) for item in self.tos_dirs]

    @property
    def game_paths(self) -> list[Path]:
        return [Path(item) for item in self.game_dirs]

    @property
    def catalog_paths(self) -> list[Path]:
        return [Path(item) for item in self.catalog_dirs]


def load_defaults(path: str | Path | None = None) -> PathSettings:
    """Read the shipped system defaults; missing or invalid data means empty."""
    target = Path(path).expanduser() if path else DEFAULTS_FILE
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    art_template = (
        _clean_url(raw["art_url_template"])
        if "art_url_template" in raw
        else DEFAULT_ART_URL_TEMPLATE
    )
    return PathSettings(
        hatari_bin=_clean(raw.get("hatari_bin")),
        tos_dirs=_clean_list(raw.get("tos_dirs")),
        game_dirs=_clean_list(raw.get("game_dirs")),
        catalog_dirs=_clean_list(raw.get("catalog_dirs")),
        boot_turbo=_as_bool(raw.get("boot_turbo"), True),
        online_enrichment=_as_bool(raw.get("online_enrichment"), True),
        enrichment_url=_clean_url(raw.get("enrichment_url")),
        art_url_template=art_template,
    )


def _environment_dirs(*names: str) -> list[str]:
    for name in names:
        value = os.environ.get(name)
        if value:
            return _clean_list(value.split(os.pathsep))
    return []


def _environment_tos() -> list[str]:
    found = _environment_dirs("HATARI_TOS_DIR")
    if found:
        return found
    image = os.environ.get("HATARI_TOS")
    if image:
        return _clean_list([str(Path(image).expanduser().parent)])
    return []


def from_config(
    config: Any, *, defaults_path: str | Path | None = None
) -> PathSettings:
    """Merge ``config.paths`` over environment overrides and shipped defaults.

    The legacy top-level ``game_dirs`` list (used by older state files and the
    Add folder button) still works when no explicit ``paths.game_dirs`` exists.
    An explicitly empty list in ``paths`` is respected.
    """
    default = load_defaults(defaults_path)
    raw = config.get("paths", {}) if hasattr(config, "get") else {}
    if not isinstance(raw, dict):
        raw = {}

    def selected(
        key: str, fallback: list[str], environment: list[str]
    ) -> list[str]:
        if key in raw:
            return _clean_list(raw.get(key))
        return list(environment or fallback)

    game_dirs = selected(
        "game_dirs",
        default.game_dirs,
        _environment_dirs(
            "HATARI_MCP_GAMES",
            "HATARI_MCP_GAME_ROOT",
            "HATARI_GAMES_ROOT",
        ),
    )
    if "game_dirs" not in raw:
        legacy = getattr(config, "game_dirs", [])
        if legacy:
            game_dirs = _clean_list(legacy)

    if "hatari_bin" in raw:
        hatari_bin = _clean(raw["hatari_bin"])
    else:
        hatari_bin = _clean(os.environ.get("HATARI_BIN")) or default.hatari_bin

    ui = config.get("ui", {}) if hasattr(config, "get") else {}
    if not isinstance(ui, dict):
        ui = {}
    boot_turbo = _as_bool(ui.get("boot_turbo"), default.boot_turbo)
    online = _as_bool(ui.get("online_enrichment"), default.online_enrichment)

    online_config = config.get("online", {}) if hasattr(config, "get") else {}
    if not isinstance(online_config, dict):
        online_config = {}
    if "url" in online_config:
        enrichment_url = _clean_url(online_config.get("url"))
    elif "url_template" in online_config:
        enrichment_url = _clean_url(online_config.get("url_template"))
    else:
        enrichment_url = default.enrichment_url
    if "art_url_template" in online_config:
        art_template = _clean_url(online_config.get("art_url_template"))
    elif "art_url" in online_config:
        art_template = _clean_url(online_config.get("art_url"))
    else:
        art_template = default.art_url_template

    return PathSettings(
        hatari_bin=hatari_bin,
        tos_dirs=selected("tos_dirs", default.tos_dirs, _environment_tos()),
        game_dirs=game_dirs,
        catalog_dirs=selected(
            "catalog_dirs",
            default.catalog_dirs,
            _environment_dirs(
                "HATARI_MCP_KAIROST_CATALOG", "KAIROST_CATALOG"
            ),
        ),
        boot_turbo=boot_turbo,
        online_enrichment=online,
        enrichment_url=enrichment_url,
        art_url_template=art_template,
    ).normalised()


def save(config: Any, settings: PathSettings) -> PathSettings:
    """Persist settings while preserving unrelated UI and provider options."""
    normalised = settings.normalised()
    paths_payload = {
        "hatari_bin": normalised.hatari_bin,
        "tos_dirs": list(normalised.tos_dirs),
        "game_dirs": list(normalised.game_dirs),
        "catalog_dirs": list(normalised.catalog_dirs),
    }
    ui = config.get("ui", {}) if hasattr(config, "get") else {}
    ui = dict(ui) if isinstance(ui, dict) else {}
    ui.update(
        {
            "boot_turbo": normalised.boot_turbo,
            "online_enrichment": normalised.online_enrichment,
        }
    )

    online = config.get("online", {}) if hasattr(config, "get") else {}
    online = dict(online) if isinstance(online, dict) else {}
    for key in ("provider", "url", "url_template", "art_url", "art_url_template"):
        online.pop(key, None)
    enrichment_url = normalised.enrichment_url
    art_template = normalised.art_url_template
    if enrichment_url:
        online["provider"] = "http"
        online["url"] = enrichment_url
    if art_template:
        online["art_url_template"] = art_template
        if not enrichment_url:
            if art_template == DEFAULT_ART_URL_TEMPLATE:
                online["provider"] = "libretro"
            else:
                online["provider"] = "http"

    config.update(
        {
            "paths": paths_payload,
            "game_dirs": list(normalised.game_dirs),
            "ui": ui,
            "online": online,
        }
    )
    return normalised


def default_game_paths(settings: PathSettings) -> list[Path]:
    """Return configured roots, or the shared environment-aware fallback."""
    return settings.game_paths or paths.game_roots()
