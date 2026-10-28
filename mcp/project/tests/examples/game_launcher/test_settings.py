"""Configurable launcher paths, TOS discovery and multi-catalog lookup."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

PROJECT = Path(__file__).resolve().parents[3]
PROGRAMS = PROJECT / "examples" / "programs"
if str(PROGRAMS) not in sys.path:
    sys.path.insert(0, str(PROGRAMS))

import game_launcher.service as service_module  # noqa: E402
from common import Config, paths  # noqa: E402
from common.catalog import DiskEntry  # noqa: E402
from game_launcher import profiles  # noqa: E402
from game_launcher.kairost import KairoSTCatalog  # noqa: E402
from game_launcher.service import LauncherService  # noqa: E402
from game_launcher.enrichment import DEFAULT_ART_URL_TEMPLATE  # noqa: E402
from game_launcher.settings import (  # noqa: E402
    PathSettings,
    from_config,
    load_defaults,
    save,
)


_PATH_ENV = (
    "HATARI_BIN",
    "HATARI_TOS_DIR",
    "HATARI_TOS",
    "HATARI_MCP_GAMES",
    "HATARI_MCP_GAME_ROOT",
    "HATARI_GAMES_ROOT",
    "HATARI_MCP_KAIROST_CATALOG",
    "KAIROST_CATALOG",
)


@pytest.fixture(autouse=True)
def _clear_path_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _PATH_ENV:
        monkeypatch.delenv(name, raising=False)


def _defaults(path: Path) -> Path:
    path.write_text(
        json.dumps(
            {
                "hatari_bin": "$HOME/Hatari/hatari",
                "tos_dirs": ["$HOME/Hatari/tos"],
                "game_dirs": ["$HOME/Hatari/games"],
                "catalog_dirs": ["$HOME/Hatari/KairoST/catalog"],
            }
        ),
        encoding="utf-8",
    )
    return path


def _config(tmp_path: Path) -> Config:
    return Config(tmp_path / "config.json")


def test_settings_defaults_expand_home(tmp_path: Path) -> None:
    default_path = _defaults(tmp_path / "defaults.json")
    settings = from_config(_config(tmp_path), defaults_path=default_path)
    home = Path(os.environ["HOME"])
    assert settings.hatari_bin == str(home / "Hatari" / "hatari")
    assert settings.tos_dirs == [str(home / "Hatari" / "tos")]
    assert settings.game_dirs == [str(home / "Hatari" / "games")]
    assert settings.catalog_dirs == [str(home / "Hatari" / "KairoST" / "catalog")]
    assert load_defaults(default_path).game_dirs == settings.game_dirs


def test_settings_override_and_empty_list(tmp_path: Path) -> None:
    default_path = _defaults(tmp_path / "defaults.json")
    config = _config(tmp_path)
    config.update(
        {
            "paths": {
                "hatari_bin": "/opt/hatari",
                "tos_dirs": ["/roms"],
                "game_dirs": [],
                "catalog_dirs": ["/cat-a", "/cat-b"],
            }
        }
    )
    settings = from_config(config, defaults_path=default_path)
    assert settings.hatari_bin == "/opt/hatari"
    assert settings.tos_dirs == ["/roms"]
    assert settings.game_dirs == []
    assert settings.catalog_dirs == ["/cat-a", "/cat-b"]


def test_legacy_game_dirs_and_save_mirror(tmp_path: Path) -> None:
    default_path = _defaults(tmp_path / "defaults.json")
    config = _config(tmp_path)
    config.update({"game_dirs": ["/legacy/games"]})
    settings = from_config(config, defaults_path=default_path)
    assert settings.game_dirs == ["/legacy/games"]

    updated = PathSettings(
        hatari_bin="/bin/hatari",
        tos_dirs=["/roms"],
        game_dirs=["/games"],
        catalog_dirs=[],
    )
    save(config, updated)
    assert config.game_dirs == ["/games"]
    assert config.get("paths")["game_dirs"] == ["/games"]
    assert from_config(config, defaults_path=default_path).catalog_dirs == []


def test_tos_dirs_feed_discovery(tmp_path: Path) -> None:
    root = tmp_path / "tos"
    (root / "emutos").mkdir(parents=True)
    (root / "emutos" / "etos192us.img").write_bytes(b"x" * 196608)
    found = paths.tos_candidates([root])
    assert found.get("emutos") == (root / "emutos" / "etos192us.img").resolve()
    discovered = profiles.discover_tos_candidates([root])
    assert discovered.get("emutos") == found["emutos"]


def _mini_catalog(root: Path, games: dict[str, Any]) -> Path:
    data = root / "catalog"
    research = data / "research"
    research.mkdir(parents=True)
    (research / "games-v1.json").write_text(
        json.dumps({"games": games}), encoding="utf-8"
    )
    return data


def test_kairost_accepts_multiple_catalogs(tmp_path: Path) -> None:
    first = _mini_catalog(
        tmp_path / "one",
        {
            "atarist:1": {
                "title": "Game One",
                "sourceGroups": ["atarist:dir/Game One (1990)(Pub)[a].zip"],
            }
        },
    )
    second = _mini_catalog(
        tmp_path / "two",
        {
            "atarist:2": {
                "title": "Game Two",
                "launchbox": 222,
                "sourceGroups": ["atarist:dir/Game Two (1991)(Pub).zip"],
            }
        },
    )
    art = second / "artwork" / "art" / "catalog" / "atarist" / "222"
    art.mkdir(parents=True)
    cover = art / "box-abc.webp"
    cover.write_bytes(b"RIFF0000WEBP")

    one_path = tmp_path / "Game One (1990)(Pub)[a].zip"
    two_path = tmp_path / "Game Two (1991)(Pub).zip"
    one_path.write_bytes(b"PK")
    two_path.write_bytes(b"PK")
    catalog = KairoSTCatalog([first, second])
    one = DiskEntry.from_path(one_path, tmp_path)
    two = DiskEntry.from_path(two_path, tmp_path)
    found_one = catalog.lookup(one)
    found_two = catalog.lookup(two)
    assert found_one is not None and found_one.data_dir == first
    assert found_two is not None and found_two.data_dir == second
    assert found_two.local_art() == cover.resolve()


def test_service_passes_hatari_bin_to_client_env(
    tmp_path: Path, monkeypatch: Any
) -> None:
    captured: dict[str, Any] = {}

    class FakeClient:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

    monkeypatch.setattr(service_module, "MCPClient", FakeClient)
    binary = tmp_path / "hatari"
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    service = LauncherService(
        state_dir=tmp_path / "state",
        hatari_bin=binary,
        tos_dirs=[tmp_path / "tos"],
        tos_candidates={},
    )
    client = service._new_client()
    assert isinstance(client, FakeClient)
    assert captured["env"] == {"HATARI_BIN": str(binary.resolve())}


def test_environment_overrides_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    default_path = _defaults(tmp_path / "defaults.json")
    monkeypatch.setenv("HATARI_BIN", "/env/hatari")
    monkeypatch.setenv("HATARI_TOS_DIR", "/env/tos")
    monkeypatch.setenv(
        "HATARI_MCP_GAMES", os.pathsep.join(["/env/games-a", "/env/games-b"])
    )
    monkeypatch.setenv("HATARI_MCP_KAIROST_CATALOG", "/env/catalog")
    settings = from_config(_config(tmp_path), defaults_path=default_path)
    assert settings.hatari_bin == "/env/hatari"
    assert settings.tos_dirs == ["/env/tos"]
    assert settings.game_dirs == ["/env/games-a", "/env/games-b"]
    assert settings.catalog_dirs == ["/env/catalog"]

    config = _config(tmp_path)
    config.update(
        {
            "paths": {
                "hatari_bin": "/explicit/hatari",
                "tos_dirs": ["/explicit/tos"],
                "game_dirs": ["/explicit/games"],
                "catalog_dirs": ["/explicit/catalog"],
            }
        }
    )
    explicit = from_config(config, defaults_path=default_path)
    assert explicit.hatari_bin == "/explicit/hatari"
    assert explicit.tos_dirs == ["/explicit/tos"]
    assert explicit.game_dirs == ["/explicit/games"]
    assert explicit.catalog_dirs == ["/explicit/catalog"]


def test_launcher_options_default_to_on(tmp_path: Path) -> None:
    default_path = _defaults(tmp_path / "defaults.json")
    settings = from_config(_config(tmp_path), defaults_path=default_path)
    assert settings.boot_turbo is True
    assert settings.online_enrichment is True
    assert settings.enrichment_url == ""
    assert settings.art_url_template == DEFAULT_ART_URL_TEMPLATE


def test_launcher_options_config_override_and_save_round_trip(
    tmp_path: Path,
) -> None:
    default_path = _defaults(tmp_path / "defaults.json")
    config = _config(tmp_path)
    config.update(
        {
            "ui": {"online_enrichment": False, "boot_turbo": False},
            "online": {
                "provider": "http",
                "url_template": "https://metadata.invalid/{id}",
                "art_url": "https://art.invalid/{stem}.png",
                "name": "custom",
                "timeout": 4,
            },
        }
    )
    settings = from_config(config, defaults_path=default_path)
    assert settings.online_enrichment is False
    assert settings.boot_turbo is False
    assert settings.enrichment_url == "https://metadata.invalid/{id}"
    assert settings.art_url_template == "https://art.invalid/{stem}.png"

    settings.boot_turbo = True
    settings.online_enrichment = True
    saved = save(config, settings)
    assert saved.boot_turbo is True
    online = config.get("online")
    assert online["provider"] == "http"
    assert online["url"] == "https://metadata.invalid/{id}"
    assert "url_template" not in online
    assert online["art_url_template"] == "https://art.invalid/{stem}.png"
    assert "art_url" not in online
    assert online["name"] == "custom"
    assert online["timeout"] == 4
    assert config.ui_get("online_enrichment") is True
    assert config.ui_get("boot_turbo") is True

    reloaded = from_config(config, defaults_path=default_path)
    assert reloaded.enrichment_url == "https://metadata.invalid/{id}"
    assert reloaded.art_url_template == "https://art.invalid/{stem}.png"


def test_save_provider_rules_and_explicit_blanks(tmp_path: Path) -> None:
    default_path = _defaults(tmp_path / "defaults.json")
    config = _config(tmp_path)
    config.update(
        {
            "ui": {"status_visible": True},
            "online": {
                "provider": "http",
                "url": "https://old.invalid/{id}",
                "name": "keep-me",
                "timeout": 9,
                "mobygames_key": "secret",
            },
        }
    )

    blank = from_config(config, defaults_path=default_path)
    assert blank.enrichment_url == "https://old.invalid/{id}"
    blank.enrichment_url = ""
    blank.art_url_template = ""
    save(config, blank)
    online = config.get("online")
    assert "provider" not in online
    assert "url" not in online
    assert "url_template" not in online
    assert "art_url" not in online
    assert "art_url_template" not in online
    assert online["name"] == "keep-me"
    assert online["timeout"] == 9
    assert online["mobygames_key"] == "secret"
    assert config.ui_get("status_visible") is True

    libretro = from_config(config, defaults_path=default_path)
    libretro.enrichment_url = ""
    libretro.art_url_template = DEFAULT_ART_URL_TEMPLATE
    save(config, libretro)
    assert config.get("online")["provider"] == "libretro"
    assert config.get("online")["art_url_template"] == DEFAULT_ART_URL_TEMPLATE

    custom = from_config(config, defaults_path=default_path)
    custom.enrichment_url = ""
    custom.art_url_template = "https://art.invalid/{stem}.png"
    save(config, custom)
    assert config.get("online")["provider"] == "http"
    assert "url" not in config.get("online")
    assert config.get("online")["art_url_template"] == "https://art.invalid/{stem}.png"
