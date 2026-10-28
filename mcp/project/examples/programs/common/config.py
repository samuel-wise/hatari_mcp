"""Atomic JSON configuration and learned per-game launch state."""

from __future__ import annotations

import contextlib
import copy
import json
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

try:  # package import
    from . import paths
except ImportError:  # direct script import
    import paths  # type: ignore[no-redef]

DEFAULT_CONFIG: dict[str, Any] = {
    "version": 1,
    "game_dirs": [],
    "ui": {},
    "games": {},
}


def atomic_write_json(path: str | Path, payload: Any, *, mode: int = 0o600) -> Path:
    """Write JSON via a same-directory temp file, fsync, and ``os.replace``."""
    target = Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False).encode("utf-8")
    fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        with contextlib.suppress(OSError):
            os.chmod(tmp, mode)
        os.replace(tmp, target)
        with contextlib.suppress(OSError):
            dir_fd = os.open(target.parent, os.O_DIRECTORY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
    finally:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()
    return target


class Config:
    """Thread-safe, self-contained JSON store for the example programs."""

    def __init__(self, path: str | Path | None = None, *, autosave: bool = True):
        self.path = Path(path).expanduser() if path else paths.config_path()
        self.autosave = autosave
        self._lock = threading.RLock()
        self.data: dict[str, Any] = copy.deepcopy(DEFAULT_CONFIG)
        self.load_error: str | None = None
        self._load()

    # -- persistence ---------------------------------------------------

    def _load(self) -> None:
        with self._lock:
            path = self.path
            if not path.is_file():
                self.data = copy.deepcopy(DEFAULT_CONFIG)
                return
            try:
                parsed = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(parsed, dict):
                    raise ValueError("top-level JSON value is not an object")
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                self.load_error = str(exc)
                backup = path.with_name(f"{path.name}.corrupt-{int(time.time())}")
                with contextlib.suppress(OSError):
                    os.replace(path, backup)
                self.data = copy.deepcopy(DEFAULT_CONFIG)
                return
            merged = copy.deepcopy(DEFAULT_CONFIG)
            merged.update(parsed)
            if not isinstance(merged.get("game_dirs"), list):
                merged["game_dirs"] = []
            if not isinstance(merged.get("ui"), dict):
                merged["ui"] = {}
            if not isinstance(merged.get("games"), dict):
                merged["games"] = {}
            self.data = merged

    def reload(self) -> "Config":
        self._load()
        return self

    def save(self) -> Path:
        with self._lock:
            return atomic_write_json(self.path, self.data)

    def _changed(self) -> None:
        if self.autosave:
            self.save()

    # -- generic access -------------------------------------------------

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return copy.deepcopy(self.data.get(key, default))

    def set(self, key: str, value: Any) -> "Config":
        with self._lock:
            self.data[key] = copy.deepcopy(value)
            self._changed()
            return self

    def update(self, values: dict[str, Any]) -> "Config":
        with self._lock:
            self.data.update(copy.deepcopy(values))
            self._changed()
            return self

    # -- game directories -------------------------------------------------

    @property
    def game_dirs(self) -> list[str]:
        with self._lock:
            return list(self.data.get("game_dirs", []))

    def set_game_dirs(self, directories: list[str | Path]) -> "Config":
        with self._lock:
            self.data["game_dirs"] = [str(Path(item).expanduser()) for item in directories]
            self._changed()
            return self

    def add_game_dir(self, directory: str | Path) -> "Config":
        value = str(Path(directory).expanduser())
        with self._lock:
            dirs = self.data.setdefault("game_dirs", [])
            if value not in dirs:
                dirs.append(value)
                self._changed()
            return self

    def remove_game_dir(self, directory: str | Path) -> "Config":
        value = str(Path(directory).expanduser())
        with self._lock:
            dirs = self.data.setdefault("game_dirs", [])
            self.data["game_dirs"] = [item for item in dirs if item != value]
            self._changed()
            return self

    # -- UI preferences -------------------------------------------------

    def ui_get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return copy.deepcopy(self.data.get("ui", {}).get(key, default))

    def ui_set(self, key: str, value: Any) -> "Config":
        with self._lock:
            self.data.setdefault("ui", {})[key] = copy.deepcopy(value)
            self._changed()
            return self

    # -- learned per-game launch configurations -------------------------

    def get_game(self, game_id: str) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self.data.get("games", {}).get(game_id, {}))

    def set_game(self, game_id: str, values: dict[str, Any]) -> "Config":
        with self._lock:
            self.data.setdefault("games", {})[game_id] = copy.deepcopy(values)
            self._changed()
            return self

    def update_game(self, game_id: str, values: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            current = self.data.setdefault("games", {}).get(game_id, {})
            if not isinstance(current, dict):
                current = {}
            current.update(copy.deepcopy(values))
            self.data["games"][game_id] = current
            self._changed()
            return copy.deepcopy(current)

    def record_launch(
        self,
        game_id: str,
        *,
        machine: str | None = None,
        profile: str | None = None,
        tos: str | Path | None = None,
        args: list[str] | None = None,
        resolution: tuple[int, int] | list[int] | None = None,
        status: str,
        failure_reason: str | None = None,
        message: str | None = None,
        success: bool | None = None,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Merge a launch observation into the learned record for ``game_id``.

        ``last_success`` only moves when a launch succeeded; failed attempts
        retain the previous working configuration while updating the status and
        failure reason.
        """
        now = int(time.time())
        with self._lock:
            previous = self.data.setdefault("games", {}).get(game_id, {})
            if not isinstance(previous, dict):
                previous = {}
            learned = copy.deepcopy(previous)
            learned.update(
                {
                    "machine": (
                        machine if machine is not None else previous.get("machine")
                    ),
                    "profile": (
                        profile if profile is not None else previous.get("profile")
                    ),
                    "tos": str(tos) if tos is not None else previous.get("tos"),
                    "args": (
                        list(args) if args is not None else previous.get("args", [])
                    ),
                    "observed_resolution": (
                        list(resolution)
                        if resolution is not None
                        else previous.get("observed_resolution")
                    ),
                    "status": status,
                    "failure_reason": failure_reason,
                    "message": message,
                    "last_attempt": now,
                    "attempts": int(previous.get("attempts", 0)) + 1,
                }
            )
            if success is None:
                success = status == "success"
            if success:
                learned["last_success"] = now
                learned["last_success_config"] = {
                    "machine": learned["machine"],
                    "profile": learned["profile"],
                    "tos": learned["tos"],
                    "args": learned["args"],
                    "observed_resolution": learned["observed_resolution"],
                }
            else:
                learned["last_success"] = previous.get("last_success")
                learned["last_success_config"] = previous.get("last_success_config")
            if extra:
                learned.update(copy.deepcopy(extra))
            self.data["games"][game_id] = learned
            self._changed()
            return copy.deepcopy(learned)

    def learned_launch(self, game_id: str) -> dict[str, Any]:
        """Best known launch configuration, falling back to the last success."""
        with self._lock:
            record = self.data.setdefault("games", {}).get(game_id, {})
            if not isinstance(record, dict):
                return {}
            success = record.get("last_success_config")
            if isinstance(success, dict):
                return copy.deepcopy(success)
            return {
                key: record.get(key)
                for key in ("machine", "profile", "tos", "args", "observed_resolution")
                if record.get(key) is not None
            }


def load(path: str | Path | None = None) -> Config:
    return Config(path)
