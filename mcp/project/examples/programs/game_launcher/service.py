"""Launch orchestration around ``GameSession``.

This module owns the blocking MCP work and is meant to be called from a worker
thread (the Tk application uses ``common.ui.TaskRunner``).  It keeps the MCP
server alive between games so only a machine/TOS change needs a full Hatari
restart; otherwise it uses ``GameSession.swap_game`` for an in-place disk swap.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

_PROGRAMS = Path(__file__).resolve().parent.parent
if str(_PROGRAMS) not in sys.path:
    sys.path.insert(0, str(_PROGRAMS))

from common import Config, paths  # noqa: E402
from common import config as config_module  # noqa: E402
from common.game_runner import (  # noqa: E402
    GameResult,
    GameSession,
    normalise_launch_path,
)
from common.mcp_client import MCPClient  # noqa: E402

from . import profiles  # noqa: E402
from .metadata import MetadataResolver, ResolvedMetadata  # noqa: E402


@dataclass
class LaunchOutcome:
    result: GameResult
    profile: profiles.LaunchProfile
    metadata: ResolvedMetadata
    action: str
    explanation: str
    evidence_path: str | None = None
    recorded: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "result": self.result.as_dict(),
            "profile": self.profile.as_dict(),
            "metadata": self.metadata.as_dict(),
            "action": self.action,
            "explanation": self.explanation,
            "evidence_path": self.evidence_path,
        }


def _entry_value(entry: Any, key: str, default: Any = None) -> Any:
    if isinstance(entry, dict):
        return entry.get(key, default)
    return getattr(entry, key, default)


def _truncate(value: Any, depth: int = 0) -> Any:
    if depth >= 4:
        return "<truncated>"
    if isinstance(value, dict):
        return {str(key): _truncate(item, depth + 1) for key, item in value.items()}
    if isinstance(value, list):
        return [_truncate(item, depth + 1) for item in value[:40]]
    if isinstance(value, str):
        return value if len(value) <= 800 else value[:800] + "..."
    return value


class LauncherService:
    """One MCP client, one game session, testable without a Tk root."""

    def __init__(
        self,
        *,
        config: Config | None = None,
        state_dir: str | Path | None = None,
        metadata: MetadataResolver | None = None,
        client_factory: Callable[[], Any] | None = None,
        session_factory: Callable[..., Any] | None = None,
        default_machine: str = "st",
        boot_timeout: float = 45.0,
        disk_mode: str = "symlink",
        display: bool = True,
        turbo: bool = True,
        hatari_bin: str | Path | None = None,
        tos_dirs: list[str | Path] | None = None,
        tos_candidates: dict[str, Path] | None = None,
    ):
        self.config = config or Config()
        self.state_dir = (
            Path(state_dir).expanduser().resolve() if state_dir else paths.state_dir()
        )
        self.metadata = metadata or MetadataResolver(state_dir=self.state_dir)
        self._client_factory = client_factory
        self._session_factory = session_factory or GameSession
        self.default_machine = default_machine
        self.boot_timeout = float(boot_timeout)
        self.disk_mode = disk_mode
        self.display = bool(display)
        self.turbo = bool(turbo)
        self.hatari_bin = (
            str(Path(hatari_bin).expanduser().resolve()) if hatari_bin else None
        )
        self.tos_dirs = (
            [str(Path(item).expanduser().resolve()) for item in tos_dirs]
            if tos_dirs is not None
            else None
        )
        self.tos_candidates = (
            dict(tos_candidates) if tos_candidates is not None else None
        )
        self._lock = threading.RLock()
        self._client: Any = None
        self._client_hatari_bin: str | None = None
        self._session: Any = None
        self.last_outcome: LaunchOutcome | None = None

    # -- lifecycle -----------------------------------------------------

    @property
    def client(self) -> Any:
        return self._client

    @property
    def session(self) -> Any:
        return self._session

    def set_paths(
        self,
        *,
        hatari_bin: str | Path | None,
        tos_dirs: list[str | Path],
        tos_candidates: dict[str, Path] | None,
    ) -> None:
        """Update configured paths for future sessions and profile lookups."""
        with self._lock:
            self.hatari_bin = (
                str(Path(hatari_bin).expanduser().resolve()) if hatari_bin else None
            )
            self.tos_dirs = [
                str(Path(item).expanduser().resolve()) for item in tos_dirs
            ]
            self.tos_candidates = (
                dict(tos_candidates) if tos_candidates is not None else None
            )

    def _new_client(self) -> Any:
        if self._client_factory is not None:
            return self._client_factory()
        env = {"HATARI_BIN": self.hatari_bin} if self.hatari_bin else None
        return MCPClient(env=env)

    def _ensure_session(self) -> Any:
        wanted_bin = self.hatari_bin or ""
        if self._client is not None and self._client_hatari_bin != wanted_bin:
            running = self._session is not None and bool(self._session.running)
            if not running:
                self.close()
        if self._client is None:
            client = self._new_client()
            client.start()
            self._client = client
            self._client_hatari_bin = wanted_bin
        if self._session is None:
            self._session = self._session_factory(
                self._client,
                config=self.config,
                state_dir=self.state_dir,
                default_machine=self.default_machine,
                boot_timeout=self.boot_timeout,
                disk_mode=self.disk_mode,
                display=self.display,
                turbo=self.turbo,
            )
        else:
            self._session.turbo = self.turbo
        return self._session

    def status(self) -> dict[str, Any]:
        with self._lock:
            if self._session is not None:
                try:
                    return dict(self._session._status())
                except BaseException:
                    pass
            if self._client is None:
                return {"state": "not_started"}
            try:
                return dict(self._client.call_tool("emu_status", timeout=5.0))
            except BaseException as exc:
                return {"state": "unknown", "error": str(exc)}

    # -- launch --------------------------------------------------------

    def launch(
        self,
        entry: Any,
        *,
        requested_machine: str | None = None,
        requested_tos: str | Path | None = None,
        timeout: float | None = None,
        launch_path: str | None = None,
    ) -> LaunchOutcome:
        with self._lock:
            try:
                guest_path = normalise_launch_path(launch_path)
            except ValueError:
                guest_path = str(launch_path).strip() or None
            resolved = self.metadata.resolve(entry)
            profile = profiles.choose_profile(
                entry,
                config=self.config,
                metadata=resolved,
                requested_machine=requested_machine,
                requested_tos=requested_tos,
                tos_candidates=self.tos_candidates,
            )
            session = self._ensure_session()
            running = bool(session.running)
            current_id = getattr(session, "current_game_id", None)
            reason = profiles.incompatibility_reason(
                getattr(session, "current_machine", None),
                getattr(session, "current_tos", None),
                profile,
                current_game_id=current_id,
            )

            if running and reason is None and not guest_path:
                action = "swap"
                explanation = (
                    f"hot disk swap on the running {profile.machine} machine "
                    f"({profile.tos_name})"
                )
                result = session.swap_game(
                    entry,
                    machine=profile.machine,
                    tos=profile.tos,
                    args=list(profile.extra_args),
                    timeout=timeout,
                )
            else:
                if running:
                    action = "restart"
                    if reason is None:
                        explanation = (
                            "launch path needs a fresh boot; stopping and restarting Hatari"
                        )
                    else:
                        explanation = f"{reason}; stopping and restarting Hatari"
                    session.stop()
                else:
                    action = "start"
                    explanation = (
                        f"cold start: {profile.machine} / {profile.tos_name}"
                    )
                result = session.start_game(
                    entry,
                    machine=profile.machine,
                    profile=profile.profile,
                    tos=profile.tos,
                    args=list(profile.extra_args),
                    timeout=timeout,
                    autostart=guest_path,
                )

            evidence_path = self._write_evidence(
                result, profile, resolved, action, launch_path=guest_path
            )
            recorded = self._record(
                result,
                profile,
                resolved,
                action,
                explanation,
                evidence_path,
                launch_path=guest_path,
            )
            outcome = LaunchOutcome(
                result=result,
                profile=profile,
                metadata=resolved,
                action=action,
                explanation=explanation,
                evidence_path=evidence_path,
                recorded=recorded,
            )
            self.last_outcome = outcome
            return outcome

    def switch_disk(
        self,
        entry: Any,
        *,
        drive: str = "a",
        timeout: float | None = None,
    ) -> GameResult:
        """Insert another disk into the running machine without a reset."""
        with self._lock:
            session = self._ensure_session()
            result = session.switch_disk(
                entry, drive=drive, timeout=timeout or 15.0
            )
            if result.ok:
                try:
                    self.config.update_game(
                        result.game_id,
                        {
                            "last_disk": {
                                "id": str(getattr(entry, "id", "") or ""),
                                "filename": str(getattr(entry, "filename", "") or ""),
                                "drive": drive.lower(),
                                "path": result.evidence.get("disk_path"),
                                "at": int(time.time()),
                            }
                        },
                    )
                except OSError:
                    pass
            return result

    def _write_evidence(
        self,
        result: GameResult,
        profile: profiles.LaunchProfile,
        metadata: ResolvedMetadata,
        action: str,
        *,
        launch_path: str | None = None,
    ) -> str | None:
        game_id = result.game_id or "unknown"
        directory = self.state_dir / "run" / game_id
        try:
            directory.mkdir(parents=True, exist_ok=True)
            target = directory / "evidence.json"
            payload = {
                "recorded_at": int(time.time()),
                "action": action,
                "result": result.as_dict(),
                "profile": profile.as_dict(),
                "metadata_provenance": metadata.provenance,
                "art_source": metadata.art_source,
                "launch_path": launch_path,
            }
            config_module.atomic_write_json(target, _truncate(payload))
            return str(target)
        except OSError:
            return None

    def _record(
        self,
        result: GameResult,
        profile: profiles.LaunchProfile,
        metadata: ResolvedMetadata,
        action: str,
        explanation: str,
        evidence_path: str | None,
        *,
        launch_path: str | None = None,
    ) -> dict[str, Any]:
        game_id = result.game_id or ""
        if not game_id or self.config is None:
            return {}
        launcher = {
            "action": action,
            "explanation": explanation,
            "profile_source": profile.source,
            "tos_source": profile.tos_source,
            "machine": profile.machine,
            "profile": profile.profile,
            "tos": str(profile.tos) if profile.tos else None,
            "expected_resolution": (
                list(profile.expected_resolution)
                if profile.expected_resolution
                else None
            ),
            "observed_resolution": (
                list(result.resolution) if result.resolution else None
            ),
            "extra_args": list(profile.extra_args),
            "metadata_provenance": dict(metadata.provenance),
            "art_source": metadata.art_source,
            "evidence_path": evidence_path,
            "launch_path": launch_path,
        }
        updates: dict[str, Any] = {
            "launcher": launcher,
            "launch_path": launch_path,
            "last_result": {
                "ok": result.ok,
                "status": result.status,
                "message": result.message,
                "failure_reason": result.failure_reason,
                "resolution": list(result.resolution) if result.resolution else None,
                "frame_path": result.frame_path,
                "evidence_path": evidence_path,
                "action": action,
                "at": int(time.time()),
            },
        }
        if result.ok:
            updates["last_success_launcher"] = launcher
        else:
            updates["last_failure_config"] = {
                "machine": profile.machine,
                "profile": profile.profile,
                "tos": str(profile.tos) if profile.tos else None,
                "extra_args": list(profile.extra_args),
                "profile_source": profile.source,
                "tos_source": profile.tos_source,
                "failure_reason": result.failure_reason,
                "message": result.message,
                "evidence_path": evidence_path,
                "launch_path": launch_path,
            }
        try:
            return self.config.update_game(game_id, updates)
        except OSError:
            return {}

    # -- stop/close ----------------------------------------------------

    def stop(self) -> dict[str, Any]:
        with self._lock:
            if self._session is None:
                return {"ok": True, "state": "stopped", "note": "no session"}
            return self._session.stop()

    def close(self, *, timeout: float | None = None) -> None:
        with self._lock:
            try:
                if self._session is not None:
                    self._session.stop()
            finally:
                if self._client is not None:
                    self._client.stop(timeout=timeout)
                self._session = None
                self._client = None
                self._client_hatari_bin = None

    def __enter__(self) -> LauncherService:
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()


def read_evidence(path: str | Path | None, *, limit: int = 4000) -> str:
    """Return a bounded, human-readable evidence snapshot for the UI."""
    if not path:
        return ""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return ""
    try:
        parsed = json.loads(text)
    except ValueError:
        return text[:limit]
    return json.dumps(parsed, indent=2, sort_keys=True)[:limit]
