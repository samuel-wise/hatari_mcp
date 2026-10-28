"""Floppy, hard-disk and cartridge media tools."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from ..app import App
from ..errors import error
from ._common import guarded

FLOPPY_EXTENSIONS = {".st", ".msa"}
CAPSLIB_FLOPPY_EXTENSIONS = {".stx", ".ipf"}
HD_KINDS = {"acsi", "scsi", "ide-master", "ide-slave"}
FLOPPY_SECTOR_SIZE = 512
MSA_MAGIC = b"\x0e\x0f"


def escape_option_arg(value: str) -> str:
    """Escape whitespace for Hatari's hatari-option command parser.

    ``Change_ApplyCommandline()`` splits on whitespace unless the whitespace
    is immediately preceded by a backslash, which it removes.  Backslashes are
    therefore not escaped themselves; doing so would change paths that already
    contain a backslash.
    """
    if "\n" in value or "\r" in value:
        raise error("bad_option", "Paths must not contain newlines.", "Use a single-line path.")
    if value != value.strip():
        raise error(
            "bad_option",
            "Paths with leading or trailing whitespace cannot be passed to Hatari.",
            "Remove the surrounding whitespace from the path.",
        )
    escaped: list[str] = []
    for char in value:
        if char in {" ", "\t"}:
            escaped.append("\\")
        escaped.append(char)
    return "".join(escaped)


def normalize_drive(drive: str) -> str:
    normalized = drive.strip().lower()
    if normalized not in {"a", "b"}:
        raise error("bad_option", f"Invalid floppy drive: {drive!r}", "Use drive a or b.")
    return normalized


def normalize_hd_kind(kind: str) -> str:
    normalized = kind.strip().lower().replace("_", "-")
    aliases = {
        "ide-master": "ide-master",
        "idemaster": "ide-master",
        "ide0": "ide-master",
        "ide-slave": "ide-slave",
        "ideslave": "ide-slave",
        "ide1": "ide-slave",
    }
    normalized = aliases.get(normalized, normalized)
    if normalized not in HD_KINDS:
        raise error(
            "bad_option",
            f"Invalid hard-disk target: {kind!r}",
            "Use acsi, scsi, ide-master or ide-slave.",
        )
    return normalized


def normalize_hd_id(kind: str, device_id: int | None) -> int | None:
    if kind in {"acsi", "scsi"}:
        if device_id is None:
            return 0
        if not 0 <= int(device_id) <= 7:
            raise error("bad_option", "ACSI/SCSI ids must be 0-7.", "Pass id=0..7.")
        return int(device_id)
    if device_id is None:
        return None
    expected = 0 if kind == "ide-master" else 1
    if int(device_id) != expected:
        raise error(
            "bad_option",
            f"{kind} uses IDE id {expected}, not {device_id}.",
            "Omit id for IDE targets or pass the matching 0/1 value.",
        )
    return expected


def build_floppy_option(drive: str, path: str) -> str:
    normalized = normalize_drive(drive)
    return f"--disk-{normalized} {escape_option_arg(path)}"


def build_floppy_eject_option(drive: str) -> str:
    normalized = normalize_drive(drive)
    return f"--disk-{normalized} none"


def build_hd_mount_option(kind: str, path: str, device_id: int | None = None) -> str:
    normalized = normalize_hd_kind(kind)
    target_id = normalize_hd_id(normalized, device_id)
    escaped = escape_option_arg(path)
    if normalized in {"acsi", "scsi"}:
        return f"--{normalized} {target_id}={escaped}"
    return f"--{normalized} {escaped}"


def build_hd_unmount_option(kind: str, device_id: int | None = None) -> str:
    normalized = normalize_hd_kind(kind)
    target_id = normalize_hd_id(normalized, device_id)
    if normalized in {"acsi", "scsi"}:
        return f"--{normalized} {target_id}=none"
    return f"--{normalized} none"


def build_cartridge_option(path: str) -> str:
    return f"--cartridge {escape_option_arg(path)}"


def _check_readable(path: Path) -> None:
    try:
        with path.open("rb") as handle:
            handle.read(1)
    except OSError as exc:
        raise error(
            "invalid_path",
            f"File is not readable: {path}",
            "Check permissions and that the path is a regular file.",
            detail=str(exc),
        ) from exc


def _require_file(path: Path) -> None:
    if not path.exists():
        raise error("invalid_path", f"File does not exist: {path}", "Check the path.")
    if not path.is_file():
        raise error("invalid_path", f"Path is not a file: {path}", "Pass an image file.")
    _check_readable(path)


def validate_floppy_image(path: str | Path) -> Path:
    target = Path(path)
    _require_file(target)
    suffix = target.suffix.lower()
    if suffix in CAPSLIB_FLOPPY_EXTENSIONS:
        raise error(
            "unsupported_format",
            f"{suffix} floppy images need a capslib-enabled Hatari build.",
            "Use .st or .msa, or probe the build's capslib support before mounting.",
            detail="[R] build-dependent format",
        )
    if suffix not in FLOPPY_EXTENSIONS:
        raise error(
            "unsupported_format",
            f"Unsupported floppy image extension: {suffix or '<none>'}",
            "Use .st or .msa.",
        )
    size = target.stat().st_size
    if size <= 0:
        raise error("invalid_image", f"Floppy image is empty: {target}", "Use a non-empty image.")
    if suffix == ".msa":
        if size < 10 or target.read_bytes()[:2] != MSA_MAGIC:
            raise error(
                "invalid_image",
                f"Not a valid MSA image: {target}",
                "The file must start with the MSA 0x0e0f magic.",
            )
    elif size % FLOPPY_SECTOR_SIZE:
        raise error(
            "invalid_image",
            f".st image size ({size} bytes) is not a multiple of {FLOPPY_SECTOR_SIZE}.",
            "Use a raw ST sector image.",
        )
    else:
        _validate_st_boot_sector(target)
    return target


def _validate_st_boot_sector(target: Path) -> None:
    """Reject images Hatari would stop on with a modal Pacifist/Makedisk alert.

    Hatari's Floppy_IsBootSectorOK() accepts a boot sector when byte 13 (FAT
    sectors-per-cluster) is non-zero or the 16-bit sum of all 256 words is
    0x1234.  A fully zeroed .st image satisfies neither, and inserting it opens
    a blocking alert dialog in the headless session.
    """
    boot = target.read_bytes()[:FLOPPY_SECTOR_SIZE]
    if boot[13] != 0:
        return
    checksum = sum((boot[i] << 8) + boot[i + 1] for i in range(0, FLOPPY_SECTOR_SIZE, 2)) & 0xFFFF
    if checksum == 0x1234:
        return
    raise error(
        "invalid_image",
        f"Floppy boot sector looks invalid or zeroed: {target}",
        "Use a formatted .st image. Hatari would open a blocking Pacifist/Makedisk "
        "warning dialog for a boot sector with sectors-per-cluster 0 and a "
        "non-executable checksum.",
    )


def validate_hd_image(path: str | Path) -> Path:
    target = Path(path)
    _require_file(target)
    size = target.stat().st_size
    if size <= 0:
        raise error("invalid_image", f"Hard-disk image is empty: {target}", "Use a non-empty image.")
    if size % 512:
        raise error(
            "invalid_image",
            f"Hard-disk image size ({size} bytes) is not a multiple of 512.",
            "Use a raw sector/block image.",
        )
    return target


def validate_cartridge_image(path: str | Path) -> Path:
    target = Path(path)
    _require_file(target)
    if target.stat().st_size <= 0:
        raise error("invalid_image", f"Cartridge image is empty: {target}", "Use a non-empty ROM image.")
    return target


async def disable_modal_alerts(app: App) -> None:
    """Keep headless Hatari from opening SDL alert dialogs on log notices.

    The server's generated config currently sets nAlertDlgLogLevel to LOG_TODO,
    so LOG_INFO successes (recording started) and LOG_WARN notices open a modal
    dialog.  Setting the alert level to 'fatal' keeps the control channel and
    emulation responsive.  This is a defensive workaround; the core config
    should set a lower alert level.
    """
    await app.set_option("--alert-level fatal")


def _restart_required(option: str, message: str, hint: str) -> None:
    raise error(
        "restart_required",
        message,
        hint,
        extra={"option": option, "command": f"hatari-option {option}"},
    )


def register(server: MCPServer, app: App) -> None:
    @server.tool(
        name="media_insert_floppy",
        description=(
            "Insert a .st/.msa floppy image into drive A or B at runtime with "
            "hatari-option --disk-a/--disk-b. The file is validated before the command is sent."
        ),
        annotations=ToolAnnotations(destructiveHint=False, openWorldHint=False),
    )
    @guarded
    async def media_insert_floppy(drive: str, path: str) -> dict[str, Any]:
        normalized = normalize_drive(drive)
        target = validate_floppy_image(app.guard_path(path))
        option = build_floppy_option(normalized, str(target))
        await disable_modal_alerts(app)
        await app.set_option(option)
        return {
            "ok": True,
            "drive": normalized.upper(),
            "path": str(target),
            "option": option,
        }

    @server.tool(
        name="media_eject_floppy",
        description=(
            "Eject the floppy in drive A or B with hatari-option --disk-a none / "
            "--disk-b none. Hatari treats the literal string 'none' as eject."
        ),
        annotations=ToolAnnotations(destructiveHint=True, openWorldHint=False),
    )
    @guarded
    async def media_eject_floppy(drive: str) -> dict[str, Any]:
        normalized = normalize_drive(drive)
        option = build_floppy_eject_option(normalized)
        await app.set_option(option)
        return {
            "ok": True,
            "drive": normalized.upper(),
            "ejected": True,
            "option": option,
        }

    @server.tool(
        name="media_mount_hd",
        description=(
            "Validate and build the Hatari option for an ACSI/SCSI (id 0-7) or "
            "IDE master/slave hard-disk image. Hatari applies HD changes with a cold "
            "reset, so this tool returns restart_required instead of opening a reset "
            "dialog in the live headless session. The option is not included in MCP "
            "ToolError text; supply the disk option explicitly in a fresh emu_start."
        ),
        annotations=ToolAnnotations(destructiveHint=False, openWorldHint=False),
    )
    @guarded
    async def media_mount_hd(kind: str, path: str, id: int | None = None) -> dict[str, Any]:
        normalized = normalize_hd_kind(kind)
        normalize_hd_id(normalized, id)
        target = validate_hd_image(app.guard_path(path))
        option = build_hd_mount_option(normalized, str(target), id)
        _restart_required(
            option,
            f"Mounting a {normalized} hard disk requires a Hatari cold reset.",
            "Restart with the disk option supplied explicitly in a fresh emu_start so Hatari can reset safely.",
        )
        return {"ok": True}

    @server.tool(
        name="media_unmount_hd",
        description=(
            "Build the Hatari option to detach an ACSI/SCSI/IDE hard-disk image. "
            "Hatari's option parser accepts 'none', but the change requires a cold reset, "
            "so this tool returns restart_required rather than toggling the live session."
        ),
        annotations=ToolAnnotations(destructiveHint=True, openWorldHint=False),
    )
    @guarded
    async def media_unmount_hd(kind: str, id: int | None = None) -> dict[str, Any]:
        normalized = normalize_hd_kind(kind)
        normalize_hd_id(normalized, id)
        option = build_hd_unmount_option(normalized, id)
        _restart_required(
            option,
            f"Detaching a {normalized} hard disk requires a Hatari cold reset.",
            "Hatari accepts 'none' as a detach value, but it is not a safe live hot-swap. "
            "Restart with the detach option supplied explicitly in a fresh emu_start.",
        )
        return {"ok": True}

    @server.tool(
        name="media_insert_cartridge",
        description=(
            "Validate a cartridge ROM and build hatari-option --cartridge. Cartridges "
            "are executed at boot, so insertion requires a restart; the live session is "
            "never modified. A cartridge is privileged guest code."
        ),
        annotations=ToolAnnotations(destructiveHint=True, openWorldHint=False),
    )
    @guarded
    async def media_insert_cartridge(path: str) -> dict[str, Any]:
        target = validate_cartridge_image(app.guard_path(path))
        option = build_cartridge_option(str(target))
        _restart_required(
            option,
            "Cartridge images are loaded at boot and require a Hatari restart.",
            "Restart with the cartridge option supplied explicitly in a fresh emu_start. "
            "Cartridge code runs before TOS, so treat the image as privileged guest code.",
        )
        return {"ok": True}

    @server.tool(
        name="media_eject_cartridge",
        description=(
            "Cartridge eject has no runtime option: Hatari rejects 'none' for --cartridge, "
            "and there is no control command for it. Restart without --cartridge to eject."
        ),
        annotations=ToolAnnotations(destructiveHint=True, openWorldHint=False),
    )
    @guarded
    async def media_eject_cartridge() -> dict[str, Any]:
        raise error(
            "restart_required",
            "Stock Hatari has no runtime cartridge eject.",
            "Restart Hatari without --cartridge. --cartridge none is rejected because "
            "the option uses CHECK_FILE and has no enable/disable pointer.",
            extra={"supported": False, "mechanism": "restart without --cartridge"},
        )

    @server.tool(
        name="media_status",
        description=(
            "Return the debugger's GEMDOS, ACIA and video information as raw lines. "
            "This is read-only and does not change media."
        ),
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False),
    )
    @guarded
    async def media_status() -> dict[str, Any]:
        sections: dict[str, list[str]] = {}
        raw: dict[str, str] = {}
        for name in ("gemdos", "acia", "video"):
            result = await app.supervisor.debug(f"info {name}")
            sections[name] = list(result.get("lines", []))
            raw[name] = result.get("raw", "")
        return {
            "ok": True,
            "gemdos": sections["gemdos"],
            "acia": sections["acia"],
            "video": sections["video"],
            "raw": raw,
        }
