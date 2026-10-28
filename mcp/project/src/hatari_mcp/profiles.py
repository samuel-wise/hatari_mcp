"""TOML launch profiles."""

from __future__ import annotations

import os
import re
import tomllib
from pathlib import Path
from typing import Any

from .errors import error

_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_ROOT_KEYS = {"name", "description", "launch", "paths", "server"}
_LAUNCH_KEYS = {"args", "env", "display"}
_SERVER_KEYS = {"command_timeout_ms", "startup_timeout_ms"}


def _expand(value: str) -> str:
    return os.path.expandvars(os.path.expanduser(value))


def profile_path(profiles_dir: Path, name: str) -> Path:
    if not _NAME_RE.match(name):
        raise error("bad_option", f"Invalid profile name: {name!r}", "Use letters, digits, '.', '_' or '-'.")
    path = profiles_dir / f"{name}.toml"
    if not path.is_file():
        raise error(
            "bad_option",
            f"Profile not found: {name}",
            f"Create {path} or choose another profile.",
        )
    return path


def load_profile(profiles_dir: Path, name: str) -> dict[str, Any]:
    path = profile_path(profiles_dir, name)
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise error("bad_option", f"Could not read profile {name}: {exc}", "Fix the TOML file.") from exc

    unknown = set(data) - _ROOT_KEYS
    if unknown:
        raise error("bad_option", f"Unknown profile keys: {sorted(unknown)}", "Remove or fix the keys.")
    launch = data.get("launch", {})
    if not isinstance(launch, dict):
        raise error("bad_option", "profile.launch must be a table", "Use [launch] in the TOML profile.")
    unknown = set(launch) - _LAUNCH_KEYS
    if unknown:
        raise error("bad_option", f"Unknown launch keys: {sorted(unknown)}", "Fix the profile.")
    server = data.get("server", {})
    if not isinstance(server, dict):
        raise error("bad_option", "profile.server must be a table", "Use [server] in the TOML profile.")
    unknown = set(server) - _SERVER_KEYS
    if unknown:
        raise error("bad_option", f"Unknown server keys: {sorted(unknown)}", "Fix the profile.")
    args = launch.get("args", [])
    env = launch.get("env", {})
    if not isinstance(args, list) or not all(isinstance(item, str) for item in args):
        raise error("bad_option", "launch.args must be a list of strings", "Fix the profile.")
    if not isinstance(env, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in env.items()):
        raise error("bad_option", "launch.env must map strings to strings", "Fix the profile.")
    paths = data.get("paths", {})
    if not isinstance(paths, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in paths.items()):
        raise error("bad_option", "paths must map strings to strings", "Fix the profile.")
    data["_path"] = str(path)
    data["launch"] = {**launch, "args": args, "env": {k: _expand(v) for k, v in env.items()}}
    data["paths"] = {k: _expand(v) for k, v in paths.items()}
    data["server"] = server
    return data


def select_profile(profiles_dir: Path, explicit: str | None) -> dict[str, Any] | None:
    name = explicit or os.environ.get("HATARI_MCP_PROFILE")
    if not name:
        return None
    return load_profile(profiles_dir, name)
