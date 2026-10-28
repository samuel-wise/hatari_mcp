"""Application context shared by all MCP tools."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .config import Settings
from .errors import error
from .profiles import select_profile
from .supervisor import Supervisor


class App:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or Settings.load()
        self.supervisor = Supervisor(self.settings)
        self.active_profile: str | None = None

    async def shutdown(self) -> None:
        await self.supervisor.shutdown()

    def status(self) -> dict[str, Any]:
        status = self.supervisor.status()
        status["profile"] = self.active_profile
        status["hatari_bin"] = str(self.settings.hatari_bin) if self.settings.hatari_bin else None
        status["tos"] = str(self.settings.default_tos) if self.settings.default_tos else None
        return status

    async def start(
        self,
        *,
        profile: str | None = None,
        args: list[str] | None = None,
        tos: str | None = None,
        display: bool = False,
        workspace: str | None = None,
        autostart: str | None = None,
        gemdos_drive: str = "C",
        gemdos_case: str = "off",
        gemdos_time: str = "atari",
        gemdos_conv: str = "off",
        write_protect: str = "off",
        natfeats: bool = False,
    ) -> dict[str, Any]:
        prof = select_profile(self.settings.profiles_dir, profile)
        self.active_profile = (prof or {}).get("name") or profile
        launch_args: list[str] = []
        env: dict[str, str] = {}
        tos_path: Path | None = Path(tos).expanduser() if tos else None
        if prof:
            launch_args.extend(prof["launch"].get("args", []))
            env.update(prof["launch"].get("env", {}))
            if not display:
                display = bool(prof["launch"].get("display", False))
            if not tos_path and prof["paths"].get("tos"):
                tos_path = Path(prof["paths"]["tos"])
        launch_args.extend(args or [])
        if tos_path is None:
            tos_path = self.settings.default_tos

        if workspace:
            ws = Path(workspace).expanduser().resolve()
            if not ws.is_dir():
                raise error(
                    "invalid_path",
                    f"Workspace directory does not exist: {ws}",
                    "Create the directory or pass an existing host directory.",
                )
            drive = gemdos_drive.strip().upper()
            if len(drive) != 1 or drive not in "CDEFGHIJKLMNOPQRSTUVWXYZ":
                raise error("bad_option", f"Invalid GEMDOS drive: {gemdos_drive!r}", "Use C-Z.")
            if gemdos_case not in {"off", "upper", "lower"}:
                raise error("bad_option", f"Invalid gemdos_case: {gemdos_case!r}", "Use off, upper or lower.")
            if gemdos_time not in {"atari", "host"}:
                raise error("bad_option", f"Invalid gemdos_time: {gemdos_time!r}", "Use atari or host.")
            if gemdos_conv not in {"off", "on"}:
                raise error("bad_option", f"Invalid gemdos_conv: {gemdos_conv!r}", "Use off or on.")
            if write_protect not in {"off", "on", "auto"}:
                raise error("bad_option", f"Invalid write_protect: {write_protect!r}", "Use off, on or auto.")
            launch_args += [
                "--harddrive",
                str(ws),
                "--gemdos-drive",
                drive,
                "--gemdos-case",
                gemdos_case,
                "--gemdos-time",
                gemdos_time,
                "--gemdos-conv",
                gemdos_conv,
                "--protect-hd",
                write_protect,
            ]
            if autostart:
                auto = Path(autostart)
                if not auto.is_absolute():
                    auto = ws / auto
                auto = auto.expanduser().resolve()
                try:
                    rel = auto.relative_to(ws)
                except ValueError:
                    raise error(
                        "path_not_allowed",
                        f"Autostart program is outside the workspace: {auto}",
                        "Copy the program into the workspace or add it to a later multi-root workspace.",
                    ) from None
                if not auto.is_file():
                    raise error("invalid_path", f"Autostart program does not exist: {auto}", "Check the path.")
                guest = drive + ":\\" + str(rel).replace("/", "\\")
                if gemdos_case == "upper":
                    guest = guest.upper()
                elif gemdos_case == "lower":
                    guest = guest.lower()
                launch_args += ["--auto", guest, "--symload", "exec"]
        elif autostart:
            raise error(
                "bad_option",
                "autostart requires a workspace.",
                "Pass workspace=<host dir> and a program inside it.",
            )

        return await self.supervisor.start(
            args=launch_args,
            tos=tos_path,
            env=env,
            display=display,
            natfeats=natfeats,
        )

    async def set_option(self, options: str) -> dict[str, Any]:
        if not options.strip():
            raise error("bad_option", "No options supplied.", "Pass Hatari CLI options as one line.")
        if "\n" in options or "\r" in options:
            raise error("bad_option", "Options must be one line.", "Send a single option string.")
        if self.supervisor.state.value == "at_prompt":
            raise error(
                "not_stopped",
                "Hatari is at the debugger prompt; control options are queued.",
                "Continue or quit the debugger before changing emulator options.",
            )
        await self.supervisor._socket_command("hatari-option " + options)
        return {"ok": True, "options": options}

    def guard_path(self, path: str | Path, *, must_exist: bool = False) -> Path:
        resolved = Path(path).expanduser().resolve()
        if must_exist and not resolved.exists():
            raise error("invalid_path", f"Path does not exist: {resolved}", "Check the path.")
        return resolved

    def resolve_artifact(self, path: str | None, default_name: str) -> Path:
        if path:
            return self.guard_path(path)
        base = self.supervisor.runtime.path if self.supervisor.runtime else Path("/tmp")
        return base / default_name

    def env_default(self, key: str) -> str | None:
        return os.environ.get(key)
