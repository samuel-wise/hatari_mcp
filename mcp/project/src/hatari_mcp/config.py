"""Environment-derived settings and repository discovery."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path


def _find_repo_root() -> Path:
    env = os.environ.get("HATARI_MCP_REPO")
    if env:
        return Path(env).expanduser().resolve()
    for parent in Path(__file__).resolve().parents:
        if (parent / "src/sdl/main_sdl.c").is_file():
            return parent
    return Path.cwd().resolve()


def _find_hatari(repo: Path) -> Path | None:
    env = os.environ.get("HATARI_BIN")
    if env:
        return Path(env).expanduser().resolve()
    for candidate in (
        repo / "build-install/bin/hatari",
        repo / "build/src/hatari",
    ):
        if candidate.is_file():
            return candidate
    which = shutil.which("hatari")
    return Path(which).resolve() if which else None


def _find_tos() -> Path | None:
    env = os.environ.get("HATARI_TOS")
    if env:
        return Path(env).expanduser().resolve()
    home = Path.home()
    candidates = [
        home / "Projects/Hatari/tos/emutos/etos192us.img",
        home / "Projects/Hatari/tos/tos3x/roms/tos306us.img",
        home / "Projects/Hatari/tos/tos3x/roms/tos306de.img",
        home / "Projects/Hatari/tos/tos1x/roms/tos104us.img",
        home / "Projects/Hatari/tos/tos1x/roms/tos206us.img",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    return None


@dataclass(frozen=True)
class Settings:
    repo_root: Path
    hatari_bin: Path | None
    default_tos: Path | None
    profiles_dir: Path
    startup_timeout: float = 30.0
    command_timeout: float = 2.0
    stop_timeout: float = 5.0
    ring_bytes: int = 4 * 1024 * 1024
    max_command_bytes: int = 3500
    runtime_root: Path = field(default_factory=lambda: Path("/tmp"))

    @classmethod
    def load(cls) -> "Settings":
        repo = _find_repo_root()
        return cls(
            repo_root=repo,
            hatari_bin=_find_hatari(repo),
            default_tos=_find_tos(),
            profiles_dir=Path(
                os.environ.get(
                    "HATARI_MCP_PROFILES",
                    str(repo / "mcp/project/config/profiles"),
                )
            )
            .expanduser()
            .resolve(),
        )

    def require_hatari(self) -> Path:
        from .errors import error

        if not self.hatari_bin:
            raise error(
                "missing_hatari",
                "Hatari executable was not found.",
                "Build Hatari or set HATARI_BIN to the emulator binary.",
            )
        if not self.hatari_bin.is_file():
            raise error(
                "missing_hatari",
                f"Hatari executable does not exist: {self.hatari_bin}",
                "Set HATARI_BIN to a valid executable.",
            )
        return self.hatari_bin
