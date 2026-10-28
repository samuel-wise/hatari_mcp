"""Read-only discovery and staging for the external Atari corpus trees.

The corpus (Okami-Shell, FreeMiNT, GodLib) lives outside this repository and is
never copied into it.  Everything here treats the trees as read-only inputs:
discovery never writes, and :func:`stage_files` only copies out of a root into a
per-case workspace under ``/tmp``.

The module has no MCP dependency so it can be imported by the scenario engine,
the CLI and tests.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

DEFAULT_CORPUS = {
    "okami": ("OKAMI_ROOT", Path.home() / "Projects" / "Hatari" / "Okami-Shell"),
    "freemint": ("FREEMINT_ROOT", Path.home() / "Projects" / "Hatari" / "freemint"),
    "godlib": ("GODLIB_ROOT", Path.home() / "Projects" / "Hatari" / "godlib"),
}
FREEMINT_SUBPROJECTS = ("cflib", "gemlib", "libcmini", "mintlib")
PREBUILT_SUFFIXES = {".prg", ".ttp", ".app"}
MAX_DISCOVERED_FILES = 40
MAX_SIGNATURE_FILES = 500
LICENSE_NOTES = {
    "okami": "Okami-Shell is third-party software; check its license before publishing artifacts.",
    "freemint": "FreeMiNT components have their own licenses; do not redistribute external source.",
    "godlib": "GodLib is third-party software; consume prebuilt artifacts only.",
}


def _repo_root() -> Path:
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "src" / "sdl" / "main_sdl.c").is_file():
            return parent
    return here.parents[3]


def default_corpus_roots(environment: Mapping[str, str] | None = None) -> list[Path]:
    env = dict(os.environ if environment is None else environment)
    roots: list[Path] = []
    for env_var, default in DEFAULT_CORPUS.values():
        value = env.get(env_var)
        root = Path(value).expanduser() if value else default
        with_root = root.resolve() if root.exists() else root
        if with_root not in roots:
            roots.append(with_root)
    return roots


def is_within_corpus(path: str | Path, environment: Mapping[str, str] | None = None) -> bool:
    candidate = Path(path).expanduser()
    try:
        resolved = candidate.resolve()
    except OSError:
        return False
    for root in default_corpus_roots(environment):
        try:
            resolved.relative_to(root)
            return True
        except (ValueError, OSError):
            continue
    return False


@dataclass
class CorpusProject:
    name: str
    env_var: str
    root: Path
    available: bool
    skip_reason: str | None = None
    subprojects: dict[str, dict[str, Any]] = field(default_factory=dict)
    prebuilt: list[str] = field(default_factory=list)
    checks: dict[str, Any] = field(default_factory=dict)
    license_note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "env_var": self.env_var,
            "root": str(self.root),
            "available": self.available,
            "skip_reason": self.skip_reason,
            "subprojects": self.subprojects,
            "prebuilt": self.prebuilt,
            "checks": self.checks,
            "license_note": self.license_note,
            "read_only": True,
        }


def _find_prebuilt(root: Path, limit: int = MAX_DISCOVERED_FILES) -> list[str]:
    found: list[str] = []
    if not root.is_dir():
        return found
    for current, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(name for name in dirnames if not name.startswith("."))
        for filename in sorted(filenames):
            if Path(filename).suffix.lower() in PREBUILT_SUFFIXES:
                found.append(str(Path(current) / filename))
                if len(found) >= limit:
                    return found
    return found


def _tool_check(names: Iterable[str]) -> dict[str, Any]:
    return {name: bool(shutil.which(name)) for name in names}


def _tos_check(environment: Mapping[str, str] | None = None) -> dict[str, Any]:
    env = dict(os.environ if environment is None else environment)
    explicit = env.get("HATARI_TOS")
    if explicit:
        candidate = Path(explicit).expanduser()
        return {"path": str(candidate), "available": candidate.is_file(), "source": "HATARI_TOS"}
    try:
        from .config import Settings

        settings = Settings.load()
        path = settings.default_tos
        return {"path": str(path) if path else None, "available": bool(path and path.is_file()), "source": "default"}
    except Exception:  # pragma: no cover - defensive
        return {"path": None, "available": False, "source": "unknown"}


def _discover_okami(environment: Mapping[str, str] | None = None) -> CorpusProject:
    env = dict(os.environ if environment is None else environment)
    env_var, default = DEFAULT_CORPUS["okami"]
    root = Path(env.get(env_var, default)).expanduser()
    prebuilt = _find_prebuilt(root)
    checks = {
        "tree": root.is_dir(),
        "readme": any((root / name).is_file() for name in ("README", "README.md", "readme.txt")),
        "prebuilt": len(prebuilt),
        "tos": _tos_check(environment),
    }
    if not root.is_dir():
        reason = f"missing Okami-Shell tree (set {env_var} or create {default})"
    elif not prebuilt:
        reason = "Okami-Shell has no prebuilt .TTP/.PRG smoke binary"
    else:
        reason = None
    return CorpusProject(
        name="okami",
        env_var=env_var,
        root=root,
        available=reason is None,
        skip_reason=reason,
        prebuilt=prebuilt,
        checks=checks,
        license_note=LICENSE_NOTES["okami"],
    )


def _discover_freemint(environment: Mapping[str, str] | None = None) -> CorpusProject:
    env = dict(os.environ if environment is None else environment)
    env_var, default = DEFAULT_CORPUS["freemint"]
    root = Path(env.get(env_var, default)).expanduser()
    subprojects: dict[str, dict[str, Any]] = {}
    any_prebuilt = False
    for name in FREEMINT_SUBPROJECTS:
        child = root / name
        prebuilt = _find_prebuilt(child, limit=8) if child.is_dir() else []
        available = child.is_dir() and bool(prebuilt)
        any_prebuilt = any_prebuilt or available
        subprojects[name] = {
            "root": str(child),
            "exists": child.is_dir(),
            "prebuilt": prebuilt,
            "available": available,
            "skip_reason": (
                None
                if available
                else (f"missing FreeMiNT subproject {child}" if not child.is_dir() else f"no prebuilt smoke binary in {child}")
            ),
        }
    checks = {
        "tree": root.is_dir(),
        "subprojects": {name: data["exists"] for name, data in subprojects.items()},
        "tools": _tool_check(["m68k-atari-mint-gcc", "m68k-atari-mint-ld"]),
        "tos": _tos_check(environment),
    }
    if not root.is_dir():
        reason = f"missing FreeMiNT tree (set {env_var} or create {default})"
    elif not any_prebuilt:
        reason = "FreeMiNT has no prebuilt component smoke binaries"
    else:
        reason = None
    return CorpusProject(
        name="freemint",
        env_var=env_var,
        root=root,
        available=reason is None,
        skip_reason=reason,
        subprojects=subprojects,
        checks=checks,
        license_note=LICENSE_NOTES["freemint"],
    )


def _discover_godlib(environment: Mapping[str, str] | None = None) -> CorpusProject:
    env = dict(os.environ if environment is None else environment)
    env_var, default = DEFAULT_CORPUS["godlib"]
    root = Path(env.get(env_var, default)).expanduser()
    prebuilt = _find_prebuilt(root)
    checks = {
        "tree": root.is_dir(),
        "library": (root / "godlib").is_dir(),
        "spl": (root / "godlib.spl").exists(),
        "prebuilt": len(prebuilt),
        "tos": _tos_check(environment),
    }
    if not root.is_dir():
        reason = f"missing GodLib tree (set {env_var} or create {default})"
    elif not checks["library"] and not checks["spl"]:
        reason = "GodLib tree has neither godlib/ nor godlib.spl"
    elif not prebuilt:
        reason = "GodLib has no prebuilt sample/demo .PRG"
    else:
        reason = None
    return CorpusProject(
        name="godlib",
        env_var=env_var,
        root=root,
        available=reason is None,
        skip_reason=reason,
        prebuilt=prebuilt,
        checks=checks,
        license_note=LICENSE_NOTES["godlib"],
    )


def discover_corpus(environment: Mapping[str, str] | None = None) -> dict[str, Any]:
    projects = {
        "okami": _discover_okami(environment),
        "freemint": _discover_freemint(environment),
        "godlib": _discover_godlib(environment),
    }
    return {
        "root_env": {name: env_var for name, (env_var, _default) in DEFAULT_CORPUS.items()},
        "read_only": True,
        "projects": {name: project.as_dict() for name, project in projects.items()},
    }


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


def _assert_no_symlinks(source: Path) -> None:
    """Reject symlinks so a copied corpus tree cannot leak host files."""
    if source.is_symlink():
        raise ValueError(f"stage source is a symlink: {source}")
    if not source.is_dir():
        return
    for current, dirnames, filenames in os.walk(source, followlinks=False):
        for name in dirnames + filenames:
            candidate = Path(current) / name
            if candidate.is_symlink():
                raise ValueError(f"stage source contains a symlink: {candidate}")


def _safe_source(root: Path, relative: str) -> Path:
    candidate = Path(relative)
    raw = candidate.expanduser() if candidate.is_absolute() else root / candidate
    if raw.is_symlink():
        raise ValueError(f"stage source is a symlink: {relative}")
    if candidate.is_absolute():
        resolved = raw.resolve()
    else:
        resolved = raw.resolve()
    try:
        resolved.relative_to(root.resolve())
    except (ValueError, OSError) as exc:
        raise ValueError(f"stage source escapes the corpus root: {relative}") from exc
    return resolved


def file_signature(path: Path) -> dict[str, Any]:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    stat = path.stat()
    return {"path": str(path), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns, "sha256": digest.hexdigest()}


def tree_signature(root: str | Path, *, limit: int = MAX_SIGNATURE_FILES) -> dict[str, Any]:
    base = Path(root).expanduser()
    signature: list[dict[str, Any]] = []
    if base.is_file():
        return {"root": str(base), "files": [file_signature(base)], "truncated": False}
    if not base.is_dir():
        return {"root": str(base), "files": [], "truncated": False, "missing": True}
    count = 0
    for current, dirnames, filenames in os.walk(base):
        dirnames[:] = sorted(dirnames)
        for filename in sorted(filenames):
            path = Path(current) / filename
            if path.is_symlink():
                target = path.resolve()
                try:
                    target.relative_to(base.resolve())
                except (ValueError, OSError):
                    continue
            signature.append(file_signature(path))
            count += 1
            if count >= limit:
                return {"root": str(base), "files": signature, "truncated": True}
    return {"root": str(base), "files": signature, "truncated": False}


def stage_files(
    root: str | Path,
    entries: Iterable[str | Mapping[str, Any]],
    workspace: str | Path,
) -> dict[str, Any]:
    """Copy declared corpus files/directories into ``workspace``.

    Sources must stay under ``root``; symlinks that escape the root are
    rejected.  The source tree is only read, never modified.
    """
    if Path(workspace).expanduser().is_symlink():
        raise ValueError(f"stage workspace must not be a symlink: {workspace}")
    base = Path(root).expanduser().resolve()
    target_root = Path(workspace).expanduser().resolve()
    temp_root = Path(tempfile.gettempdir()).resolve()
    if target_root == temp_root or not _inside(target_root, temp_root):
        raise ValueError(f"stage workspace must be a private directory under {temp_root}: {target_root}")
    if target_root.exists() and (not target_root.is_dir() or any(target_root.iterdir())):
        raise ValueError(f"stage workspace must be fresh/empty: {target_root}")
    protected = [base, *default_corpus_roots(), _repo_root()]
    for root_path in protected:
        if _inside(target_root, root_path):
            raise ValueError(
                f"stage workspace must not be inside a corpus or repository tree: {target_root}"
            )
    target_root.mkdir(parents=True, exist_ok=True)
    staged: list[dict[str, Any]] = []
    for entry in entries:
        if isinstance(entry, str):
            relative = entry
            destination_rel = Path(relative).name
            optional = False
        else:
            relative = str(entry.get("path") or entry.get("from") or "")
            destination_rel = str(entry.get("to") or Path(relative).name)
            optional = bool(entry.get("optional", False))
        if not relative:
            raise ValueError("stage entry has no path")
        if Path(destination_rel).is_absolute() or ".." in Path(destination_rel).parts:
            raise ValueError(f"stage destination must be relative: {destination_rel}")
        source = _safe_source(base, relative)
        destination = (target_root / destination_rel).resolve()
        try:
            destination.relative_to(target_root)
        except (ValueError, OSError) as exc:
            raise ValueError(f"stage destination escapes the workspace: {destination_rel}") from exc
        if not source.exists():
            if optional:
                staged.append({"source": str(source), "to": destination_rel, "skipped": "missing"})
                continue
            raise FileNotFoundError(f"stage source does not exist: {source}")
        _assert_no_symlinks(source)
        digest = file_signature(source) if source.is_file() else None
        if source.is_dir():
            shutil.copytree(source, destination, symlinks=False, dirs_exist_ok=True)
            kind = "directory"
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            kind = "file"
        staged.append(
            {
                "source": str(source),
                "to": destination_rel,
                "kind": kind,
                "sha256": digest["sha256"] if digest else None,
                "size": digest["size"] if digest else None,
            }
        )
    manifest = {
        "root": str(base),
        "workspace": str(target_root),
        "read_only_source": True,
        "entries": staged,
    }
    (target_root / ".stage-manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest
