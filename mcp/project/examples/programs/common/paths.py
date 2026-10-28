"""Repository, TOS, game and state locations shared by the example programs.

All functions are environment-aware and are deliberately cheap: importing this
module does not touch the network, probe Tk, or discover the 13k game corpus.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

DISK_SUFFIXES = (".st", ".msa", ".stx", ".dim", ".ipf", ".zip")
ZIP_DISK_SUFFIXES = (".st", ".msa", ".stx", ".dim", ".ipf")

DEFAULT_GAME_ROOT = (
    "/home/ubuntu/Projects/Hatari/atari_st/archive.org/download/"
    "Atari_ST_TOSEC_2012_04_23/Games"
)


def repo_root() -> Path:
    """Return the Hatari checkout root, honouring ``HATARI_MCP_REPO``."""
    env = os.environ.get("HATARI_MCP_REPO")
    if env:
        return Path(env).expanduser().resolve()
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "mcp/project/src/hatari_mcp").is_dir():
            return parent
        if (parent / "src/sdl/main_sdl.c").is_file():
            return parent
    return Path.cwd().resolve()


def programs_dir() -> Path:
    return repo_root() / "mcp/project/examples/programs"


def default_server_python() -> Path:
    venv = repo_root() / "mcp/.venv/bin/python"
    return venv if venv.is_file() else Path(sys.executable)


def state_dir(*, create: bool = True) -> Path:
    """Examples state root. Override with ``HATARI_MCP_EXAMPLES_STATE``."""
    env = os.environ.get("HATARI_MCP_EXAMPLES_STATE")
    path = Path(env).expanduser() if env else programs_dir() / "state"
    path = path.resolve()
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def cache_dir(*, create: bool = True) -> Path:
    path = state_dir(create=create) / "cache"
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def art_dir(*, create: bool = True) -> Path:
    path = cache_dir(create=create) / "art"
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def metadata_dir(*, create: bool = True) -> Path:
    path = state_dir(create=create) / "metadata"
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def run_dir(*, create: bool = True) -> Path:
    path = state_dir(create=create) / "run"
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def config_path() -> Path:
    return state_dir(create=True) / "config.json"


def default_game_root() -> Path:
    """Return the preferred corpus root, existing or not."""
    for key in ("HATARI_MCP_GAMES", "HATARI_MCP_GAME_ROOT", "HATARI_GAMES_ROOT"):
        env = os.environ.get(key)
        if env:
            return Path(env).expanduser().resolve()
    return Path(DEFAULT_GAME_ROOT).expanduser().resolve()


def game_roots() -> list[Path]:
    """Configured roots from a path-separator-separated environment value."""
    env = os.environ.get("HATARI_MCP_GAMES")
    if env:
        roots = [Path(part).expanduser().resolve() for part in env.split(os.pathsep) if part.strip()]
        if roots:
            return roots
    return [default_game_root()]


_TOS_DIR = Path("/home/ubuntu/Projects/Hatari/tos")

TOS_IMAGE_SIZES = frozenset({196608, 262144, 524288})

_TOS_NAMES = {
    "emutos": ["emutos/etos192us.img", "emutos/emutos.img", "emutos/etos192de.img"],
    "tos100": ["tos1x/roms/tos100us.img"],
    "tos102": ["tos1x/roms/tos102us.img", "tos1x/roms/tos102uk.img"],
    "tos104": ["tos1x/roms/tos104us.img", "tos1x/roms/tos104uk.img"],
    "tos106": ["tos1x/roms/tos106us.img"],
    "tos162": ["tos1x/roms/tos162us.img"],
    "tos206": ["tos3x/roms/tos206us.img", "tos3x/tos206us.img", "tos1x/roms/tos206us.img"],
    "tos306": ["tos3x/roms/tos306us.img", "tos3x/tos306us.img"],
    "tos404": ["tos3x/roms/tos404us.img", "tos3x/roms/tos404.img"],
}


def is_valid_tos(path: Path | str) -> bool:
    """Return whether ``path`` looks like a complete TOS/EmuTOS ROM image."""
    candidate = Path(path).expanduser()
    try:
        return candidate.is_file() and candidate.stat().st_size in TOS_IMAGE_SIZES
    except OSError:
        return False


def tos_candidates(roots: list[str | Path] | None = None) -> dict[str, Path]:
    """Known TOS images that actually exist on this host, keyed by short name.

    ``roots`` lets callers (the launcher settings) supply their own TOS
    directories.  When omitted the historical environment-aware layout is used.
    """
    if roots is None:
        dirs = [_TOS_DIR]
        env = os.environ.get("HATARI_TOS_DIR")
        if env:
            dirs.insert(0, Path(env).expanduser())
    else:
        dirs = [Path(item).expanduser() for item in roots]
    found: dict[str, Path] = {}
    for key, names in _TOS_NAMES.items():
        for root in dirs:
            for name in names:
                candidate = (root / name).expanduser()
                if is_valid_tos(candidate):
                    found[key] = candidate.resolve()
                    break
            if key in found:
                break
    if "emutos" not in found:
        for root in dirs:
            for candidate in sorted((root / "emutos").glob("emutos*.img")):
                if is_valid_tos(candidate):
                    found["emutos"] = candidate.resolve()
                    break
    return found


def find_tos(kind: str = "emutos") -> Path | None:
    """Find one TOS image by short name (``emutos``, ``tos104``, ...).

    ``HATARI_TOS`` wins when set so users can always point the examples at a
    private ROM.
    """
    env = os.environ.get("HATARI_TOS")
    if env:
        path = Path(env).expanduser()
        if path.is_file():
            return path.resolve()
    return tos_candidates().get(kind)


def tos_for_machine(machine: str) -> Path | None:
    machine = (machine or "st").lower()
    preferences = {
        "st": ("tos104", "tos102", "tos100", "tos106", "emutos"),
        "ste": ("tos106", "tos162", "tos206", "tos104", "emutos"),
        "tt": ("tos306", "tos304", "tos404", "emutos"),
        "falcon": ("tos404", "tos306", "emutos"),
    }.get(machine, ("tos104", "tos102", "tos100", "emutos"))
    candidates = tos_candidates()
    for key in preferences:
        if key in candidates:
            return candidates[key]
    return None


def safe_component(text: str, fallback: str = "disk") -> str:
    """Return a filename component without whitespace or control characters."""
    cleaned = []
    for char in text:
        if char.isalnum() or char in "._-":
            cleaned.append(char)
        elif char.isspace():
            cleaned.append("_")
        else:
            cleaned.append("_")
    result = "".join(cleaned).strip("._-")
    while "__" in result:
        result = result.replace("__", "_")
    if not result:
        result = fallback
    return result[:120]


def _space_free_dir(dest_dir: Path) -> Path:
    dest_dir = Path(dest_dir).expanduser().resolve()
    if " " not in str(dest_dir) and "\t" not in str(dest_dir):
        dest_dir.mkdir(parents=True, exist_ok=True)
        return dest_dir
    fallback = Path(tempfile.gettempdir()) / "hmcp-examples-disks"
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


def make_space_free(
    source: str | Path,
    dest_dir: str | Path,
    *,
    mode: str = "symlink",
) -> Path:
    """Expose ``source`` under a path that contains no spaces.

    Hatari's control API takes raw option strings, so a disk path containing a
    space cannot be quoted reliably. A symlink with a sanitized *link* name is
    enough even when the target itself contains spaces; copy mode is available
    for filesystems where symlinks are undesirable.
    """
    source_path = Path(source).expanduser().resolve()
    if not source_path.is_file():
        raise FileNotFoundError(f"disk does not exist: {source_path}")
    if mode not in {"symlink", "copy", "hardlink"}:
        raise ValueError("mode must be 'symlink', 'copy' or 'hardlink'")
    target_dir = _space_free_dir(Path(dest_dir))
    digest = hashlib.sha1(str(source_path).encode("utf-8")).hexdigest()[:8]
    suffix = "".join(source_path.suffixes)[-16:] or ""
    stem = safe_component(source_path.name[: -len(suffix)] if suffix else source_path.name)
    target = target_dir / f"{stem}-{digest}{suffix}"
    if " " in str(target):
        raise ValueError(f"could not construct a space-free path: {target}")
    if target.exists() or target.is_symlink():
        try:
            if target.is_symlink() and Path(os.readlink(target)) == source_path:
                return target
            if mode == "symlink":
                target.unlink()
            elif target.stat().st_size == source_path.stat().st_size:
                return target
            else:
                target.unlink()
        except OSError:
            target.unlink(missing_ok=True)
    if mode == "symlink":
        try:
            target.symlink_to(source_path)
            return target
        except OSError:
            pass
    if mode == "hardlink":
        try:
            os.link(source_path, target)
            return target
        except OSError:
            pass
    shutil.copy2(source_path, target)
    return target


def zip_disk_members(archive: str | Path) -> list[str]:
    """Return sorted disk-image members in a zip, ignoring macOS noise."""
    with zipfile.ZipFile(archive) as zf:
        members = [
            info.filename
            for info in zf.infolist()
            if not info.is_dir()
            and not info.filename.startswith("__MACOSX/")
            and Path(info.filename).suffix.lower() in ZIP_DISK_SUFFIXES
        ]
    return sorted(members, key=lambda name: (len(name), name.casefold()))


def materialize_disk(
    source: str | Path,
    dest_dir: str | Path,
    *,
    mode: str = "symlink",
) -> Path:
    """Return a space-free path for a disk, extracting the first image in a zip.

    The corpus is mostly zipped TOSEC menu disks. Hatari cannot mount the zip
    directly, so the first usable disk image (preferring ``.st``, then ``.msa``,
    then ``.stx``) is extracted into ``dest_dir`` with a sanitized name.
    """
    source_path = Path(source).expanduser().resolve()
    if not source_path.is_file():
        raise FileNotFoundError(f"disk does not exist: {source_path}")
    if source_path.suffix.lower() != ".zip":
        return make_space_free(source_path, dest_dir, mode=mode)

    members = zip_disk_members(source_path)
    if not members:
        raise ValueError(f"zip contains no supported disk image: {source_path}")

    preference = {".st": 0, ".msa": 1, ".stx": 2, ".dim": 3, ".ipf": 4}
    members.sort(key=lambda name: (preference.get(Path(name).suffix.lower(), 9), name.casefold()))
    member = members[0]
    target_dir = _space_free_dir(Path(dest_dir))
    digest = hashlib.sha1(f"{source_path}:{member}".encode("utf-8")).hexdigest()[:8]
    member_path = Path(member)
    target = target_dir / f"{safe_component(member_path.stem)}-{digest}{member_path.suffix.lower()}"
    if target.is_file() and target.stat().st_size > 0:
        return target
    with zipfile.ZipFile(source_path) as zf:
        data = zf.read(member)
    tmp = target.with_name(target.name + ".part")
    tmp.write_bytes(data)
    os.replace(tmp, target)
    return target


def ensure_example_dirs() -> dict[str, Path]:
    return {
        "state": state_dir(),
        "cache": cache_dir(),
        "art": art_dir(),
        "metadata": metadata_dir(),
        "run": run_dir(),
    }
