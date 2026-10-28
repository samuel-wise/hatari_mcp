"""Scenario schema, matrix expansion, runner, artifacts and CI reports.

This module is the P6 engine.  It deliberately keeps the public surface small:

* :func:`load_scenario` / :func:`validate_scenario` implement scenario v1.
* :func:`expand_matrix` produces deterministic cases.
* :class:`ScenarioEngine` runs cases (over a real MCP stdio server by default).
* :class:`ArtifactBundle` writes atomic, checksummed bundles.
* :func:`build_junit_xml` / :func:`build_report` implement CI output.

The module has no dependency on the MCP request layer, so it can be used from
the CLI, from ``tools/automation.py`` and from tests with a fake backend.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import hashlib
import json
import math
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence

from .. import __version__
from ..errors import HatariError

SCENARIO_SCHEMA_VERSION = 1
REPORT_SCHEMA = "hatari-mcp.report/1"
REPLAY_SCHEMA = "hatari-mcp.replay/1"
MANIFEST_SCHEMA = "hatari-mcp.manifest/1"

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2
EXIT_INFRA = 3
EXIT_WATCHDOG = 4
EXIT_INTERNAL = 5
EXIT_INTERRUPTED = 130

DEFAULT_SCENARIO_TIMEOUT_MS = 120_000
DEFAULT_STEP_TIMEOUT_MS = 5_000
STEP_TIMEOUTS = {
    "emu_start": 30_000,
    "start": 30_000,
    "wait": 30_000,
    "input": 2_000,
    "capture": 10_000,
    "state": 10_000,
    "assert": 5_000,
    "screen_assert": 10_000,
    "teardown": 15_000,
}
MAX_STEP_TIMEOUT_MS = 3_600_000
MAX_SCENARIO_TIMEOUT_MS = 24 * 3600_000
DEFAULT_MAX_BUNDLE_BYTES = 50 * 1024 * 1024
DEFAULT_MAX_RUN_BYTES = 500 * 1024 * 1024
DEFAULT_MAX_CASE_BYTES = 256 * 1024 * 1024
STAGING_CHUNK_BYTES = 1024 * 1024
MAX_TEARDOWN_SECONDS = 30.0
DEFAULT_MAX_CASES = 256
MAX_SCENARIO_DEPTH = 2
SCREEN_FORMATS = {"bmp", "png"}
INPUT_TOOLS = {
    "input_key",
    "input_text",
    "input_key_down",
    "input_key_up",
    "input_mouse_move",
    "input_mouse_button",
    "input_joystick",
    "input_sequence",
    "input_key_combo",
    "input_mouse_click",
    "input_replay",
}

# A scenario is executable configuration, not a shell.  Only these tools may be
# invoked from a scenario step by default.  Everything privileged or capable of
# writing host files is deliberately absent: dbg_command, ahcc_*, scenario_run,
# emu_set_option/emu_set_path, dbg_memory_write/save, dbg_state_save/load,
# trace/profile writers, media_mount_hd and the session/attach tools.
SCENARIO_ALLOWED_TOOLS = {
    # lifecycle
    "emu_start",
    "emu_stop",
    "emu_status",
    "emu_pause",
    "emu_resume",
    "emu_wait_for_exit",
    "emu_reset",
    "emu_shortcut",
    "emu_capabilities",
    "emu_quit_debugger",
    # screen / read-only inspection
    "screen_frame_capture",
    "screen_screenshot",
    "screen_frame_info",
    "screen_frame_diff",
    "screen_frames_diff",
    "screen_info",
    "screen_pixel_get",
    "screen_region_hash",
    "screen_wait_stable",
    # input
    *INPUT_TOOLS,
    # logs and workspace
    "log_read",
    "console_read",
    "workspace_info",
    # debugger reads and execution control (no raw command, no file writes)
    "dbg_memory_read",
    "dbg_memory_find",
    "dbg_registers_get",
    "dbg_evaluate",
    "dbg_disassemble",
    "dbg_backtrace",
    "dbg_stack_dump",
    "dbg_exception_get",
    "dbg_symbols_list",
    "dbg_breakpoint_list",
    "dbg_breakpoint_set",
    "dbg_breakpoint_delete",
    "dbg_breakpoint_wait",
    "dbg_continue",
    "dbg_step",
    "dbg_step_over",
    "dbg_state_diff",
    "dbg_history",
    # safe media operations whose paths are still validated
    "media_status",
    "media_insert_floppy",
    "media_eject_floppy",
    "media_insert_cartridge",
    "media_eject_cartridge",
    "run_prg",
}
INPUT_TOOLS = frozenset(INPUT_TOOLS)

# Environment is never inherited wholesale by the MCP child.  These names may be
# forwarded; everything else stays in the parent process.
SAFE_ENV_KEYS = (
    "HATARI_BIN",
    "HATARI_TOS",
    "HATARI_MCP_REPO",
    "HATARI_MCP_PROFILES",
    "HATARI_MCP_GIT_COMMIT",
    "OKAMI_ROOT",
    "FREEMINT_ROOT",
    "GODLIB_ROOT",
    "PATH",
    "HOME",
    "LANG",
    "LC_ALL",
    "TZ",
)
# Only these environment values may widen the scenario's readable roots.
PATH_ENV_KEYS = (
    "HATARI_BIN",
    "HATARI_TOS",
    "HATARI_MCP_REPO",
    "HATARI_MCP_PROFILES",
    "OKAMI_ROOT",
    "FREEMINT_ROOT",
    "GODLIB_ROOT",
)

_SCENARIO_DEPTH: contextvars.ContextVar[int] = contextvars.ContextVar("hatari_scenario_depth", default=0)

VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
NAME_RE = re.compile(r"^[A-Za-z0-9_.:-]+$")
TOOL_RE = re.compile(r"^[a-z][a-z0-9_]*$")
SANITIZE_RE = re.compile(r"[^A-Za-z0-9_.-]+")

TOP_KEYS = {
    "version",
    "id",
    "name",
    "description",
    "requires",
    "determinism",
    "timeouts",
    "config",
    "matrix",
    "overrides",
    "steps",
    "teardown",
    "attempts",
    "retry_on",
    "tags",
    "vars",
    "env",
    "record",
    "stage",
    "seed",
    "skip_reason",
    "baseline",
}
REQUIRES_KEYS = {"env", "files", "rom", "tools", "skip_reason", "dirs"}
TIMEOUT_KEYS = {"scenario_ms", "step_ms"}
CONFIG_KEYS = {
    "profile",
    "tos",
    "workspace",
    "args",
    "autostart",
    "gemdos_drive",
    "gemdos_case",
    "gemdos_time",
    "gemdos_conv",
    "write_protect",
    "display",
}
STEP_KEYS = {
    "id",
    "tool",
    "args",
    "timeout_ms",
    "expect",
    "capture",
    "assert",
    "asserts",
    "description",
}
CAPTURE_KEYS = {"name", "kind", "format", "hash", "path", "resume"}
ASSERT_KEYS = {"path", "eq", "ne", "contains", "regex", "gte", "lte", "exists", "type"}
STAGE_KEYS = {"from", "to", "optional"}
OVERRIDE_KEYS = {"match", "timeouts", "config", "determinism", "env", "vars", "skip_reason"}
MATRIX_META_KEYS = {"max_cases", "fail_fast"}
DETERMINISM_MODES = {"strict", "tolerant", "off"}
ROM_REQUIREMENTS = {"emutos", "tos", "either"}

# Scenario-supplied Hatari options are allowlisted rather than denylisted.  A
# denylist is not safe here: ``--parse`` (arbitrary debugger commands),
# ``--debug``/``-D``, every short alias, media/host-drive options, recorders and
# output files can all write outside the per-case workspace.  Anything not in
# this exact allowlist is rejected, including long-option abbreviations and
# attached ``=value`` forms.
HATARI_SAFE_FLAG_OPTIONS = frozenset(
    {
        "--help",
        "--version",
        "--mono",
        "--fullscreen",
        "--window",
        "--grab",
        "--msg-repeat",
        "--benchmark",
    }
)
HATARI_SAFE_VALUE_OPTIONS = frozenset(
    {
        # general / display
        "--confirm-quit",
        "--country",
        "--layout",
        "--language",
        "--fast-forward",
        "--fast-forward-key-repeat",
        "--monitor",
        "--tos-res",
        "--resizable",
        "--frameskips",
        "--slowdown",
        "--mousewarp",
        "--statusbar",
        "--drive-led",
        "--max-width",
        "--max-height",
        "--zoom",
        "--vsync",
        "--disable-video",
        "--borders",
        "--spec512",
        "--video-timing",
        "--desktop",
        "--force-max",
        "--aspect",
        "--vdi",
        "--vdi-planes",
        "--vdi-width",
        "--vdi-height",
        "--crop",
        # safe device scalars (no host streams/media)
        "--joystick",
        "--joy0",
        "--joy1",
        "--joy2",
        "--joy3",
        "--joy4",
        "--joy5",
        "--drive-a",
        "--drive-b",
        "--drive-a-heads",
        "--drive-b-heads",
        "--fastfdc",
        "--protect-floppy",
        "--protect-hd",
        "--gemdos-case",
        "--gemdos-time",
        "--gemdos-conv",
        "--gemdos-drive",
        "--scsi-ver",
        "--acsi-ver",
        "--ide-swap",
        # memory / ROM / CPU / system
        "--memsize",
        "--ttram",
        "--tos",
        "--patch-tos",
        "--cpulevel",
        "--cpuclock",
        "--compatible",
        "--data-cache",
        "--cpu-exact",
        "--addr24",
        "--fpu",
        "--fpu-softfloat",
        "--mmu",
        "--machine",
        "--blitter",
        "--dsp",
        "--rtc-year",
        "--timer-d",
        "--fast-boot",
        # sound and non-file debug/console controls
        "--mic",
        "--sound",
        "--sound-buffer-size",
        "--sound-sync",
        "--ym-mixing",
        "--log-level",
        "--run-vbls",
    }
)
HATARI_SAFE_OPTIONS = HATARI_SAFE_FLAG_OPTIONS | HATARI_SAFE_VALUE_OPTIONS

# Shortcuts that create host files or recordings.  The scenario allowlist keeps
# the safe inspection/reset/screenshot shortcuts only.
SCENARIO_DENIED_SHORTCUTS = frozenset({"savemem", "recanim", "recsound"})
PATH_ARG_KEYS = {
    "path",
    "paths",
    "workspace",
    "autostart",
    "tos",
    "file",
    "files",
    "filename",
    "output",
    "outputs",
    "input",
    "inputs",
    "image",
    "images",
    "state",
    "source",
    "sources",
    "target",
    "targets",
    "screenshot",
    "screenshot_dir",
    "from",
    "to",
    "dest",
    "destination",
    "directory",
    "dir",
    "root",
    "program",
    "prg",
    "command",
    "commands",
    "include",
    "includes",
    "rom",
    "floppy",
    "floppies",
    "cartridge",
    "hd",
    "disk",
    "disk_image",
    "trace",
}
BENIGN_TEXT_ARG_KEYS = {"text", "keys", "expression", "regex", "contains", "label", "name", "description"}
EMU_START_ARG_KEYS = {
    "profile",
    "args",
    "tos",
    "display",
    "workspace",
    "autostart",
    "gemdos_drive",
    "gemdos_case",
    "gemdos_time",
    "gemdos_conv",
    "write_protect",
}
TEARDOWN_TOOLS = {"emu_stop", "emu_pause", "emu_quit_debugger", "log_read", "console_read"}

RESIDUAL_NONDETERMINISM = [
    "TOS/EmuTOS version and local patches",
    "--rtc-year fixes only the year; the full RTC date/time is not frozen",
    "uninitialised guest RAM",
    "host scheduling and SDL/audio timing",
    "floppy seek timing and host filesystem ordering",
    "guest programs that read host time or entropy",
    "input is host-timed unless the P7 frame-locked path is present",
]

STRICT_HATARI_ARGS = [
    "--rtc-year",
    "2020",
    "--timer-d",
    "on",
    "--fast-boot",
    "on",
    "--fastfdc",
    "off",
    "--fast-forward",
    "off",
    "--cpu-exact",
    "on",
    "--sound",
    "off",
    "--monitor",
    "rgb",
    "--statusbar",
    "off",
]
TOLERANT_HATARI_ARGS = [
    "--rtc-year",
    "2020",
    "--timer-d",
    "on",
    "--fast-boot",
    "on",
]
STRICT_ENV = {"TZ": "UTC", "LC_ALL": "C", "LANG": "C"}


class ScenarioError(HatariError):
    """A typed scenario/schema/runner error."""


class ArtifactQuotaExceeded(Exception):
    """Raised when a case or run would exceed its artifact budget."""


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _mono_ms(start: float) -> int:
    return max(0, int((time.monotonic() - start) * 1000))


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_text(text: str) -> str:
    return _sha256_bytes(text.encode("utf-8"))


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sanitize_id(value: str, *, fallback: str = "case") -> str:
    cleaned = SANITIZE_RE.sub("_", str(value)).strip("._-")
    cleaned = re.sub(r"_+", "_", cleaned)
    if not cleaned:
        cleaned = fallback
    return cleaned[:120]


def _iso_utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ScenarioError(
            "invalid_path", f"Cannot read scenario: {exc}", "Check the scenario path and permissions."
        ) from exc
    except json.JSONDecodeError as exc:
        raise ScenarioError(
            "schema_error", f"Scenario is not valid JSON: {exc}", "Fix the JSON syntax."
        ) from exc


def _load_yaml(path: Path) -> Any:
    try:
        import yaml  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise ScenarioError(
            "unsupported_format",
            "YAML scenarios require the optional PyYAML package.",
            "Install PyYAML or convert the scenario to JSON.",
        ) from exc
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ScenarioError(
            "schema_error", f"Scenario is not valid YAML: {exc}", "Fix the YAML syntax."
        ) from exc


def _require_mapping(value: Any, what: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ScenarioError("schema_error", f"{what} must be an object.", "Use a JSON object.")
    return value


def _check_unknown(value: Mapping[str, Any], allowed: set[str], what: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ScenarioError(
            "schema_error",
            f"Unknown {what} keys: {unknown}",
            f"Allowed keys are {sorted(allowed)}. Unknown keys are errors in scenario v1.",
        )


def _require_str(value: Any, what: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ScenarioError("schema_error", f"{what} must be a string.", "Use a JSON string.")
    if not allow_empty and not value.strip():
        raise ScenarioError("schema_error", f"{what} must not be empty.", "Provide a value.")
    if "\x00" in value or "\n" in value or "\r" in value:
        raise ScenarioError("schema_error", f"{what} contains a forbidden control character.", "Remove NUL/newlines.")
    return value


def _require_int(value: Any, what: str, *, minimum: int | None = None, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ScenarioError("schema_error", f"{what} must be an integer.", "Use a whole number.")
    if minimum is not None and value < minimum:
        raise ScenarioError("schema_error", f"{what} must be >= {minimum}.", "Use a larger value.")
    if maximum is not None and value > maximum:
        raise ScenarioError("schema_error", f"{what} must be <= {maximum}.", "Use a smaller value.")
    return value


def _is_scalar(value: Any) -> bool:
    return value is None or isinstance(value, (str, int, float, bool))


def _validate_scalar(value: Any, what: str) -> Any:
    if not _is_scalar(value):
        raise ScenarioError("schema_error", f"{what} must be a scalar.", "Use a string, number or boolean.")
    if isinstance(value, str) and ("\x00" in value or "\n" in value or "\r" in value):
        raise ScenarioError("schema_error", f"{what} contains a forbidden control character.", "Remove NUL/newlines.")
    return value


def _extract_vars(value: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(value, str):
        found.update(VAR_RE.findall(value))
    elif isinstance(value, dict):
        for key, item in value.items():
            found.update(_extract_vars(key))
            found.update(_extract_vars(item))
    elif isinstance(value, list):
        for item in value:
            found.update(_extract_vars(item))
    return found


def _expand(value: Any, context: Mapping[str, Any], trail: str = "scenario") -> Any:
    if isinstance(value, str):
        def replace(match: re.Match[str]) -> str:
            name = match.group(1)
            if name not in context:
                raise ScenarioError(
                    "undefined_variable",
                    f"Undefined variable ${{{name}}} in {trail}.",
                    "Declare it in requires.env, vars, env or a matrix axis.",
                )
            replacement = context[name]
            if replacement is None:
                raise ScenarioError(
                    "undefined_variable",
                    f"Variable ${{{name}}} has no value in {trail}.",
                    "Set the environment variable or matrix value.",
                )
            return str(replacement)

        return VAR_RE.sub(replace, value)
    if isinstance(value, dict):
        return {str(key): _expand(item, context, f"{trail}.{key}") for key, item in value.items()}
    if isinstance(value, list):
        return [_expand(item, context, f"{trail}[{index}]") for index, item in enumerate(value)]
    return value


@lru_cache(maxsize=1)
def _repo_root() -> Path:
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "src" / "sdl" / "main_sdl.c").is_file():
            return parent
    return Path.cwd().resolve()


def _allowed_roots(source: Path | None, environment: Mapping[str, str] | None = None) -> list[Path]:
    """Return the roots a scenario may read from.

    These are deliberately narrow: the scenario directory, the system temp
    directory and the roots declared through known read-only environment
    variables.  The whole ``~/Projects/Hatari`` tree, the repository and the
    current working directory are *not* implicitly writable.
    """
    roots: list[Path] = []
    if source is not None:
        roots.append(source.resolve().parent)
    roots.append(Path(tempfile.gettempdir()).resolve())
    env = dict(os.environ if environment is None else environment)
    for key in PATH_ENV_KEYS:
        value = env.get(key)
        if not value:
            continue
        candidate = Path(value).expanduser()
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        # For a file (e.g. HATARI_TOS) allow its directory; for a directory allow
        # the directory itself.
        roots.append(resolved.parent if resolved.is_file() else resolved)
    unique: list[Path] = []
    for root in roots:
        if root not in unique and root.is_dir():
            unique.append(root)
    return unique


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


def _protected_roots(environment: Mapping[str, str] | None = None) -> list[Path]:
    from ..corpus import default_corpus_roots

    roots = list(default_corpus_roots(environment))
    roots.append(_repo_root())
    return roots


def _reject_protected_path(path: str | Path, what: str, *, environment: Mapping[str, str] | None = None) -> Path:
    """Reject a writable path inside an external corpus tree or the repository.

    The corpus import is intentionally *not* hidden behind a blanket ``except``:
    if the policy cannot be loaded the caller must fail closed, never silently
    allow a corpus/repository write.
    """
    try:
        roots = _protected_roots(environment)
    except Exception as exc:  # pragma: no cover - defensive, but fail closed
        raise ScenarioError(
            "policy_unavailable",
            f"Cannot load corpus/repository protection policy: {exc}",
            "Repair the installation before running scenarios.",
        ) from exc
    candidate = Path(path).expanduser()
    with contextlib.suppress(OSError):
        candidate = candidate.resolve()
    for root in roots:
        if _is_within(candidate, root):
            raise ScenarioError(
                "corpus_write_risk",
                f"{what} points inside a protected corpus/repository tree: {candidate}",
                "Stage required files into a per-case workspace; never expose a corpus root or the repository as a writable drive.",
            )
    return candidate


def _validate_path_value(
    value: Any,
    *,
    roots: Sequence[Path],
    what: str,
    base: Path | None = None,
    allow_missing: bool = True,
) -> Path:
    text = _require_str(value, what)
    if "\x00" in text or "\n" in text or "\r" in text:
        raise ScenarioError("path_not_allowed", f"{what} contains a forbidden character.", "Remove NUL/newlines.")
    candidate = Path(text).expanduser()
    if not candidate.is_absolute():
        parts = candidate.parts
        if ".." in parts:
            raise ScenarioError(
                "path_not_allowed",
                f"{what} uses '..' traversal: {text!r}",
                "Use a path below the scenario directory or an allowed root.",
            )
        candidate = (base or Path.cwd()) / candidate
    try:
        resolved = candidate.resolve(strict=False)
    except OSError as exc:
        raise ScenarioError("path_not_allowed", f"Cannot resolve {what}: {exc}", "Use a simple path.") from exc
    if not any(_is_within(resolved, root) for root in roots):
        raise ScenarioError(
            "path_not_allowed",
            f"{what} escapes the configured roots: {resolved}",
            f"Allowed roots: {[str(root) for root in roots]}",
        )
    if not allow_missing and not resolved.exists():
        raise ScenarioError("invalid_path", f"{what} does not exist: {resolved}", "Check the path.")
    return resolved


def _system_temp_root() -> Path:
    return Path(tempfile.gettempdir()).resolve()


def _is_fresh_temp_workspace(path: Path) -> bool:
    """True only for a fresh/empty directory below the system temp root."""
    try:
        resolved = path.resolve(strict=False)
    except OSError:
        return False
    temp_root = _system_temp_root()
    if resolved == temp_root or not _is_within(resolved, temp_root):
        return False
    if resolved.exists():
        if resolved.is_symlink() or not resolved.is_dir():
            return False
        try:
            next(resolved.iterdir())
        except StopIteration:
            return True
        except OSError:
            return False
        return False
    # A not-yet-created path is acceptable only when its nearest existing parent
    # is already inside the temp root (and therefore cannot be a corpus/repo).
    anchor = resolved
    while not anchor.exists() and anchor != anchor.parent:
        anchor = anchor.parent
    return anchor.is_dir() and _is_within(anchor, temp_root)


def _validate_workspace_path(
    value: Any,
    *,
    what: str,
    roots: Sequence[Path],
    base: Path | None = None,
    environment: Mapping[str, str] | None = None,
) -> Path:
    """Require a scenario workspace to be a fresh temp dir, never a host tree.

    Corpus/repository paths, TOS parents and other allowed read roots are not
    valid workspaces: Hatari mounts a workspace read-write as a GEMDOS drive.
    """
    resolved = _validate_path_value(value, roots=roots, what=what, base=base, allow_missing=True)
    _reject_protected_path(resolved, what, environment=environment)
    if not _is_fresh_temp_workspace(resolved):
        raise ScenarioError(
            "path_not_allowed",
            f"{what} must be a fresh/empty directory under the system temp root: {resolved}",
            "Omit workspace and let the runner create the per-case workspace, or pass an empty directory under /tmp.",
        )
    return resolved


def _validate_hatari_arg_value(value: Any, *, roots: Sequence[Path], what: str) -> str:
    if not isinstance(value, str):
        raise ScenarioError("schema_error", f"{what} must be a string.", "Use ['--flag', 'value'].")
    if "\x00" in value or "\n" in value or "\r" in value:
        raise ScenarioError("bad_option", f"{what} contains a forbidden control character.", "Remove NUL/newlines.")
    if _looks_like_path(value):
        _validate_path_value(value, roots=roots, what=f"{what} value", allow_missing=True)
    return value


def _validate_hatari_args(
    args: Any,
    *,
    roots: Sequence[Path],
    what: str = "config.args",
) -> list[str]:
    """Validate scenario-supplied Hatari options against a strict allowlist.

    Only exact long options are accepted.  Short aliases (``-D``/``-d``/...),
    positional disk/directory arguments, attached ``=value`` forms and every
    file/stream/recorder/debugger option are rejected.  Values are checked for
    path confinement too (notably ``--tos``).
    """
    if args is None:
        return []
    if not isinstance(args, list) or not all(isinstance(item, str) for item in args):
        raise ScenarioError("schema_error", f"{what} must be a list of strings.", "Use ['--flag', 'value'].")
    cleaned: list[str] = []
    index = 0
    while index < len(args):
        token = args[index]
        if not isinstance(token, str):
            raise ScenarioError("schema_error", f"{what} must be a list of strings.", "Use ['--flag', 'value'].")
        if "\x00" in token or "\n" in token or "\r" in token:
            raise ScenarioError("bad_option", f"{what} contains a forbidden control character.", "Remove NUL/newlines.")
        if not token.startswith("--"):
            # Single-dash options (short aliases) and positional disk/directory
            # arguments are both ways to redirect host files into Hatari.
            raise ScenarioError(
                "unsafe_argument",
                f"{what} contains a short or positional argument: {token}",
                "Use only exact long options from the safe allowlist.",
            )
        if token == "--" or "=" in token:
            raise ScenarioError(
                "unsafe_argument",
                f"{what} contains an unsupported option form: {token}",
                "Pass each option and value as separate tokens; attached '=' forms are not accepted.",
            )
        option = token.lower()
        if option not in HATARI_SAFE_OPTIONS:
            raise ScenarioError(
                "unsafe_argument",
                f"{what} contains a forbidden option: {token}",
                "Only safe, non-file long options are allowed; media, host-drive, recorder, parse and debugger options are denied.",
            )
        cleaned.append(token)
        if option in HATARI_SAFE_VALUE_OPTIONS:
            index += 1
            if index >= len(args):
                raise ScenarioError("bad_option", f"{what} option {token} has no value.", "Pass the option value after it.")
            cleaned.append(_validate_hatari_arg_value(args[index], roots=roots, what=f"{what} {token}"))
        index += 1
    return cleaned


def _looks_like_path(value: str) -> bool:
    if not isinstance(value, str):
        return False
    return (
        value.startswith("/")
        or value.startswith("~/")
        or value.startswith("./")
        or value.startswith("../")
        or "\\" in value
        or ("/" in value and not value.startswith("-"))
    )


def _validate_capture(raw: Any, step_id: str) -> dict[str, Any]:
    capture = _require_mapping(raw, f"step {step_id}.capture")
    _check_unknown(capture, CAPTURE_KEYS, f"step {step_id}.capture")
    kind = capture.get("kind", "result")
    if kind not in {"result", "file", "screenshot"}:
        raise ScenarioError("schema_error", f"step {step_id}.capture.kind is invalid.", "Use result, file or screenshot.")
    fmt = str(capture.get("format", "bmp")).lower().lstrip(".")
    if kind == "screenshot" and fmt not in SCREEN_FORMATS:
        raise ScenarioError("schema_error", f"step {step_id}.capture.format is invalid.", "Use bmp or png.")
    name = capture.get("name") or step_id
    path = capture.get("path")
    if path is not None:
        path = _require_str(path, f"step {step_id}.capture.path")
        rel = Path(path)
        if rel.is_absolute() or ".." in rel.parts:
            raise ScenarioError(
                "path_not_allowed",
                f"step {step_id}.capture.path must be relative and stay inside the bundle.",
                "Use a name such as screenshots/step-04.bmp.",
            )
    out = {
        "kind": kind,
        "name": _sanitize_id(_require_str(name, f"step {step_id}.capture.name"), fallback=step_id),
        "format": fmt,
        "hash": capture.get("hash", "rgba" if kind == "screenshot" else "sha256"),
        "path": path,
        "resume": bool(capture.get("resume", True)),
    }
    if out["hash"] not in {"rgba", "sha256", "none"}:
        raise ScenarioError("schema_error", f"step {step_id}.capture.hash is invalid.", "Use rgba, sha256 or none.")
    if kind == "screenshot" and fmt == "png" and out["hash"] == "rgba":
        raise ScenarioError(
            "schema_error",
            f"step {step_id}.capture asks for rgba hashing of a PNG.",
            "Use format=bmp for decoded-RGBA hashes, or hash=sha256 for PNG bytes.",
        )
    return out


def _validate_asserts(raw: Any, step_id: str) -> list[dict[str, Any]]:
    if raw is None:
        return []
    if isinstance(raw, dict):
        items = [raw]
    elif isinstance(raw, list):
        items = raw
    else:
        raise ScenarioError("schema_error", f"step {step_id}.assert must be an object or list.", "Use an assertion object.")
    result: list[dict[str, Any]] = []
    for index, item in enumerate(items):
        assertion = _require_mapping(item, f"step {step_id}.assert[{index}]")
        _check_unknown(assertion, ASSERT_KEYS, f"step {step_id}.assert[{index}]")
        if "path" not in assertion:
            raise ScenarioError("schema_error", f"step {step_id}.assert[{index}] has no path.", "Add a dotted result path.")
        _require_str(assertion["path"], f"step {step_id}.assert[{index}].path")
        if len(set(assertion) & (ASSERT_KEYS - {"path"})) == 0:
            raise ScenarioError(
                "schema_error",
                f"step {step_id}.assert[{index}] has no comparison.",
                "Use eq, ne, contains, regex, gte, lte, exists or type.",
            )
        if "regex" in assertion:
            _require_str(assertion["regex"], f"step {step_id}.assert[{index}].regex")
            try:
                re.compile(assertion["regex"])
            except re.error as exc:
                raise ScenarioError(
                    "schema_error",
                    f"step {step_id}.assert[{index}].regex is invalid: {exc}",
                    "Fix the regular expression or use contains/eq.",
                ) from exc
        result.append(assertion)
    return result


def _validate_step(raw: Any, index: int, *, in_teardown: bool = False) -> dict[str, Any]:
    step = _require_mapping(raw, f"steps[{index}]")
    _check_unknown(step, STEP_KEYS, f"steps[{index}]")
    step_id = _require_str(step.get("id"), f"steps[{index}].id")
    if len(step_id) > 120:
        raise ScenarioError("schema_error", f"steps[{index}].id is too long.", "Use at most 120 characters.")
    tool = _require_str(step.get("tool"), f"step {step_id}.tool")
    if not TOOL_RE.match(tool) and tool not in {"wait"}:
        raise ScenarioError(
            "schema_error",
            f"step {step_id}.tool is not a valid tool name: {tool!r}",
            "Use lowercase letters, digits and underscores.",
        )
    allowed = SCENARIO_ALLOWED_TOOLS | {"wait"}
    if tool not in allowed:
        raise ScenarioError(
            "tool_not_allowed",
            f"step {step_id}.tool is not in the scenario allowlist: {tool}",
            "Scenario steps may only call the safe automation subset; raw debugger, compiler, "
            "filesystem and recursive scenario tools are denied by default.",
        )
    if in_teardown and tool not in TEARDOWN_TOOLS:
        raise ScenarioError(
            "tool_not_allowed",
            f"teardown step {step_id} may not call {tool}.",
            f"Use one of {sorted(TEARDOWN_TOOLS)} in teardown.",
        )
    args = step.get("args", {})
    if args is None:
        args = {}
    args = _require_mapping(args, f"step {step_id}.args")
    if tool == "emu_start":
        _check_unknown(args, EMU_START_ARG_KEYS, f"step {step_id}.args")
    expect = step.get("expect", {})
    if expect is None:
        expect = {}
    expect = _require_mapping(expect, f"step {step_id}.expect")
    timeout = step.get("timeout_ms")
    if timeout is None:
        timeout = STEP_TIMEOUTS.get(tool, DEFAULT_STEP_TIMEOUT_MS)
    timeout = _require_int(timeout, f"step {step_id}.timeout_ms", minimum=1, maximum=MAX_STEP_TIMEOUT_MS)
    capture = _validate_capture(step["capture"], step_id) if step.get("capture") is not None else None
    asserts = _validate_asserts(step.get("assert", step.get("asserts")), step_id)
    return {
        "id": step_id,
        "tool": tool,
        "args": args,
        "timeout_ms": timeout,
        "expect": expect,
        "capture": capture,
        "asserts": asserts,
        "description": step.get("description"),
        "in_teardown": in_teardown,
    }


def _normalize_determinism(raw: Any) -> tuple[str, dict[str, Any]]:
    if raw is None:
        raw = "off"
    if isinstance(raw, str):
        mode = raw
        extra: dict[str, Any] = {}
    elif isinstance(raw, dict):
        data = dict(raw)
        _check_unknown(data, {"mode", "fidelity", "seed", "args", "residual"}, "determinism")
        mode = data.get("mode", "off")
        extra = data
    else:
        raise ScenarioError("schema_error", "determinism must be a string or object.", "Use strict, tolerant or off.")
    if mode not in DETERMINISM_MODES:
        raise ScenarioError("schema_error", f"Invalid determinism mode: {mode}", "Use strict, tolerant or off.")
    if isinstance(raw, dict):
        if "seed" in extra:
            extra["seed"] = _require_int(extra["seed"], "determinism.seed", minimum=0, maximum=2**63 - 1)
        if "args" in extra:
            if not isinstance(extra["args"], list) or not all(isinstance(item, str) for item in extra["args"]):
                raise ScenarioError("schema_error", "determinism.args must be a list of strings.", "Use ['--flag', 'value'].")
        if "residual" in extra:
            if not isinstance(extra["residual"], list) or not all(isinstance(item, str) for item in extra["residual"]):
                raise ScenarioError("schema_error", "determinism.residual must be a list of strings.", "Describe known nondeterminism.")
    return mode, extra


def determinism_args(mode: str) -> list[str]:
    if mode == "strict":
        return list(STRICT_HATARI_ARGS)
    if mode == "tolerant":
        return list(TOLERANT_HATARI_ARGS)
    return []


def determinism_environment(mode: str) -> dict[str, str]:
    return dict(STRICT_ENV) if mode == "strict" else {}


def determinism_report(mode: str, extra: Mapping[str, Any] | None = None) -> dict[str, Any]:
    extra = dict(extra or {})
    report = {
        "mode": mode,
        "fidelity": "approximate-frame" if mode == "strict" else ("host-timed" if mode == "tolerant" else "none"),
        "vbl_locked": False,
        "native_args": determinism_args(mode),
        "declared_args": list(extra.get("args") or []),
        "residual_nondeterminism": list(RESIDUAL_NONDETERMINISM) if mode in {"strict", "tolerant"} else [],
        "note": (
            "Strict mode fixes available stock-Hatari options and hashes decoded RGBA, but it does not "
            "claim VBL-exact or byte-identical PNG determinism."
        ),
    }
    if "seed" in extra:
        report["seed"] = extra["seed"]
    if extra.get("residual"):
        report["residual_nondeterminism"] = [*report["residual_nondeterminism"], *[str(item) for item in extra["residual"]]]
    return report


@dataclass
class Scenario:
    id: str
    name: str
    version: int
    description: str = ""
    requires: dict[str, Any] = field(default_factory=dict)
    determinism: str = "off"
    determinism_extra: dict[str, Any] = field(default_factory=dict)
    timeouts: dict[str, int] = field(default_factory=dict)
    config: dict[str, Any] = field(default_factory=dict)
    matrix: dict[str, list[Any]] = field(default_factory=dict)
    matrix_meta: dict[str, Any] = field(default_factory=dict)
    overrides: list[dict[str, Any]] = field(default_factory=list)
    steps: list[dict[str, Any]] = field(default_factory=list)
    teardown: list[dict[str, Any]] = field(default_factory=list)
    attempts: int = 1
    retry_on: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    vars: dict[str, Any] = field(default_factory=dict)
    env: dict[str, str] = field(default_factory=dict)
    record: bool = False
    stage: list[dict[str, Any]] = field(default_factory=list)
    seed: int = 0
    skip_reason: str | None = None
    baseline: Any = None
    source: Path | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def scenario_timeout_ms(self) -> int:
        return int(self.timeouts["scenario_ms"])

    @property
    def default_step_timeout_ms(self) -> int:
        return int(self.timeouts["step_ms"])

    def to_manifest(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "determinism": determinism_report(self.determinism),
            "requires": self.requires,
            "timeouts": self.timeouts,
            "config": self.config,
            "matrix": {**self.matrix, **self.matrix_meta},
            "attempts": self.attempts,
            "tags": self.tags,
            "seed": self.seed,
            "source": str(self.source) if self.source else None,
        }


def parse_scenario(raw: Any, *, source: Path | None = None, environment: Mapping[str, str] | None = None) -> Scenario:
    data = _require_mapping(raw, "scenario")
    _check_unknown(data, TOP_KEYS, "scenario")
    version = data.get("version")
    if version != SCENARIO_SCHEMA_VERSION:
        raise ScenarioError(
            "schema_error",
            f"Unsupported scenario version: {version!r}",
            f"This server implements scenario v{SCENARIO_SCHEMA_VERSION}.",
        )
    scenario_id = data.get("id") or data.get("name")
    scenario_id = _require_str(scenario_id, "scenario.id")
    if not NAME_RE.match(scenario_id):
        raise ScenarioError("schema_error", f"Invalid scenario id: {scenario_id!r}", "Use letters, digits, '.', '_', ':' or '-'.")
    name = _require_str(data.get("name") or scenario_id, "scenario.name")
    description = data.get("description", "")
    if description is not None:
        description = _require_str(description, "scenario.description", allow_empty=True)

    requires = data.get("requires", {})
    requires = _require_mapping(requires, "requires")
    _check_unknown(requires, REQUIRES_KEYS, "requires")
    env_names = requires.get("env", [])
    if env_names is None:
        env_names = []
    if not isinstance(env_names, list) or not all(isinstance(item, str) and item for item in env_names):
        raise ScenarioError("schema_error", "requires.env must be a list of names.", "Use ['HATARI_TOS'].")
    files = requires.get("files", [])
    if files is None:
        files = []
    if not isinstance(files, list) or not all(isinstance(item, str) and item for item in files):
        raise ScenarioError("schema_error", "requires.files must be a list of paths.", "Use ['${ROOT}/file'].")
    tools = requires.get("tools", [])
    if tools is None:
        tools = []
    if not isinstance(tools, list) or not all(isinstance(item, str) and item for item in tools):
        raise ScenarioError("schema_error", "requires.tools must be a list of names.", "Use ['m68k-atari-mint-gcc'].")
    dirs = requires.get("dirs", [])
    if dirs is None:
        dirs = []
    if not isinstance(dirs, list) or not all(isinstance(item, str) and item for item in dirs):
        raise ScenarioError("schema_error", "requires.dirs must be a list of paths.", "Use ['${ROOT}'].")
    rom = requires.get("rom")
    if rom is not None and rom not in ROM_REQUIREMENTS:
        raise ScenarioError("schema_error", f"Invalid requires.rom: {rom!r}", "Use emutos, tos or either.")
    skip_reason = requires.get("skip_reason")
    if skip_reason is not None:
        skip_reason = _require_str(skip_reason, "requires.skip_reason", allow_empty=True)
    requires_norm = {
        "env": list(env_names),
        "files": list(files),
        "tools": list(tools),
        "dirs": list(dirs),
        "rom": rom,
        "skip_reason": skip_reason,
    }

    mode, mode_extra = _normalize_determinism(data.get("determinism"))

    timeouts = data.get("timeouts", {})
    timeouts = _require_mapping(timeouts, "timeouts")
    _check_unknown(timeouts, TIMEOUT_KEYS, "timeouts")
    scenario_ms = _require_int(
        timeouts.get("scenario_ms", DEFAULT_SCENARIO_TIMEOUT_MS),
        "timeouts.scenario_ms",
        minimum=1,
        maximum=MAX_SCENARIO_TIMEOUT_MS,
    )
    step_ms = _require_int(
        timeouts.get("step_ms", DEFAULT_STEP_TIMEOUT_MS),
        "timeouts.step_ms",
        minimum=1,
        maximum=MAX_STEP_TIMEOUT_MS,
    )
    if step_ms > scenario_ms:
        step_ms = scenario_ms

    config = data.get("config", {})
    config = _require_mapping(config, "config")
    _check_unknown(config, CONFIG_KEYS, "config")
    roots = _allowed_roots(source, environment)
    if "args" in config:
        config = {**config, "args": _validate_hatari_args(config["args"], roots=roots)}
    if mode_extra.get("args"):
        mode_extra["args"] = _validate_hatari_args(mode_extra["args"], roots=roots, what="determinism.args")
    for key in ("tos", "workspace", "autostart"):
        if key in config and config[key] is not None:
            _require_str(config[key], f"config.{key}")

    matrix = data.get("matrix", {})
    matrix = _require_mapping(matrix, "matrix")
    axes: dict[str, list[Any]] = {}
    for axis, values in matrix.items():
        if axis in MATRIX_META_KEYS:
            continue
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", axis):
            raise ScenarioError("schema_error", f"Invalid matrix axis: {axis!r}", "Use a variable-style name.")
        if not isinstance(values, list) or not values:
            raise ScenarioError("schema_error", f"matrix.{axis} must be a non-empty list.", "Add at least one value.")
        cleaned = [_validate_scalar(item, f"matrix.{axis}") for item in values]
        seen: set[str] = set()
        for item in cleaned:
            marker = _canonical(item)
            if marker in seen:
                raise ScenarioError(
                    "duplicate_matrix_value",
                    f"matrix.{axis} contains a duplicate value: {item!r}",
                    "Remove duplicate axis values so case ids stay unique.",
                )
            seen.add(marker)
        axes[axis] = cleaned
    matrix_meta: dict[str, Any] = {}
    max_cases = matrix.get("max_cases")
    if max_cases is not None:
        matrix_meta["max_cases"] = _require_int(max_cases, "matrix.max_cases", minimum=1)
    if matrix.get("fail_fast") is not None:
        if not isinstance(matrix["fail_fast"], bool):
            raise ScenarioError("schema_error", "matrix.fail_fast must be a boolean.", "Use true or false.")
        matrix_meta["fail_fast"] = matrix["fail_fast"]

    overrides_raw = data.get("overrides", [])
    if overrides_raw is None:
        overrides_raw = []
    if not isinstance(overrides_raw, list):
        raise ScenarioError("schema_error", "overrides must be a list.", "Use a list of override objects.")
    overrides: list[dict[str, Any]] = []
    for index, raw_override in enumerate(overrides_raw):
        override = _require_mapping(raw_override, f"overrides[{index}]")
        _check_unknown(override, OVERRIDE_KEYS, f"overrides[{index}]")
        match = override.get("match", {})
        match = _require_mapping(match, f"overrides[{index}].match")
        for axis in match:
            if axis not in axes:
                raise ScenarioError(
                    "schema_error",
                    f"overrides[{index}].match refers to unknown axis {axis!r}.",
                    "Declare the axis in matrix.",
                )
        if "timeouts" in override:
            _require_mapping(override["timeouts"], f"overrides[{index}].timeouts")
        if "config" in override:
            cfg = _require_mapping(override["config"], f"overrides[{index}].config")
            _check_unknown(cfg, CONFIG_KEYS, f"overrides[{index}].config")
            if "args" in cfg:
                cfg = {**cfg, "args": _validate_hatari_args(cfg["args"], roots=roots)}
            override["config"] = cfg
        if "determinism" in override:
            _normalize_determinism(override["determinism"])
        if "env" in override and not isinstance(override["env"], dict):
            raise ScenarioError("schema_error", f"overrides[{index}].env must be an object.", "Use a mapping.")
        if "vars" in override and not isinstance(override["vars"], dict):
            raise ScenarioError("schema_error", f"overrides[{index}].vars must be an object.", "Use a mapping.")
        overrides.append(override)

    steps_raw = data.get("steps")
    if not isinstance(steps_raw, list) or not steps_raw:
        raise ScenarioError("schema_error", "steps must be a non-empty list.", "Add at least one tool step.")
    steps: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for index, raw_step in enumerate(steps_raw):
        step = _validate_step(raw_step, index)
        if step["id"] in seen_ids:
            raise ScenarioError("duplicate_step_id", f"Duplicate step id: {step['id']}", "Step ids must be unique.")
        seen_ids.add(step["id"])
        steps.append(step)

    teardown_raw = data.get("teardown", [])
    if teardown_raw is None:
        teardown_raw = []
    if not isinstance(teardown_raw, list):
        raise ScenarioError("schema_error", "teardown must be a list.", "Use a list of tool steps.")
    teardown: list[dict[str, Any]] = []
    for index, raw_step in enumerate(teardown_raw):
        step = _validate_step(raw_step, index, in_teardown=True)
        if step["id"] in seen_ids:
            raise ScenarioError("duplicate_step_id", f"Duplicate teardown id: {step['id']}", "Step ids must be unique.")
        seen_ids.add(step["id"])
        teardown.append(step)
    if not teardown:
        teardown = [
            {
                "id": "teardown-stop",
                "tool": "emu_stop",
                "args": {"force": False},
                "timeout_ms": STEP_TIMEOUTS["teardown"],
                "expect": {},
                "capture": None,
                "asserts": [],
                "description": "Always stop Hatari",
                "in_teardown": True,
            }
        ]

    attempts = _require_int(data.get("attempts", 1), "attempts", minimum=1, maximum=10)
    retry_on = data.get("retry_on", [])
    if retry_on is None:
        retry_on = []
    if not isinstance(retry_on, list) or not all(isinstance(item, str) for item in retry_on):
        raise ScenarioError("schema_error", "retry_on must be a list of error codes.", "Use ['step_timeout'].")
    tags = data.get("tags", [])
    if tags is None:
        tags = []
    if not isinstance(tags, list) or not all(isinstance(item, str) for item in tags):
        raise ScenarioError("schema_error", "tags must be a list of strings.", "Use ['smoke'].")
    vars_raw = data.get("vars", {})
    vars_map = _require_mapping(vars_raw, "vars")
    for key, value in vars_map.items():
        _validate_scalar(value, f"vars.{key}")
    env_raw = data.get("env", {})
    env_map = _require_mapping(env_raw, "env")
    for key, value in env_map.items():
        if not isinstance(value, str):
            raise ScenarioError("schema_error", f"env.{key} must be a string.", "Use a string value.")
    record = data.get("record", False)
    if not isinstance(record, bool):
        raise ScenarioError("schema_error", "record must be a boolean.", "Use true or false.")
    seed_value = data.get("seed", mode_extra.get("seed", 0))
    seed = _require_int(seed_value, "seed", minimum=0, maximum=2**63 - 1)

    stage_raw = data.get("stage", [])
    if stage_raw is None:
        stage_raw = []
    if not isinstance(stage_raw, list):
        raise ScenarioError("schema_error", "stage must be a list.", "Use a list of stage entries.")
    stage: list[dict[str, Any]] = []
    for index, raw_stage in enumerate(stage_raw):
        entry = _require_mapping(raw_stage, f"stage[{index}]")
        _check_unknown(entry, STAGE_KEYS, f"stage[{index}]")
        source_value = _require_str(entry.get("from"), f"stage[{index}].from")
        to_value = _require_str(entry.get("to", Path(source_value).name or f"item-{index}"), f"stage[{index}].to")
        to_path = Path(to_value)
        if to_path.is_absolute() or ".." in to_path.parts:
            raise ScenarioError("path_not_allowed", f"stage[{index}].to must be a safe relative path.", "Use a relative path below the workspace.")
        stage.append({"from": source_value, "to": to_value, "optional": bool(entry.get("optional", False))})

    scenario_skip = data.get("skip_reason")
    if scenario_skip is not None:
        scenario_skip = _require_str(scenario_skip, "skip_reason", allow_empty=True)

    known_vars = (
        set(requires_norm["env"])
        | set(vars_map)
        | set(env_map)
        | set(axes)
    )
    used_vars = _extract_vars({"requires": requires_norm, "steps": steps, "teardown": teardown, "config": config, "stage": stage, "overrides": overrides})
    undefined = sorted(used_vars - known_vars)
    if undefined:
        raise ScenarioError(
            "undefined_variable",
            f"Scenario uses undeclared variables: {undefined}",
            "Declare them in requires.env, vars, env or matrix.",
        )

    scenario = Scenario(
        id=scenario_id,
        name=name,
        version=SCENARIO_SCHEMA_VERSION,
        description=description or "",
        requires=requires_norm,
        determinism=mode,
        determinism_extra=mode_extra,
        timeouts={"scenario_ms": scenario_ms, "step_ms": step_ms},
        config=config,
        matrix=axes,
        matrix_meta=matrix_meta,
        overrides=overrides,
        steps=steps,
        teardown=teardown,
        attempts=attempts,
        retry_on=list(retry_on),
        tags=list(tags),
        vars=dict(vars_map),
        env=dict(env_map),
        record=record,
        stage=stage,
        seed=seed,
        skip_reason=scenario_skip or requires_norm.get("skip_reason"),
        baseline=data.get("baseline"),
        source=source,
        raw=data,
    )
    return scenario


def load_scenario(path: str | Path, *, environment: Mapping[str, str] | None = None) -> Scenario:
    target = Path(path).expanduser()
    if not target.is_file():
        raise ScenarioError("invalid_path", f"Scenario file does not exist: {target}", "Check the path.")
    if target.suffix.lower() in {".yaml", ".yml"}:
        raw = _load_yaml(target)
    else:
        raw = _read_json(target)
    return parse_scenario(raw, source=target.resolve(), environment=environment)


@dataclass
class MatrixCase:
    id: str
    axes: dict[str, Any]
    override: dict[str, Any]
    scenario: Scenario

    @property
    def case_id(self) -> str:
        return self.id


def _sort_key(value: Any) -> tuple[str, str]:
    return (type(value).__name__, json.dumps(value, sort_keys=True, default=str))


def _case_id(scenario_id: str, axes: Mapping[str, Any]) -> str:
    if not axes:
        return _sanitize_id(scenario_id, fallback="case")
    suffix = ",".join(f"{key}={_sanitize_id(str(value), fallback='value')}" for key, value in sorted(axes.items()))
    return _sanitize_id(f"{scenario_id}__{suffix}", fallback="case")


def _match_override(override: Mapping[str, Any], axes: Mapping[str, Any]) -> bool:
    match = override.get("match") or {}
    return all(axes.get(key) == value for key, value in match.items())


def expand_matrix(
    scenario: Scenario,
    *,
    max_cases: int | None = None,
) -> list[MatrixCase]:
    axes_names = sorted(scenario.matrix)
    value_lists = [sorted(scenario.matrix[name], key=_sort_key) for name in axes_names]

    # Compute the cardinality before materialising any combination.  A 300x300
    # matrix must be rejected, not expanded into 90,000 dicts first.
    total = math.prod(len(values) for values in value_lists) if value_lists else 1
    declared_cap = scenario.matrix_meta.get("max_cases")
    caps = [cap for cap in (max_cases, declared_cap, DEFAULT_MAX_CASES) if cap is not None]
    cap = min(caps) if caps else DEFAULT_MAX_CASES
    if total > cap:
        raise ScenarioError(
            "matrix_too_large",
            f"Matrix expands to {total} cases but max_cases is {cap}.",
            "Reduce the matrix or raise --max-cases (hard default cap is "
            f"{DEFAULT_MAX_CASES}).",
        )

    combinations: list[dict[str, Any]] = [{}]
    for name, values in zip(axes_names, value_lists):
        next_combinations: list[dict[str, Any]] = []
        for base in combinations:
            for value in values:
                next_combinations.append({**base, name: value})
        combinations = next_combinations

    cases: list[MatrixCase] = []
    seen_ids: set[str] = set()
    for axes in combinations:
        case_id = _case_id(scenario.id, axes)
        if case_id in seen_ids:
            raise ScenarioError(
                "duplicate_case_id",
                f"Matrix values collide after sanitising case ids: {case_id}",
                "Use matrix values that differ by more than punctuation.",
            )
        seen_ids.add(case_id)
        override: dict[str, Any] = {}
        for candidate in scenario.overrides:
            if _match_override(candidate, axes):
                override = {**override, **candidate}
        cases.append(MatrixCase(id=case_id, axes=axes, override=override, scenario=scenario))
    return cases


@dataclass
class ResolvedCase:
    scenario_id: str
    case_id: str
    axes: dict[str, Any]
    steps: list[dict[str, Any]]
    teardown: list[dict[str, Any]]
    config: dict[str, Any]
    determinism: str
    timeouts: dict[str, int]
    requires: dict[str, Any]
    stage: list[dict[str, Any]]
    vars: dict[str, Any]
    env: dict[str, str]
    skip_reason: str | None
    resolved: dict[str, Any]
    resolved_hash: str
    secret_values: tuple[str, ...] = ()

    @property
    def scenario_ms(self) -> int:
        return int(self.timeouts["scenario_ms"])


def _merge_overrides(scenario: Scenario, override: Mapping[str, Any]) -> dict[str, Any]:
    config = dict(scenario.config)
    timeouts = dict(scenario.timeouts)
    determinism = scenario.determinism
    vars_map = dict(scenario.vars)
    env_map = dict(scenario.env)
    skip_reason = scenario.skip_reason
    if "config" in override:
        config.update(override["config"])
    if "timeouts" in override:
        allowed = {key: int(value) for key, value in override["timeouts"].items() if key in TIMEOUT_KEYS}
        timeouts.update(allowed)
    if "determinism" in override:
        determinism, _extra = _normalize_determinism(override["determinism"])
    if "vars" in override:
        vars_map.update(override["vars"])
    if "env" in override:
        env_map.update({str(k): str(v) for k, v in override["env"].items()})
    if override.get("skip_reason"):
        skip_reason = str(override["skip_reason"])
    return {
        "config": config,
        "timeouts": timeouts,
        "determinism": determinism,
        "vars": vars_map,
        "env": env_map,
        "skip_reason": skip_reason,
    }


def resolve_case(
    case: MatrixCase,
    *,
    environment: Mapping[str, str] | None = None,
    check_paths: bool = True,
) -> ResolvedCase:
    scenario = case.scenario
    env = dict(os.environ if environment is None else environment)
    merged = _merge_overrides(scenario, case.override)
    context: dict[str, Any] = {}
    context.update({name: env.get(name) for name in scenario.requires.get("env", [])})
    context.update(scenario.vars)
    context.update(merged["vars"])
    context.update(scenario.env)
    context.update(merged["env"])
    context.update(case.axes)
    missing_env = [name for name, value in context.items() if value is None and name in scenario.requires.get("env", [])]
    # Missing required variables are reported as a skip by the runner, but the
    # resolved snapshot still needs a placeholder so validation can complete.
    for name in missing_env:
        context[name] = f"<missing:{name}>"

    roots = _allowed_roots(scenario.source, env)
    base = scenario.source.parent if scenario.source else Path.cwd()
    resolved_steps = _expand(scenario.steps, context, "steps")
    resolved_teardown = _expand(scenario.teardown, context, "teardown")
    resolved_config = _expand(merged["config"], context, "config")
    resolved_stage = _expand(scenario.stage, context, "stage")
    resolved_requires = _expand(scenario.requires, context, "requires")

    if check_paths:
        for step in resolved_steps + resolved_teardown:
            _validate_step_paths(step, roots=roots, base=base, stage=resolved_stage, environment=env)
        for key in ("tos", "workspace", "autostart"):
            value = resolved_config.get(key)
            if value and not _has_missing_placeholder(value):
                if key == "workspace":
                    resolved_config[key] = str(
                        _validate_workspace_path(
                            value,
                            what="config.workspace",
                            roots=roots,
                            base=base,
                            environment=env,
                        )
                    )
                    continue
                resolved_path = _validate_path_value(value, roots=roots, what=f"config.{key}", base=base, allow_missing=True)
                if key == "tos":
                    resolved_config[key] = str(resolved_path)
        if resolved_config.get("args"):
            _validate_hatari_args(resolved_config["args"], roots=roots)
        for entry in resolved_stage:
            if not _has_missing_placeholder(entry["from"]):
                _validate_path_value(entry["from"], roots=roots, what="stage.from", base=base, allow_missing=True)
        for path_value in resolved_requires.get("files", []):
            if not _has_missing_placeholder(path_value):
                _validate_path_value(path_value, roots=roots, what="requires.files", base=base, allow_missing=True)
        for path_value in resolved_requires.get("dirs", []):
            if not _has_missing_placeholder(path_value):
                _validate_path_value(path_value, roots=roots, what="requires.dirs", base=base, allow_missing=True)

    secret_values = _secret_values(scenario, merged, env)
    resolved = {
        "version": scenario.version,
        "id": scenario.id,
        "name": scenario.name,
        "case": case.id,
        "axes": case.axes,
        "config": resolved_config,
        "determinism": merged["determinism"],
        "determinism_args": list(scenario.determinism_extra.get("args") or []),
        "determinism_residual": list(scenario.determinism_extra.get("residual") or []),
        "timeouts": merged["timeouts"],
        "requires": resolved_requires,
        "vars": {**scenario.vars, **merged["vars"]},
        "env": {**scenario.env, **merged["env"]},
        "steps": resolved_steps,
        "teardown": resolved_teardown,
        "stage": resolved_stage,
        "record": scenario.record,
        "seed": scenario.seed,
        "skip_reason": merged["skip_reason"],
        "redactions": sorted(secret_values),
    }
    snapshot = {key: value for key, value in resolved.items() if key != "case"}
    resolved_hash = _sha256_text(_canonical(snapshot))
    public_resolved = _redact_secrets(resolved, secret_values)
    return ResolvedCase(
        scenario_id=scenario.id,
        case_id=case.id,
        axes=dict(case.axes),
        steps=resolved_steps,
        teardown=resolved_teardown,
        config=resolved_config,
        determinism=merged["determinism"],
        timeouts={key: int(value) for key, value in merged["timeouts"].items()},
        requires=resolved_requires,
        stage=resolved_stage,
        vars={**scenario.vars, **merged["vars"]},
        env={**scenario.env, **merged["env"]},
        skip_reason=merged["skip_reason"],
        resolved=public_resolved,
        resolved_hash=resolved_hash,
        secret_values=tuple(sorted(secret_values)),
    )


def _has_missing_placeholder(value: Any) -> bool:
    return isinstance(value, str) and "<missing:" in value


def _secret_values(scenario: Scenario, merged: Mapping[str, Any], env: Mapping[str, str]) -> set[str]:
    """Return every declared env/var value, not just secret-looking names.

    A declared variable is policy-controlled data.  Redacting all of it means a
    value named ``AWS_ACCESS_KEY_ID`` (or any other name outside the small
    TOKEN/SECRET/PASSWORD/API_KEY heuristic) can never leak through artifacts,
    replay events, stage metadata or logs.  Short values are skipped only to
    avoid corrupting unrelated text.
    """
    values: set[str] = set()
    for name in scenario.requires.get("env", []):
        if env.get(name) not in (None, ""):
            values.add(str(env[name]))
    for _key, value in {**scenario.env, **merged.get("env", {})}.items():
        if value not in (None, ""):
            values.add(str(value))
    for _key, value in {**scenario.vars, **merged.get("vars", {})}.items():
        if value not in (None, ""):
            values.add(str(value))
    return {value for value in values if value and len(value) >= 4}


def _secret_values_for_case(
    scenario: Scenario,
    case: MatrixCase,
    environment: Mapping[str, str] | None = None,
) -> set[str]:
    """Collect declared secrets without resolving paths or expanding variables.

    ``resolve_case`` itself can fail (for example when a declared secret is
    interpolated into ``config.tos`` and escapes the allowed roots).  The run
    must still redact that secret from its failure messages and run-level CI
    files, so the runner collects it before calling ``resolve_case``.
    """
    env = dict(os.environ if environment is None else environment)
    merged = _merge_overrides(scenario, case.override)
    return _secret_values(scenario, merged, env)


def _redact_secrets(value: Any, secret_values: Iterable[str]) -> Any:
    secrets = [item for item in secret_values if item]
    if isinstance(value, str):
        for secret in secrets:
            value = value.replace(secret, "<redacted>")
        return value
    if isinstance(value, dict):
        return {str(key): _redact_secrets(item, secrets) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_secrets(item, secrets) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_secrets(item, secrets) for item in value)
    return value


def _pathlike_arg_values(args: Any, key: str = "") -> Iterable[tuple[str, str]]:
    if isinstance(args, dict):
        for child_key, child_value in args.items():
            yield from _pathlike_arg_values(child_value, str(child_key))
    elif isinstance(args, list):
        for item in args:
            yield from _pathlike_arg_values(item, key)
    elif isinstance(args, str):
        if key in BENIGN_TEXT_ARG_KEYS:
            return
        if key in PATH_ARG_KEYS or _looks_like_path(args):
            yield key, args


def _validate_step_paths(
    step: Mapping[str, Any],
    *,
    roots: Sequence[Path],
    base: Path,
    stage: Sequence[Mapping[str, Any]],
    environment: Mapping[str, str] | None = None,
) -> None:
    tool = step.get("tool")
    args = step.get("args") or {}
    for key, item in _pathlike_arg_values(args):
        if not item.strip() or _has_missing_placeholder(item):
            continue
        _validate_path_value(item, roots=roots, what=f"step {step.get('id')}.args.{key}", base=base)
    if tool == "emu_start":
        extra = args.get("args")
        if extra is not None:
            _validate_hatari_args(extra, roots=roots, what=f"step {step.get('id')}.args.args")
        workspace = args.get("workspace")
        if workspace and not _has_missing_placeholder(workspace):
            _validate_workspace_path(
                workspace,
                what=f"step {step.get('id')}.args.workspace",
                roots=roots,
                base=base,
                environment=environment,
            )
    if tool == "emu_shortcut":
        name = str(args.get("name") or "").strip().lower()
        if name in SCENARIO_DENIED_SHORTCUTS:
            raise ScenarioError(
                "unsafe_argument",
                f"step {step.get('id')} invokes the host-writing shortcut {name!r}.",
                "Use screenshot, reset or inspection shortcuts; savemem/recanim/recsound are denied.",
            )
    if tool in {"media_insert_floppy", "media_insert_cartridge"}:
        # Hatari opens inserted media read-write by default.  Even a fresh temp
        # copy is safe, but a corpus/repository image would be mutated in place.
        for key, item in _pathlike_arg_values(args):
            if not item.strip() or _has_missing_placeholder(item):
                continue
            _reject_protected_path(
                item,
                f"step {step.get('id')}.args.{key}",
                environment=environment,
            )
    if tool == "run_prg":
        # run_prg mounts its workspace read-write.  The scenario must not turn
        # that into a way around the fresh-temp/staged-workspace rule.
        workspace = args.get("workspace")
        if workspace and not _has_missing_placeholder(workspace):
            _validate_workspace_path(
                workspace,
                what=f"step {step.get('id')}.args.workspace",
                roots=roots,
                base=base,
                environment=environment,
            )
    # capture.path is already constrained to a relative bundle path by
    # _validate_capture; it cannot point into a corpus/repository tree.


def validate_scenario(path: str | Path, *, max_cases: int | None = None, environment: Mapping[str, str] | None = None) -> dict[str, Any]:
    scenario = load_scenario(path, environment=environment)
    cases = expand_matrix(scenario, max_cases=max_cases)
    resolved_cases: list[dict[str, Any]] = []
    for case in cases:
        resolved = resolve_case(case, environment=environment, check_paths=True)
        resolved_cases.append(
            {
                "case": resolved.case_id,
                "axes": resolved.axes,
                "hash": resolved.resolved_hash,
            }
        )
    return {
        "ok": True,
        "scenario": scenario.id,
        "name": scenario.name,
        "version": scenario.version,
        "cases": len(cases),
        "resolved_cases": resolved_cases,
        "determinism": determinism_report(scenario.determinism),
        "warnings": _validation_warnings(scenario),
    }


def _validation_warnings(scenario: Scenario) -> list[str]:
    warnings: list[str] = []
    if scenario.determinism == "strict":
        if scenario.requires.get("rom") not in {"emutos"}:
            warnings.append("strict mode with a non-EmuTOS ROM cannot promise repeatability")
        if any(step["tool"] in INPUT_TOOLS for step in scenario.steps):
            warnings.append("strict mode records host-timed input fidelity as approximate-frame")
    if scenario.requires.get("env") and "skip_reason" not in scenario.requires:
        warnings.append("requires.env has no requires.skip_reason; CI will report a generic skip reason")
    return warnings


# ---------------------------------------------------------------------------
# Artifacts
# ---------------------------------------------------------------------------


class RunBudget:
    """Shared byte budget across every case in one run."""

    def __init__(self, max_bytes: int = DEFAULT_MAX_RUN_BYTES) -> None:
        self.max_bytes = max_bytes
        self.used = 0
        self.exceeded = False
        self._lock = threading.Lock()

    def reserve(self, extra: int, *, enforce: bool = True) -> None:
        with self._lock:
            if self.used + extra > self.max_bytes:
                self.exceeded = True
                if enforce:
                    raise ArtifactQuotaExceeded(
                        f"run artifact budget would exceed {self.max_bytes} bytes "
                        f"({self.used} used, {extra} more)"
                    )
            self.used += extra


class ArtifactBundle:
    """A per-case attempt directory, finalized atomically."""

    def __init__(
        self,
        run_root: Path,
        final: Path,
        staging: Path,
        *,
        run_id: str,
        case_id: str,
        attempt: int,
        max_bytes: int = DEFAULT_MAX_BUNDLE_BYTES,
        run_budget: RunBudget | None = None,
        allowed_sources: Sequence[Path] | None = None,
        redact_values: Sequence[str] | None = None,
    ) -> None:
        self.run_root = run_root
        self.final = final
        self.staging = staging
        self.run_id = run_id
        self.case_id = case_id
        self.attempt = attempt
        self.max_bytes = max_bytes
        self.run_budget = run_budget
        self.allowed_sources = [Path(root).resolve() for root in (allowed_sources or [])]
        self.redact_values = tuple(value for value in (redact_values or ()) if value)
        self.artifacts: dict[str, int] = {}
        self.bytes_written = 0
        self.quota_exceeded = False

    @classmethod
    def create(
        cls,
        artifacts_root: str | Path,
        run_id: str,
        case_id: str,
        attempt: int = 1,
        *,
        max_bytes: int = DEFAULT_MAX_BUNDLE_BYTES,
        run_budget: RunBudget | None = None,
        allowed_sources: Sequence[Path] | None = None,
        redact_values: Sequence[str] | None = None,
    ) -> "ArtifactBundle":
        raw_root = Path(artifacts_root).expanduser()
        if raw_root.is_symlink():
            raise ScenarioError(
                "path_not_allowed",
                f"Artifact root must not be a symlink: {raw_root}",
                "Use a real directory under a private temp/run root.",
            )
        run_root = raw_root.resolve()
        _reject_protected_path(run_root, "artifacts root")
        run_root.mkdir(parents=True, exist_ok=True)
        if run_root.is_symlink() or not run_root.is_dir():
            raise ScenarioError("path_not_allowed", f"Artifact root is not a real directory: {run_root}", "Use a real directory.")
        case_dir = run_root / _sanitize_id(case_id, fallback="case")
        if case_dir.is_symlink():
            raise ScenarioError(
                "path_not_allowed",
                f"Case artifact directory is a symlink: {case_dir}",
                "Remove the symlink; bundles are written only below the run root.",
            )
        case_dir.mkdir(parents=True, exist_ok=True)
        if case_dir.is_symlink() or not _is_within(case_dir, run_root):
            raise ScenarioError("path_not_allowed", f"Case directory escapes the artifact root: {case_dir}", "Use a safe case id.")
        os.chmod(case_dir, 0o700)
        final = case_dir / f"attempt-{attempt}"
        if final.is_symlink():
            raise ScenarioError(
                "path_not_allowed",
                f"Artifact attempt path is a symlink: {final}",
                "Remove the symlink before running.",
            )
        if final.exists():
            suffix = 1
            while (case_dir / f"attempt-{attempt}-{suffix}").exists() or (case_dir / f"attempt-{attempt}-{suffix}").is_symlink():
                suffix += 1
            final = case_dir / f"attempt-{attempt}-{suffix}"
        staging = case_dir / f".attempt-{attempt}-{uuid.uuid4().hex}.partial"
        staging.mkdir(mode=0o700, parents=False, exist_ok=False)
        if staging.is_symlink():
            raise ScenarioError("path_not_allowed", f"Staging directory is a symlink: {staging}", "Use a clean artifact root.")
        os.chmod(staging, 0o700)
        return cls(
            run_root,
            final,
            staging,
            run_id=run_id,
            case_id=case_id,
            attempt=attempt,
            max_bytes=max_bytes,
            run_budget=run_budget,
            allowed_sources=allowed_sources,
            redact_values=redact_values,
        )

    @property
    def root(self) -> Path:
        return self.staging

    def _check_quota(self, extra: int, *, enforce: bool = True) -> None:
        if enforce and self.quota_exceeded:
            raise ArtifactQuotaExceeded(
                f"artifact bundle already exceeded {self.max_bytes} bytes ({self.bytes_written} written)"
            )
        if not self.quota_exceeded and self.bytes_written + extra > self.max_bytes:
            self.quota_exceeded = True
            if enforce:
                raise ArtifactQuotaExceeded(
                    f"artifact bundle would exceed {self.max_bytes} bytes "
                    f"({self.bytes_written} written, {extra} more)"
                )
        if self.run_budget is not None:
            self.run_budget.reserve(extra, enforce=enforce)

    def _target(self, relpath: str) -> Path:
        rel = Path(relpath)
        if rel.is_absolute() or ".." in rel.parts or not rel.parts:
            raise ScenarioError("path_not_allowed", f"Invalid artifact path: {relpath}", "Use a relative path.")
        target = self.staging / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(target.parent, 0o700)
        return target

    def _redact_relpath(self, relpath: str) -> str:
        """Redact declared secrets from an artifact path component.

        Artifact names are guest-visible data (a capture path may expand a
        declared variable), so the physical name and every manifest/checksum
        entry must use the redacted form.  Reserved bundle metadata names are
        never rewritten.
        """
        if not self.redact_values:
            return relpath
        if relpath in {"manifest.json", "checksums.sha256", ".complete"}:
            return relpath
        return _redact_secrets(relpath, self.redact_values)

    def _write_raw(self, relpath: str, data: bytes) -> dict[str, Any]:
        relpath = self._redact_relpath(relpath)
        target = self._target(relpath)
        target.write_bytes(data)
        os.chmod(target, 0o600)
        self.bytes_written += len(data)
        self.artifacts[relpath] = len(data)
        return {"path": relpath, "size": len(data), "sha256": _sha256_bytes(data)}

    def _write_meta(self, relpath: str, data: bytes) -> dict[str, Any]:
        try:
            self._check_quota(len(data))
        except ArtifactQuotaExceeded:
            # Metadata must still be written so the bundle is complete and
            # diagnosable; quota_exceeded is already latched and the finalizer
            # turns a passed case into an error.
            pass
        return self._write_raw(relpath, data)

    def _redact_data(self, relpath: str, data: bytes) -> bytes:
        """Redact declared env/var values from UTF-8 text artifacts."""
        if not self.redact_values or not data:
            return data
        if Path(relpath).suffix.lower() not in {
            ".json",
            ".jsonl",
            ".log",
            ".txt",
            ".xml",
            ".md",
            ".yaml",
            ".yml",
        }:
            return data
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            return data
        return _redact_secrets(text, self.redact_values).encode("utf-8")

    def write_bytes(self, relpath: str, data: bytes) -> dict[str, Any]:
        data = self._redact_data(relpath, data)
        self._check_quota(len(data))
        return self._write_raw(relpath, data)

    def write_text(self, relpath: str, text: str) -> dict[str, Any]:
        return self.write_bytes(relpath, text.encode("utf-8"))

    def write_json(self, relpath: str, value: Any) -> dict[str, Any]:
        return self.write_text(relpath, json.dumps(value, indent=2, sort_keys=True) + "\n")

    def copy_file(self, relpath: str, source: Path) -> dict[str, Any]:
        candidate = Path(source).expanduser()
        if candidate.is_symlink():
            raise ScenarioError("path_not_allowed", f"Artifact source is a symlink: {candidate}", "Copy a regular file.")
        try:
            resolved = candidate.resolve()
        except OSError as exc:
            raise ScenarioError("invalid_path", f"Cannot resolve artifact source: {exc}", "Check the path.") from exc
        if self.allowed_sources and not any(_is_within(resolved, root) for root in self.allowed_sources):
            raise ScenarioError(
                "path_not_allowed",
                f"Artifact source is outside the case workspace: {resolved}",
                "Only files produced in the per-case workspace may be copied into a bundle.",
            )
        _reject_protected_path(resolved, "artifact source")
        try:
            size = resolved.stat().st_size
        except OSError as exc:
            raise ScenarioError("invalid_path", f"Cannot stat artifact source: {exc}", "Check the path.") from exc
        self._check_quota(size)
        data = self._redact_data(relpath, resolved.read_bytes())
        if len(data) > size:
            self._check_quota(len(data) - size)
        return self._write_raw(relpath, data)

    def finalize(self, status: str, manifest: dict[str, Any]) -> Path:
        artifact_list = []
        for target in sorted(self.staging.rglob("*")):
            if not target.is_file():
                continue
            rel = target.relative_to(self.staging).as_posix()
            if rel in {"manifest.json", "checksums.sha256", ".complete"}:
                continue
            if self._redact_relpath(rel) != rel:
                raise ScenarioError(
                    "path_not_allowed",
                    f"Artifact name contains a declared secret: {rel}",
                    "Rename the artifact before finalizing; secret-bearing paths are never published.",
                )
            artifact_list.append({"path": rel, "size": target.stat().st_size, "sha256": _file_sha256(target)})

        payload = {
            "schema": MANIFEST_SCHEMA,
            "run_id": self.run_id,
            "case": self.case_id,
            "attempt": self.attempt,
            "status": status,
            "finalized_utc": _iso_utc_now(),
            **manifest,
            "artifacts": artifact_list,
        }
        manifest_bytes = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
        marker_bytes = (json.dumps({"status": status, "finalized_utc": payload["finalized_utc"]}) + "\n").encode("utf-8")

        def _checksum_bytes(manifest_data: bytes, marker_data: bytes) -> bytes:
            # Every regular bundle file except checksums.sha256 itself is
            # covered, including manifest.json and the .complete marker.
            lines = [
                f"{_sha256_bytes(manifest_data)}  manifest.json",
                f"{_sha256_bytes(marker_data)}  .complete",
            ]
            for target in sorted(self.staging.rglob("*")):
                if not target.is_file():
                    continue
                rel = target.relative_to(self.staging).as_posix()
                if rel in {"manifest.json", "checksums.sha256", ".complete"}:
                    continue
                lines.append(f"{_file_sha256(target)}  {rel}")
            return ("\n".join(lines) + "\n").encode("utf-8")

        checksum_bytes = _checksum_bytes(manifest_bytes, marker_bytes)
        meta_size = len(manifest_bytes) + len(checksum_bytes) + len(marker_bytes)
        try:
            self._check_quota(meta_size)
        except ArtifactQuotaExceeded as exc:
            if status in {"passed", "skipped"}:
                status = "error"
            payload["status"] = status
            if not payload.get("failure"):
                payload["failure"] = {"error": "artifact_quota_exceeded", "message": str(exc)}
            else:
                payload.setdefault("secondary_errors", [])
                payload["secondary_errors"].append({"error": "artifact_quota_exceeded", "message": str(exc)})
            manifest_bytes = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
            marker_bytes = (json.dumps({"status": status, "finalized_utc": payload["finalized_utc"]}) + "\n").encode("utf-8")
            checksum_bytes = _checksum_bytes(manifest_bytes, marker_bytes)

        self._write_raw("manifest.json", manifest_bytes)
        self._write_raw("checksums.sha256", checksum_bytes)
        self._write_raw(".complete", marker_bytes)
        try:
            os.replace(self.staging, self.final)
        except OSError:
            # A concurrent attempt may have taken the name; pick a unique one.
            suffix = 1
            while True:
                candidate = self.final.with_name(f"{self.final.name}-{suffix}")
                try:
                    os.replace(self.staging, candidate)
                    self.final = candidate
                    break
                except OSError:
                    suffix += 1
                    if suffix > 100:
                        raise
        return self.final


# ---------------------------------------------------------------------------
# Backends
# ---------------------------------------------------------------------------


class Backend(Protocol):
    async def call_tool(self, name: str, args: Mapping[str, Any] | None = None) -> dict[str, Any]: ...

    async def close(self) -> None: ...


class ToolCallFailed(ScenarioError):
    pass


def _extract_tool_result(result: Any) -> dict[str, Any]:
    structured = getattr(result, "structured_content", None)
    if isinstance(structured, dict):
        return dict(structured)
    content = getattr(result, "content", None) or []
    texts = [item.text for item in content if getattr(item, "type", None) == "text" and isinstance(getattr(item, "text", None), str)]
    text = "\n".join(texts).strip()
    if text:
        with contextlib.suppress(json.JSONDecodeError):
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                return parsed
        return {"ok": True, "text": text}
    return {"ok": True}


class StdioBackend:
    """MCP stdio client backend, verified against the installed mcp 2.x API."""

    def __init__(
        self,
        *,
        command: str | None = None,
        args: Sequence[str] | None = None,
        env: Mapping[str, str] | None = None,
        errlog: Any = None,
        cwd: str | Path | None = None,
    ) -> None:
        self.command = command or sys.executable
        self.args = list(args if args is not None else ["-m", "hatari_mcp"])
        self.env = dict(env or {})
        self.errlog = errlog if errlog is not None else sys.stderr
        self.cwd = str(cwd) if cwd is not None else None
        self._stdio_cm: Any = None
        self._session_cm: Any = None
        self.session: Any = None

    async def connect(self) -> None:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        params = StdioServerParameters(
            command=self.command,
            args=self.args,
            env=self.env,
            cwd=self.cwd,
        )
        self._stdio_cm = stdio_client(params, errlog=self.errlog)
        read, write = await self._stdio_cm.__aenter__()
        self._session_cm = ClientSession(read, write)
        self.session = await self._session_cm.__aenter__()
        await self.session.initialize()

    async def call_tool(self, name: str, args: Mapping[str, Any] | None = None) -> dict[str, Any]:
        if self.session is None:
            raise ToolCallFailed("backend_not_connected", "MCP backend is not connected.", "Call connect() first.")
        result = await self.session.call_tool(name, dict(args or {}))
        if getattr(result, "is_error", False):
            text = _extract_tool_result(result).get("text", "")
            raise ToolCallFailed(
                "tool_error",
                f"Tool {name} failed: {text or 'unknown tool error'}",
                "Inspect the step result and server logs.",
                raw=text or None,
            )
        return _extract_tool_result(result)

    async def close(self) -> None:
        with contextlib.suppress(Exception):
            if self._session_cm is not None:
                await self._session_cm.__aexit__(None, None, None)
        with contextlib.suppress(Exception):
            if self._stdio_cm is not None:
                await self._stdio_cm.__aexit__(None, None, None)
        self.session = None
        self._session_cm = None
        self._stdio_cm = None


def default_server_env(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """Build the child environment from a fixed allowlist.

    ``extra`` is filtered through the same allowlist; a caller cannot smuggle
    the full host environment (or an arbitrary secret) into the MCP child.
    """
    source = dict(extra or {})
    env: dict[str, str] = {}
    for key in SAFE_ENV_KEYS:
        value = source.get(key)
        if value is None:
            value = os.environ.get(key)
        if value is not None:
            env[key] = value
    return env


def make_stdio_backend_factory(
    *,
    server_python: str | None = None,
    server_cmd: str | None = None,
    env: Mapping[str, str] | None = None,
    errlog_factory: Callable[[], Any] | None = None,
) -> Callable[[], Backend]:
    if server_cmd:
        parts = shlex.split(server_cmd)
        if not parts:
            raise ScenarioError("bad_option", "Empty --server-cmd.", "Pass a command line.")
        command, args = parts[0], parts[1:]
    else:
        command, args = server_python or sys.executable, ["-m", "hatari_mcp"]

    def factory() -> Backend:
        errlog = errlog_factory() if errlog_factory else sys.stderr
        return StdioBackend(command=command, args=args, env=default_server_env(env), errlog=errlog)

    return factory


# ---------------------------------------------------------------------------
# Replay recording
# ---------------------------------------------------------------------------


class ReplayRecorder:
    def __init__(self, *, scenario_id: str, seed: int = 0) -> None:
        self.scenario_id = scenario_id
        self.seed = seed
        self.start_args: dict[str, Any] | None = None
        self.events: list[dict[str, Any]] = []
        self.checkpoints: list[dict[str, Any]] = []
        self.started = time.monotonic()

    def record_start(self, args: Mapping[str, Any]) -> None:
        self.start_args = dict(args)

    def record_event(self, step_id: str, tool: str, args: Mapping[str, Any]) -> None:
        self.events.append(
            {
                "index": len(self.events),
                "step_id": step_id,
                "tool": tool,
                "args": dict(args),
                "delivered": "host-timed",
                "fidelity": "approximate",
                "recorded_elapsed_ms": _mono_ms(self.started),
            }
        )

    def record_checkpoint(self, kind: str, value: str, step_id: str) -> None:
        self.checkpoints.append(
            {
                "index": len(self.checkpoints),
                "step_id": step_id,
                "kind": kind,
                "value": value,
                "events": len(self.events),
                "recorded_elapsed_ms": _mono_ms(self.started),
            }
        )

    def write(self, bundle: ArtifactBundle) -> None:
        manifest = {
            "schema": REPLAY_SCHEMA,
            "version": 1,
            "scenario": self.scenario_id,
            "seed": self.seed,
            "fidelity": "approximate",
            "frame_locked": False,
            "delivery": "host-timed",
            "start": self.start_args,
            "events": len(self.events),
            "checkpoints": len(self.checkpoints),
            "note": (
                "Input events are recorded with host-timed delivery. Replay is approximate unless "
                "the P7.4/P7.5 frame-locked path is present."
            ),
            "created_utc": _iso_utc_now(),
        }
        bundle.write_json("replay/manifest.json", manifest)
        bundle.write_text(
            "replay/events.jsonl",
            "".join(json.dumps(event, sort_keys=True) + "\n" for event in self.events),
        )
        bundle.write_text(
            "replay/checkpoints.jsonl",
            "".join(json.dumps(item, sort_keys=True) + "\n" for item in self.checkpoints),
        )


def _verify_bundle_checksums(root: Path) -> None:
    """Verify a final bundle exactly and without following host paths.

    Every regular file in the bundle except ``checksums.sha256`` itself must be
    covered exactly once.  Absolute paths, ``..`` traversal, backslashes,
    symlinks and duplicate entries are rejected, so a crafted checksum list
    cannot bless ``/etc/passwd`` or an unlisted file.
    """
    root = root.resolve()
    checksum_path = root / "checksums.sha256"
    if checksum_path.is_symlink() or not checksum_path.is_file():
        raise ScenarioError("replay_untrusted", f"Bundle has no checksums.sha256: {root}", "Only finalized bundles may be replayed.")
    for item in root.rglob("*"):
        if item.is_symlink():
            raise ScenarioError(
                "replay_untrusted",
                f"Bundle contains a symlink: {item}",
                "Re-finalize the bundle without symlinks.",
            )
    entries: dict[str, str] = {}
    for line in checksum_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            digest, rel = line.split("  ", 1)
        except ValueError as exc:
            raise ScenarioError("replay_untrusted", "Malformed checksums.sha256 line.", "Re-finalize the bundle.") from exc
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ScenarioError("replay_untrusted", "Malformed checksums.sha256 digest.", "Re-finalize the bundle.")
        rel_path = Path(rel)
        if (
            not rel
            or rel_path.is_absolute()
            or ".." in rel_path.parts
            or not rel_path.parts
            or "\\" in rel
            or rel == "checksums.sha256"
        ):
            raise ScenarioError(
                "replay_untrusted",
                f"Checksum entry is outside the bundle: {rel}",
                "Checksum only relative bundle files.",
            )
        target = root / rel_path
        try:
            target.resolve(strict=False).relative_to(root)
        except (ValueError, OSError) as exc:
            raise ScenarioError("replay_untrusted", f"Checksum entry escapes the bundle: {rel}", "Re-finalize the bundle.") from exc
        if target.is_symlink() or not target.is_file():
            raise ScenarioError("replay_untrusted", f"Checksummed file is missing or a symlink: {rel}", "Re-record the bundle.")
        if rel in entries:
            raise ScenarioError("replay_untrusted", f"Duplicate checksum entry: {rel}", "Re-finalize the bundle.")
        entries[rel] = digest
    files = {
        item.relative_to(root).as_posix()
        for item in root.rglob("*")
        if item.is_file() and not item.is_symlink()
    }
    required = files - {"checksums.sha256"}
    missing = sorted(required - entries.keys())
    extra = sorted(entries.keys() - files)
    if missing or extra:
        detail = []
        if missing:
            detail.append(f"uncovered files: {missing}")
        if extra:
            detail.append(f"unknown entries: {extra}")
        raise ScenarioError(
            "replay_untrusted",
            f"Checksum coverage is incomplete ({'; '.join(detail)}).",
            "Re-finalize the bundle so every file is checksummed exactly once.",
        )
    for rel, digest in entries.items():
        if _file_sha256(root / rel) != digest:
            raise ScenarioError("replay_untrusted", f"Checksum mismatch for {rel}", "The bundle was modified after recording.")


def _read_jsonl(path: Path, what: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    if not path.is_file():
        return items
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ScenarioError("replay_untrusted", f"Invalid {what} line {number}: {exc}", "Re-record the bundle.") from exc
        if not isinstance(value, dict):
            raise ScenarioError("replay_untrusted", f"{what} line {number} is not an object.", "Re-record the bundle.")
        items.append(value)
    return items


async def replay_bundle(
    bundle_dir: str | Path,
    *,
    backend_factory: Callable[[], Backend] | None = None,
    artifacts_root: str | Path | None = None,
    timeout_ms: int = 120_000,
) -> dict[str, Any]:
    """Replay a recorded bundle after validating its integrity and policy.

    This is intentionally a *partial* replay: without P7.4/P7.5 input cannot be
    frame-locked, so every result is labelled ``approximate``.  Every checkpoint
    that can be observed is compared; a bundle with no screen checkpoint is
    reported as ``unverified`` rather than a false pass.
    """
    root = Path(bundle_dir).expanduser().resolve()
    if not (root / ".complete").is_file():
        raise ScenarioError("replay_incomplete", f"Bundle is incomplete (no .complete): {root}", "Replay a finalized attempt directory.")
    bundle_manifest_path = root / "manifest.json"
    if not bundle_manifest_path.is_file():
        raise ScenarioError("replay_incomplete", f"Bundle has no manifest.json: {root}", "Replay a finalized attempt directory.")
    bundle_manifest = _read_json(bundle_manifest_path)
    if not isinstance(bundle_manifest, dict) or bundle_manifest.get("schema") != MANIFEST_SCHEMA:
        raise ScenarioError("replay_untrusted", "Unsupported bundle manifest schema.", f"Expected {MANIFEST_SCHEMA}.")
    _verify_bundle_checksums(root)

    replay_dir = root / "replay"
    manifest_path = replay_dir / "manifest.json"
    if not manifest_path.is_file():
        raise ScenarioError("replay_missing", f"No replay bundle at {root}", "Pass a bundle directory containing replay/.")
    manifest = _read_json(manifest_path)
    if not isinstance(manifest, dict) or manifest.get("schema") != REPLAY_SCHEMA:
        raise ScenarioError("replay_untrusted", "Unsupported replay manifest schema.", f"Expected {REPLAY_SCHEMA}.")
    if manifest.get("version") != 1:
        raise ScenarioError("replay_untrusted", f"Unsupported replay version: {manifest.get('version')!r}", "Use replay v1.")

    events = _read_jsonl(replay_dir / "events.jsonl", "events.jsonl")
    checkpoints = _read_jsonl(replay_dir / "checkpoints.jsonl", "checkpoints.jsonl")
    if not checkpoints:
        raise ScenarioError(
            "replay_unverified",
            "Replay bundle has no checkpoints; it cannot prove replay correctness.",
            "Record at least one decoded-RGBA screen checkpoint.",
        )
    for index, checkpoint in enumerate(checkpoints):
        if not isinstance(checkpoint.get("kind"), str) or not isinstance(checkpoint.get("value"), str):
            raise ScenarioError("replay_untrusted", f"Checkpoint {index} is malformed.", "Re-record the bundle.")
        if checkpoint["kind"] == "screen_rgba_sha256" and not re.fullmatch(r"[0-9a-f]{64}", checkpoint["value"]):
            raise ScenarioError("replay_untrusted", f"Checkpoint {index} is not a SHA-256.", "Re-record the bundle.")
    for index, event in enumerate(events):
        tool = event.get("tool")
        if tool not in INPUT_TOOLS:
            raise ScenarioError(
                "tool_not_allowed",
                f"Replay event {index} invokes disallowed tool {tool!r}.",
                "Replay only recorded input events.",
            )

    workspace = Path(tempfile.mkdtemp(prefix="hatari-mcp-replay-"))
    factory = backend_factory or make_stdio_backend_factory()
    backend = factory()
    screen_values = [item["value"] for item in checkpoints if item.get("kind") == "screen_rgba_sha256"]
    result: dict[str, Any] = {
        "ok": False,
        "status": "pending",
        "fidelity": "approximate",
        "frame_locked": False,
        "events": len(events),
        "checkpoints": len(checkpoints),
        "verified_checkpoints": 0,
        "expected": screen_values[-1] if screen_values else None,
        "actual": None,
        "first_divergence": None,
        "divergences": [],
        "unverified_checkpoints": [],
        "artifacts": None,
    }
    roots = [workspace, Path(tempfile.gettempdir()).resolve()]
    try:
        await asyncio.wait_for(backend.connect(), max(1.0, min(30.0, timeout_ms / 1000.0)))
        start = manifest.get("start")
        if start is not None:
            if not isinstance(start, dict):
                raise ScenarioError("replay_untrusted", "replay.start must be an object.", "Re-record the bundle.")
            start = dict(start)
            _check_unknown(start, EMU_START_ARG_KEYS, "replay.start")
            # The recorder stores the runner-managed screenshot options; strip
            # them before allowlist validation because the replay run recreates
            # its own private screenshot directory below.
            recorded_args = _strip_screenshot_args(list(start.get("args") or []))
            start["args"] = _validate_hatari_args(recorded_args, roots=roots, what="replay.start.args")
            # Never reuse the recorder's now-deleted workspace, and never let a
            # recorded workspace point into a corpus/repository tree or at an
            # arbitrary host directory.
            if start.get("workspace"):
                _validate_workspace_path(
                    str(start["workspace"]),
                    what="replay.start.workspace",
                    roots=roots,
                    environment=os.environ,
                )
            start["workspace"] = str(workspace)
            start["args"] = _ensure_screenshot_args(start["args"], workspace)
            await asyncio.wait_for(backend.call_tool("emu_start", start), timeout_ms / 1000.0)

        by_event: dict[int, list[dict[str, Any]]] = {}
        final_checkpoints: list[dict[str, Any]] = []
        for checkpoint in checkpoints:
            event_index = checkpoint.get("events")
            if isinstance(event_index, int) and 0 <= event_index <= len(events):
                by_event.setdefault(event_index, []).append(checkpoint)
            else:
                final_checkpoints.append(checkpoint)

        async def capture_and_compare(items: list[dict[str, Any]], label: str) -> str | None:
            screen_items = [item for item in items if item.get("kind") == "screen_rgba_sha256"]
            for item in items:
                if item.get("kind") != "screen_rgba_sha256":
                    result["unverified_checkpoints"].append({"kind": item.get("kind"), "step_id": item.get("step_id"), "reason": "unsupported checkpoint kind"})
            if not screen_items:
                return None
            captured = await _capture_screen_with_backend(backend, workspace, name=label, fmt="bmp")
            actual = captured["rgba_sha256"]
            result["actual"] = actual
            for item in screen_items:
                if item.get("value") != actual:
                    divergence = {
                        "kind": "screen_rgba_sha256",
                        "step_id": item.get("step_id"),
                        "expected": item.get("value"),
                        "actual": actual,
                    }
                    result["divergences"].append(divergence)
                    if result["first_divergence"] is None:
                        result["first_divergence"] = divergence
                else:
                    result["verified_checkpoints"] += 1
            return actual

        for index, event in enumerate(events):
            step = {"id": str(event.get("step_id") or f"event-{index}"), "tool": event.get("tool"), "args": event.get("args") or {}}
            _validate_step_paths(step, roots=roots, base=workspace, stage=[], environment=os.environ)
            await asyncio.wait_for(backend.call_tool(step["tool"], step["args"]), timeout_ms / 1000.0)
            if by_event.get(index + 1):
                await capture_and_compare(by_event.pop(index + 1), f"replay-{index + 1}")
        remaining = [item for group in by_event.values() for item in group] + final_checkpoints
        if remaining:
            await capture_and_compare(remaining, "replay-final")

        if result["verified_checkpoints"] == 0:
            result["status"] = "unverified"
            result["ok"] = False
        elif result["divergences"]:
            result["status"] = "diverged"
            result["ok"] = False
        else:
            result["status"] = "verified"
            result["ok"] = True
        if artifacts_root:
            target = Path(artifacts_root).expanduser().resolve() / f"replay-{uuid.uuid4().hex[:8]}"
            _reject_protected_path(target, "replay artifacts root")
            target.mkdir(parents=True, exist_ok=True)
            (target / "replay-result.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
            result["artifacts"] = str(target)
    finally:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(backend.call_tool("emu_stop", {"force": True}), 15)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(backend.close(), 15)
        shutil.rmtree(workspace, ignore_errors=True)
    return result


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def _strip_screenshot_args(args: Sequence[str]) -> list[str]:
    cleaned: list[str] = []
    skip_next = False
    for token in args:
        if skip_next:
            skip_next = False
            continue
        if token in {"--screenshot-dir", "--screenshot-format"}:
            skip_next = True
            continue
        if token.startswith("--screenshot-dir=") or token.startswith("--screenshot-format="):
            continue
        cleaned.append(token)
    return cleaned


def _ensure_screenshot_args(args: Sequence[str], workspace: Path) -> list[str]:
    """Force the runner-managed screenshot directory, dropping user overrides."""
    cleaned = _strip_screenshot_args(args)
    shot_dir = workspace / "captures"
    shot_dir.mkdir(parents=True, exist_ok=True)
    cleaned.extend(["--screenshot-dir", str(shot_dir), "--screenshot-format", "bmp"])
    return cleaned


async def _capture_screen_with_backend(
    backend: Backend,
    workspace: Path,
    *,
    name: str,
    fmt: str = "bmp",
    resume: bool = True,
    timeout_ms: int = 10_000,
) -> dict[str, Any]:
    """Capture via the screenshot shortcut, falling back to the debugger.

    The shortcut path is deliberately preferred: it needs no socket reply, so
    it survives a busy/unresponsive debugger channel.  The debugger fallback is
    kept for builds where the shortcut is unavailable.
    """
    shot_dir = workspace / "captures"
    shot_dir.mkdir(parents=True, exist_ok=True)
    before = {path.name for path in shot_dir.glob("grab*.bmp")}
    target: Path | None = None
    method = "shortcut"
    try:
        await backend.call_tool("emu_shortcut", {"name": "screenshot"})
    except Exception:
        method = "debugger"
    if method == "shortcut":
        deadline = time.monotonic() + max(0.5, min(3.0, timeout_ms / 1000.0))
        while time.monotonic() < deadline:
            candidates = [
                path
                for path in shot_dir.glob("grab*.bmp")
                if path.name not in before and path.stat().st_size > 0
            ]
            if candidates:
                target = max(candidates, key=lambda path: path.stat().st_mtime_ns)
                break
            await asyncio.sleep(0.02)
        if target is None:
            method = "debugger"

    if method == "debugger":
        status = await backend.call_tool("emu_status", {})
        state = str(status.get("state", "unknown"))
        was_running = state == "running"
        if was_running:
            await backend.call_tool("emu_pause", {})
        target = workspace / f"{_sanitize_id(name, fallback='screen')}.{fmt}"
        target.parent.mkdir(parents=True, exist_ok=True)
        if " " in str(target):
            target = workspace / f"screen-{uuid.uuid4().hex[:8]}.{fmt}"
        command = f"screenshot {target}"
        await backend.call_tool("dbg_command", {"command": command, "timeout_ms": timeout_ms})
        deadline = time.monotonic() + max(0.5, min(2.0, timeout_ms / 1000.0))
        while time.monotonic() < deadline:
            if target.is_file() and target.stat().st_size > 0:
                break
            await asyncio.sleep(0.02)
        if not target.is_file() or target.stat().st_size == 0:
            raise ScenarioError(
                "capture_failed",
                "Hatari did not write the requested screenshot.",
                "Rendering must be enabled and the workspace writable; check log_read.",
            )
        if was_running and resume:
            with contextlib.suppress(Exception):
                await backend.call_tool("emu_resume", {})
    if target is None or not target.is_file() or target.stat().st_size == 0:
        raise ScenarioError(
            "capture_failed",
            "Hatari did not write the requested screenshot.",
            "Rendering must be enabled and the workspace writable; check log_read.",
        )
    data = target.read_bytes()
    image = None
    if fmt == "bmp":
        from ..tools.frames import decode_bmp

        decode_deadline = time.monotonic() + max(0.5, min(5.0, timeout_ms / 1000.0))
        while True:
            data = target.read_bytes()
            try:
                image = decode_bmp(data)
                break
            except HatariError as exc:
                if time.monotonic() >= decode_deadline:
                    raise ScenarioError(
                        "capture_failed",
                        f"Hatari wrote an incomplete BMP: {exc.message}",
                        "Rendering must finish before capture; retry or use a larger capture timeout.",
                    ) from exc
                await asyncio.sleep(0.03)
    result: dict[str, Any] = {
        "ok": True,
        "path": str(target),
        "format": fmt,
        "bytes": len(data),
        "file_sha256": _sha256_bytes(data),
        "width": None,
        "height": None,
        "rgba_sha256": None,
        "decoded": False,
        "fidelity": "decoded-rgba",
    }
    if image is not None:
        result.update(
            width=image.width,
            height=image.height,
            bpp=image.bpp,
            rgba_sha256=image.sha256(),
            decoded=True,
        )
    else:
        result["note"] = "PNG is captured as bytes; decoded-RGBA hashing requires bmp."
    result["capture_method"] = method
    return {**result, "bytes_data": data}


def _assert_no_symlinks(source: Path, *, deadline: float | None = None, what: str = "stage source") -> None:
    """Reject every symlink under ``source`` (lstat walk, no following)."""
    if source.is_symlink():
        raise ScenarioError("path_not_allowed", f"{what} is a symlink: {source}", "Stage real files, not links.")
    if not source.is_dir():
        return
    for current, dirnames, filenames in os.walk(source, followlinks=False):
        if deadline is not None and time.monotonic() > deadline:
            raise ScenarioError("staging_timeout", f"Staging exceeded the scenario deadline while scanning {current}.", "Raise the scenario timeout.")
        for name in dirnames + filenames:
            candidate = Path(current) / name
            if candidate.is_symlink():
                raise ScenarioError(
                    "path_not_allowed",
                    f"{what} contains a symlink: {candidate}",
                    "Symlinks are rejected so a corpus tree cannot leak host files into the sandbox.",
                )


def _copy_file_checked(
    source: Path,
    destination: Path,
    *,
    deadline: float | None = None,
    budget: int | None = None,
) -> int:
    """Chunked copy that enforces the scenario deadline and a byte budget."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    copied = 0
    try:
        with source.open("rb") as reader, destination.open("wb") as writer:
            while True:
                if deadline is not None and time.monotonic() > deadline:
                    raise ScenarioError(
                        "staging_timeout",
                        f"Staging exceeded the scenario deadline while copying {source}.",
                        "Raise the scenario timeout or stage fewer files.",
                    )
                chunk = reader.read(STAGING_CHUNK_BYTES)
                if not chunk:
                    break
                copied += len(chunk)
                if budget is not None and copied > budget:
                    raise ScenarioError(
                        "staging_budget_exceeded",
                        f"Staging {source} exceeded the per-case disk budget.",
                        "Stage fewer/smaller files or raise the case budget.",
                    )
                writer.write(chunk)
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    with contextlib.suppress(OSError):
        shutil.copystat(source, destination)
    return copied


def _copy_tree_checked(
    source: Path,
    destination: Path,
    *,
    deadline: float | None = None,
    budget: int | None = None,
) -> int:
    _assert_no_symlinks(source, deadline=deadline)
    if deadline is not None and time.monotonic() > deadline:
        raise ScenarioError("staging_timeout", "Staging exceeded the scenario deadline.", "Raise the scenario timeout.")
    copied = 0
    for current, dirnames, filenames in os.walk(source, followlinks=False):
        if deadline is not None and time.monotonic() > deadline:
            raise ScenarioError(
                "staging_timeout",
                f"Staging exceeded the scenario deadline under {current}.",
                "Raise the scenario timeout or stage fewer files.",
            )
        dirnames.sort()
        relative = Path(current).relative_to(source)
        target_dir = destination / relative
        target_dir.mkdir(parents=True, exist_ok=True)
        for name in sorted(filenames):
            if deadline is not None and time.monotonic() > deadline:
                raise ScenarioError(
                    "staging_timeout",
                    f"Staging exceeded the scenario deadline at {name}.",
                    "Raise the scenario timeout or stage fewer files.",
                )
            remaining = None if budget is None else budget - copied
            if remaining is not None and remaining < 0:
                raise ScenarioError(
                    "staging_budget_exceeded",
                    f"Staging {source} exceeded the per-case disk budget.",
                    "Stage fewer/smaller files or raise the case budget.",
                )
            copied += _copy_file_checked(
                Path(current) / name,
                target_dir / name,
                deadline=deadline,
                budget=remaining,
            )
    return copied


class CaseRun:
    def __init__(
        self,
        case: ResolvedCase,
        *,
        run_id: str,
        artifacts_root: Path,
        backend_factory: Callable[[], Backend],
        environment: Mapping[str, str] | None = None,
        record: bool = False,
        scenario_source: Path | None = None,
        max_bundle_bytes: int = DEFAULT_MAX_BUNDLE_BYTES,
        max_case_bytes: int = DEFAULT_MAX_CASE_BYTES,
        run_budget: RunBudget | None = None,
        attempt: int = 1,
    ) -> None:
        self.case = case
        self.attempt = attempt
        self.run_id = run_id
        self.artifacts_root = artifacts_root
        self.backend_factory = backend_factory
        self.environment = dict(os.environ if environment is None else environment)
        self.record = record or bool(case.resolved.get("record"))
        self.scenario_source = scenario_source
        self.backend: Backend | None = None
        self.bundle: ArtifactBundle | None = None
        self.workspace: Path | None = None
        self.workspace_root: Path | None = None
        self.recorder: ReplayRecorder | None = None
        self.step_records: list[dict[str, Any]] = []
        self.deadline = 0.0
        self.last_success: str | None = None
        self.final_state: dict[str, Any] = {}
        self.teardown_done = False
        self.primary_failure: dict[str, Any] | None = None
        self.secondary_errors: list[dict[str, Any]] = []
        self.started_utc = _iso_utc_now()
        self.started_mono = time.monotonic()
        self.actual_skip_reason: str | None = None
        self.max_bundle_bytes = max_bundle_bytes
        self.max_case_bytes = max(0, int(max_case_bytes))
        self.run_budget = run_budget
        self._server_log_path: Path | None = None
        self.teardown_deadline: float | None = None

    async def run(self) -> dict[str, Any]:
        status = "error"
        # The scenario deadline covers staging and connection, not just steps.
        self.deadline = time.monotonic() + self.case.scenario_ms / 1000.0
        try:
            self.bundle = ArtifactBundle.create(
                self.artifacts_root,
                self.run_id,
                self.case.case_id,
                self.attempt,
                max_bytes=self.max_bundle_bytes,
                run_budget=self.run_budget,
                redact_values=self.case.secret_values,
            )
            self.bundle.write_json("scenario.resolved.json", self.case.resolved)
            self._write_environment()
            blocked = self._prerequisite_error()
            if blocked:
                self.primary_failure = blocked
                self.bundle.write_json("blocked.json", blocked)
                status = "error"
                return self._finish(status)
            skip = self._skip_reason()
            if skip:
                self.actual_skip_reason = skip
                self.bundle.write_json("skip.json", {"reason": skip})
                status = "skipped"
                self.primary_failure = {"error": "skipped", "message": skip}
                return self._finish(status)
            self.recorder = ReplayRecorder(scenario_id=self.case.scenario_id, seed=int(self.case.resolved.get("seed", 0))) if self.record else None
            await asyncio.to_thread(self._prepare_workspace)
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise ScenarioError("scenario_timeout", "Scenario deadline expired during staging.", "Raise timeouts.scenario_ms.")
            connect_timeout = max(0.1, min(30.0, remaining))
            try:
                await asyncio.wait_for(self._connect(), connect_timeout)
            except asyncio.TimeoutError as exc:
                raise ScenarioError(
                    "connect_timeout",
                    f"MCP server did not connect within {connect_timeout:.1f} s.",
                    "Check the server command and HATARI_MCP_* environment.",
                ) from exc
            status, failure = await self._run_steps()
            self.primary_failure = failure
            await self._teardown()
            return self._finish(status)
        except asyncio.CancelledError:
            self.primary_failure = {"error": "cancelled", "message": "Scenario run was cancelled."}
            with contextlib.suppress(Exception):
                await asyncio.shield(self._teardown())
            with contextlib.suppress(Exception):
                await asyncio.shield(self._collect_logs())
            return self._finish("cancelled")
        except ArtifactQuotaExceeded as exc:
            self.primary_failure = {"error": "artifact_quota_exceeded", "message": str(exc)}
            with contextlib.suppress(Exception):
                await self._teardown()
            return self._finish("error")
        except ScenarioError as exc:
            code = exc.code
            status = "error" if code in {"backend_not_connected", "tool_error", "capture_failed", "connect_timeout"} else "failed"
            self.primary_failure = {
                "error": code,
                "message": exc.message,
                "hint": exc.hint,
                "step_index": None,
                "step_id": None,
                "phase": None,
                "last_success": self.last_success,
            }
            with contextlib.suppress(Exception):
                await self._teardown()
            return self._finish(status)
        except Exception as exc:  # pragma: no cover - defensive
            self.primary_failure = {"error": "internal_error", "message": str(exc)}
            with contextlib.suppress(Exception):
                await self._teardown()
            return self._finish("error")
        finally:
            await self._close()

    def _prerequisite_error(self) -> dict[str, Any] | None:
        rom = self.case.requires.get("rom")
        if not rom:
            return None
        tos = self._find_tos()
        message: str | None = None
        code = "missing_rom"
        if tos is None:
            message = "A ROM was required but no TOS/EmuTOS image was found."
        elif rom == "emutos" and not _looks_like_emutos(tos):
            code = "rom_mismatch"
            message = f"Scenario requires EmuTOS but found {tos.name}."
        elif rom == "tos" and _looks_like_emutos(tos):
            code = "rom_mismatch"
            message = f"Scenario requires a real TOS image but found {tos.name}."
        if message is None:
            return None
        return {
            "error": code,
            "message": message,
            "hint": "Set HATARI_TOS to a matching image; a missing required ROM is an error, not a skip.",
            "step_index": None,
            "step_id": None,
            "phase": None,
            "last_success": None,
        }

    def _skip_reason(self) -> str | None:
        explicit = self.case.skip_reason
        reasons: list[str] = []
        for name in self.case.requires.get("env", []):
            if not self.environment.get(name):
                reasons.append(f"missing environment variable {name}")
        for tool in self.case.requires.get("tools", []):
            if shutil.which(tool) is None:
                reasons.append(f"missing tool {tool}")
        # A missing/wrong required ROM is an error (see _prerequisite_error),
        # not a skip: CI must not silently pass an un-runnable case.
        for path in self.case.requires.get("files", []):
            try:
                resolved = Path(str(path)).expanduser()
                if not resolved.exists():
                    reasons.append(f"missing required path {resolved}")
            except OSError:
                reasons.append(f"missing required path {path}")
        for path in self.case.requires.get("dirs", []):
            try:
                resolved = Path(str(path)).expanduser()
                if not resolved.is_dir():
                    reasons.append(f"missing required directory {resolved}")
            except OSError:
                reasons.append(f"missing required directory {path}")
        if reasons:
            return explicit or "; ".join(reasons)
        return None

    def _find_tos(self) -> Path | None:
        explicit = self.environment.get("HATARI_TOS")
        if explicit:
            candidate = Path(explicit).expanduser()
            if candidate.is_file():
                return candidate
        for step in self.case.steps:
            if step["tool"] == "emu_start":
                tos = step.get("args", {}).get("tos") or self.case.config.get("tos")
                if tos and not str(tos).startswith("<missing:"):
                    candidate = Path(str(tos)).expanduser()
                    if candidate.is_file():
                        return candidate
        try:
            from ..config import Settings

            return Settings.load().default_tos
        except Exception:  # pragma: no cover - defensive
            return None

    def _prepare_workspace(self) -> None:
        root = Path(tempfile.mkdtemp(prefix=f"hatari-mcp-{_sanitize_id(self.case.case_id)}-"))
        self.workspace_root = root
        self.workspace = root
        staged_root = root / "staged"
        staged_root.mkdir(parents=True, exist_ok=True)
        staged_total = 0
        for entry in self.case.stage:
            source = Path(entry["from"]).expanduser()
            destination = staged_root / entry["to"]
            if self.deadline and time.monotonic() > self.deadline:
                raise ScenarioError("staging_timeout", "Staging exceeded the scenario deadline.", "Raise the scenario timeout.")
            if not source.exists():
                if entry.get("optional"):
                    continue
                raise ScenarioError(
                    "missing_prerequisite",
                    f"Stage source does not exist: {source}",
                    "Install the external corpus tree or mark the entry optional.",
                )
            if source.is_symlink():
                raise ScenarioError("path_not_allowed", f"Stage source is a symlink: {source}", "Stage real files.")
            remaining_budget = self.max_case_bytes - staged_total
            if remaining_budget < 0:
                raise ScenarioError(
                    "staging_budget_exceeded",
                    f"Staging exceeded the per-case disk budget of {self.max_case_bytes} bytes.",
                    "Stage fewer/smaller files or raise the case budget.",
                )
            if source.is_dir():
                staged_total += _copy_tree_checked(
                    source,
                    destination,
                    deadline=self.deadline,
                    budget=remaining_budget,
                )
            elif source.is_file():
                staged_total += _copy_file_checked(
                    source,
                    destination,
                    deadline=self.deadline,
                    budget=remaining_budget,
                )
            else:
                raise ScenarioError("path_not_allowed", f"Stage source is not a regular file or directory: {source}", "Stage real files.")
        if self.case.stage:
            self.workspace = staged_root
        if self.bundle is not None:
            self.bundle.allowed_sources = [root.resolve(), Path(tempfile.gettempdir()).resolve()]
        self.bundle.write_json(
            "stage.json",
            {
                "workspace": str(self.workspace),
                "entries": self.case.stage,
                "read_only_source": True,
            },
        )

    def _write_environment(self) -> None:
        allow = {"HATARI_BIN", "HATARI_TOS", "HATARI_MCP_REPO", "OKAMI_ROOT", "FREEMINT_ROOT", "GODLIB_ROOT"}
        values: dict[str, Any] = {}
        redactions: list[str] = []
        for key in sorted(allow):
            if key not in self.environment:
                continue
            if _looks_secret(key):
                values[key] = "<redacted>"
                redactions.append(key)
            else:
                values[key] = self.environment[key]
        data = {
            "env": values,
            "redactions": redactions,
            "host": host_metadata(),
            "determinism": determinism_report(self.case.determinism),
            "workspace": str(self.workspace) if self.workspace else None,
            "mcp_version": __version__,
            "git_commit": git_commit(),
            "hatari_version": hatari_version(self.environment.get("HATARI_BIN")),
        }
        self.bundle.write_json("environment.json", data)

    async def _connect(self) -> None:
        assert self.workspace_root is not None
        log_path = self.workspace_root / "server.log"
        self._server_log_path = log_path
        errlog = open(log_path, "a", encoding="utf-8")  # noqa: SIM115 - lives until _close
        self._errlog = errlog
        try:
            self.backend = self.backend_factory()
            if self.case.determinism == "strict" and isinstance(self.backend, StdioBackend):
                self.backend.env = {**self.backend.env, **determinism_environment("strict")}
            connect = getattr(self.backend, "connect", None)
            if connect is not None:
                await connect()
        except Exception:
            with contextlib.suppress(Exception):
                errlog.close()
            raise

    def _close_timeout(self, default: float = 15.0) -> float:
        if self.teardown_deadline is None:
            return default
        return max(0.1, min(default, self.teardown_deadline - time.monotonic()))

    async def _close(self) -> None:
        if self.backend is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self.backend.close(), self._close_timeout())
            self.backend = None
        errlog = getattr(self, "_errlog", None)
        if errlog is not None:
            with contextlib.suppress(Exception):
                errlog.flush()
            with contextlib.suppress(Exception):
                errlog.close()
            self._errlog = None
        if self.workspace_root is not None:
            shutil.rmtree(self.workspace_root, ignore_errors=True)
        self.workspace_root = None
        self.workspace = None

    def _flush_server_log(self) -> None:
        if self.bundle is None or self._server_log_path is None:
            return
        errlog = getattr(self, "_errlog", None)
        if errlog is not None:
            with contextlib.suppress(Exception):
                errlog.flush()
        with contextlib.suppress(OSError):
            if self._server_log_path.is_file():
                data = self._server_log_path.read_bytes()
                if data:
                    self.bundle.write_bytes("logs/server.log", data)
        self._server_log_path = None

    async def _run_steps(self) -> tuple[str, dict[str, Any] | None]:
        for index, step in enumerate(self.case.steps):
            remaining_ms = int((self.deadline - time.monotonic()) * 1000)
            if remaining_ms <= 0:
                return "failed", self._failure(index, step, "scenario_timeout", "Scenario deadline expired.")
            timeout_ms = min(int(step["timeout_ms"]), remaining_ms)
            step_started = time.monotonic()
            record: dict[str, Any] = {
                "index": index,
                "id": step["id"],
                "tool": step["tool"],
                "started_utc": _iso_utc_now(),
                "timeout_ms": timeout_ms,
            }
            try:
                result = await asyncio.wait_for(self._run_step(index, step), timeout_ms / 1000.0)
                record.update(status="passed", duration_ms=_mono_ms(step_started), result=_trim_result(result))
                self.step_records.append(record)
                self.last_success = step["id"]
            except asyncio.TimeoutError:
                record.update(status="timeout", duration_ms=_mono_ms(step_started))
                self.step_records.append(record)
                await self._best_effort_capture(step["id"])
                return "failed", self._failure(
                    index,
                    step,
                    "step_timeout",
                    f"Step {step['id']} exceeded {timeout_ms} ms.",
                    elapsed_ms=_mono_ms(step_started),
                )
            except ScenarioError as exc:
                record.update(
                    status="failed",
                    duration_ms=_mono_ms(step_started),
                    error={"error": exc.code, "message": exc.message, "hint": exc.hint},
                )
                self.step_records.append(record)
                await self._best_effort_capture(step["id"])
                failure = self._failure(
                    index,
                    step,
                    exc.code,
                    exc.message,
                    hint=exc.hint,
                    elapsed_ms=_mono_ms(step_started),
                )
                failure.update(exc.extra)
                return ("error" if exc.code in {"tool_error", "backend_not_connected"} else "failed"), failure
            except Exception as exc:  # pragma: no cover - defensive
                record.update(
                    status="error",
                    duration_ms=_mono_ms(step_started),
                    error={"error": "internal_error", "message": str(exc)},
                )
                self.step_records.append(record)
                return "error", self._failure(index, step, "internal_error", str(exc))
        return "passed", None

    def _failure(self, index: int, step: Mapping[str, Any], code: str, message: str, *, hint: str = "", elapsed_ms: int = 0) -> dict[str, Any]:
        return {
            "error": code,
            "message": message,
            "hint": hint,
            "step_index": index,
            "step_id": step.get("id"),
            "phase": step.get("tool"),
            "elapsed_ms": elapsed_ms,
            "last_success": self.last_success,
        }

    async def _run_step(self, index: int, step: Mapping[str, Any]) -> dict[str, Any]:
        assert self.backend is not None
        tool = str(step["tool"])
        args = dict(step.get("args") or {})
        if tool == "run_prg":
            args = self._run_prg_args(args)
        elif tool in {"media_insert_floppy", "media_insert_cartridge"}:
            args = self._media_insert_args(tool, args)
        if tool == "wait":
            wait_ms = args.get("ms", args.get("timeout_ms", 1000))
            await asyncio.sleep(max(0, int(wait_ms)) / 1000.0)
            result: dict[str, Any] = {"ok": True, "waited_ms": int(wait_ms), "host_timed": True}
        elif tool == "emu_start":
            start_args = self._start_args(args)
            if self.recorder:
                self.recorder.record_start(start_args)
            result = await self.backend.call_tool(tool, start_args)
        elif tool in {"screen_frame_capture", "screen_screenshot"}:
            capture = step.get("capture") or {"kind": "screenshot", "name": step["id"], "format": "bmp", "hash": "rgba", "resume": True}
            fmt = str(args.get("format") or capture.get("format") or "bmp").lower().lstrip(".")
            if fmt == "png" and capture.get("hash") == "rgba":
                raise ScenarioError(
                    "bad_option",
                    f"Step {step['id']} asks for decoded-RGBA hashing of PNG bytes.",
                    "Use format=bmp for rgba hashes, or hash=sha256 for PNG.",
                )
            name = str(capture.get("name") or step["id"])
            captured = await _capture_screen_with_backend(
                self.backend,
                self._workspace_path(),
                name=name,
                fmt=fmt if fmt in SCREEN_FORMATS else "bmp",
                resume=bool(capture.get("resume", True)),
                timeout_ms=int(step["timeout_ms"]),
            )
            data = captured.pop("bytes_data")
            rel = str(capture.get("path") or f"screenshots/{_sanitize_id(name)}.{fmt}")
            written = self.bundle.write_bytes(rel, data)
            captured["artifact"] = written["path"]
            if self.recorder and captured.get("rgba_sha256"):
                self.recorder.record_checkpoint("screen_rgba_sha256", captured["rgba_sha256"], str(step["id"]))
            result = captured
        else:
            result = await self.backend.call_tool(tool, args)
            if self.recorder and tool in INPUT_TOOLS:
                self.recorder.record_event(str(step["id"]), tool, args)

        if step.get("expect"):
            self._check_expect(result, step["expect"], step)
        for assertion in step.get("asserts") or []:
            self._check_assertion(result, assertion, step)
        if step.get("capture") and tool not in {"screen_frame_capture", "screen_screenshot"}:
            self._capture_result(result, step, index)
        return result

    def _start_args(self, raw: Mapping[str, Any]) -> dict[str, Any]:
        args = dict(raw)
        config = self.case.config
        if config.get("profile") and "profile" not in args:
            args["profile"] = config["profile"]
        if config.get("tos") and "tos" not in args:
            args["tos"] = config["tos"]
        if config.get("workspace") and "workspace" not in args:
            args["workspace"] = config["workspace"]
        if config.get("autostart") and "autostart" not in args:
            args["autostart"] = config["autostart"]
        for key in ("gemdos_drive", "gemdos_case", "gemdos_time", "gemdos_conv", "write_protect", "display"):
            if key in config and key not in args:
                args[key] = config[key]
        if self.case.stage and "workspace" not in args:
            args["workspace"] = str(self._workspace_path())
        extra_args = list(config.get("args") or [])
        extra_args.extend(args.get("args") or [])
        extra_args.extend(determinism_args(self.case.determinism))
        extra_args.extend(self.case.resolved.get("determinism_args") or [])
        roots = _allowed_roots(self.scenario_source, self.environment)
        workspace_value = args.get("workspace")
        if workspace_value and not _has_missing_placeholder(workspace_value):
            candidate = Path(str(workspace_value)).expanduser()
            if not candidate.is_absolute():
                candidate = (self.scenario_source.parent if self.scenario_source else Path.cwd()) / candidate
            if candidate.resolve(strict=False) != self._workspace_path().resolve():
                validated = _validate_workspace_path(
                    workspace_value,
                    what="emu_start.workspace",
                    roots=roots,
                    base=self.scenario_source.parent if self.scenario_source else Path.cwd(),
                    environment=self.environment,
                )
                if not validated.exists():
                    validated.mkdir(mode=0o700, parents=True, exist_ok=True)
                args["workspace"] = str(validated)
        extra_args = _validate_hatari_args(extra_args, roots=roots, what="emu_start.args")
        args["args"] = _ensure_screenshot_args(extra_args, self._workspace_path())
        return args

    def _workspace_path(self) -> Path:
        assert self.workspace is not None
        return self.workspace

    def _run_prg_args(self, raw: Mapping[str, Any]) -> dict[str, Any]:
        """Force ``run_prg`` onto the runner-created, read-write workspace."""
        args = dict(raw)
        runner_workspace = self._workspace_path().resolve()
        workspace_value = args.get("workspace")
        if workspace_value:
            roots = _allowed_roots(self.scenario_source, self.environment)
            base = self.scenario_source.parent if self.scenario_source else Path.cwd()
            candidate = _validate_workspace_path(
                workspace_value,
                what="run_prg.workspace",
                roots=roots,
                base=base,
                environment=self.environment,
            )
            if candidate != runner_workspace and not _is_within(candidate, runner_workspace):
                raise ScenarioError(
                    "path_not_allowed",
                    f"run_prg.workspace must be the runner-created per-case workspace: {candidate}",
                    "Omit run_prg.workspace and stage the program into the runner workspace instead.",
                )
        else:
            candidate = runner_workspace
        _reject_protected_path(candidate, "run_prg.workspace", environment=self.environment)
        if not _is_within(candidate, _system_temp_root()):
            raise ScenarioError(
                "path_not_allowed",
                f"run_prg.workspace must stay below the system temp root: {candidate}",
                "Use the runner-created workspace; never mount an arbitrary host tree.",
            )
        args["workspace"] = str(candidate)

        program_value = args.get("program", args.get("prg"))
        if program_value is None:
            raise ScenarioError(
                "bad_option",
                "run_prg requires a program path.",
                "Pass a program relative to the per-case workspace.",
            )
        program_path = _validate_path_value(
            program_value,
            roots=[candidate],
            what="run_prg.program",
            base=candidate,
            allow_missing=True,
        )
        _reject_protected_path(program_path, "run_prg.program", environment=self.environment)
        args["program"] = str(program_path)
        if "prg" in args:
            args["prg"] = str(program_path)
        return args

    def _media_insert_args(self, tool: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        """Confine inserted media images to the runner-created workspace copy."""
        args = dict(raw)
        workspace = self._workspace_path().resolve()
        for key, value in list(args.items()):
            if not isinstance(value, str) or not value.strip():
                continue
            if key in BENIGN_TEXT_ARG_KEYS:
                continue
            if key not in PATH_ARG_KEYS and not _looks_like_path(value):
                continue
            candidate = _validate_path_value(
                value,
                roots=[workspace],
                what=f"{tool}.{key}",
                base=workspace,
                allow_missing=False,
            )
            _reject_protected_path(candidate, f"{tool}.{key}", environment=self.environment)
            if not _is_within(candidate, workspace):
                raise ScenarioError(
                    "path_not_allowed",
                    f"{tool}.{key} must point inside the per-case workspace: {candidate}",
                    "Stage the media image into the runner workspace first; external media is never mounted writable.",
                )
            args[key] = str(candidate)
        return args

    def _check_expect(self, result: Mapping[str, Any], expect: Mapping[str, Any], step: Mapping[str, Any]) -> None:
        for path, expected in expect.items():
            actual = _get_path(result, path)
            if actual != expected:
                raise ScenarioError(
                    "assertion_failed",
                    f"Step {step['id']} expected {path}={expected!r}, got {actual!r}.",
                    "Inspect the captured result and adjust the expectation.",
                    extra={
                        "assertion": {
                            "kind": "expect",
                            "path": path,
                            "expected": expected,
                            "actual": actual,
                        }
                    },
                )

    def _check_assertion(self, result: Mapping[str, Any], assertion: Mapping[str, Any], step: Mapping[str, Any]) -> None:
        path = str(assertion["path"])
        actual = _get_path(result, path)
        failures: list[str] = []
        if "eq" in assertion and actual != assertion["eq"]:
            failures.append(f"expected {assertion['eq']!r}")
        if "ne" in assertion and actual == assertion["ne"]:
            failures.append(f"expected not {assertion['ne']!r}")
        if "contains" in assertion and (actual is None or str(assertion["contains"]) not in str(actual)):
            failures.append(f"expected to contain {assertion['contains']!r}")
        if "regex" in assertion:
            if actual is None or re.search(str(assertion["regex"]), str(actual)) is None:
                failures.append(f"expected regex {assertion['regex']!r}")
        if "gte" in assertion and (not isinstance(actual, (int, float)) or actual < assertion["gte"]):
            failures.append(f"expected >= {assertion['gte']!r}")
        if "lte" in assertion and (not isinstance(actual, (int, float)) or actual > assertion["lte"]):
            failures.append(f"expected <= {assertion['lte']!r}")
        if "exists" in assertion and bool(actual is not None) != bool(assertion["exists"]):
            failures.append(f"expected exists={assertion['exists']!r}")
        if "type" in assertion:
            type_name = type(actual).__name__
            expected_type = str(assertion["type"])
            if expected_type == "int" and (isinstance(actual, bool) or not isinstance(actual, int)):
                failures.append("expected type int")
            elif expected_type == "str" and not isinstance(actual, str):
                failures.append("expected type str")
            elif expected_type not in {"int", "str", "bool", "number", "dict", "list"}:
                failures.append(f"unknown type {expected_type!r}")
            elif expected_type == "number" and (isinstance(actual, bool) or not isinstance(actual, (int, float))):
                failures.append("expected type number")
            elif expected_type in {"bool", "dict", "list"} and type_name != expected_type:
                failures.append(f"expected type {expected_type}")
        if failures:
            raise ScenarioError(
                "assertion_failed",
                f"Step {step['id']} assertion on {path} failed: {', '.join(failures)} (actual={actual!r}).",
                "Inspect the captured result and adjust the assertion.",
                extra={
                    "assertion": {
                        "kind": "assert",
                        "path": path,
                        "expected": {key: value for key, value in assertion.items() if key != "path"},
                        "actual": actual,
                    }
                },
            )

    def _capture_result(self, result: Mapping[str, Any], step: Mapping[str, Any], index: int) -> None:
        capture = step["capture"]
        name = capture["name"]
        kind = capture["kind"]
        if kind == "result":
            rel = capture.get("path") or f"results/{index:02d}-{_sanitize_id(name)}.json"
            self.bundle.write_json(str(rel), _trim_result(result))
            return
        if kind == "file":
            path_value = result.get("path") or result.get("artifact")
            if not path_value:
                raise ScenarioError(
                    "capture_failed",
                    f"Step {step['id']} capture=file but the result has no path.",
                    "Return a path field or use capture.kind=result.",
                )
            roots = [self._workspace_path(), Path(tempfile.gettempdir()).resolve()]
            source = _validate_path_value(path_value, roots=roots, what=f"step {step['id']} capture source")
            _reject_protected_path(str(source), f"step {step['id']} capture source")
            if not source.is_file():
                raise ScenarioError("capture_failed", f"Artifact file does not exist: {source}", "Check the step result.")
            suffix = source.suffix or ""
            rel = capture.get("path") or f"artifacts/{_sanitize_id(name)}{suffix}"
            self.bundle.copy_file(str(rel), source)
            return
        # kind == screenshot for a non-screenshot tool
        raise ScenarioError(
            "bad_option",
            f"Step {step['id']} asks for a screenshot capture from {step['tool']}.",
            "Use tool=screen_frame_capture for screenshots.",
        )

    async def _best_effort_capture(self, step_id: str) -> None:
        if self.backend is None or self.workspace is None:
            return
        try:
            captured = await asyncio.wait_for(
                _capture_screen_with_backend(
                    self.backend,
                    self._workspace_path(),
                    name=f"failure-{step_id}",
                    fmt="bmp",
                    resume=False,
                    timeout_ms=3000,
                ),
                5.0,
            )
        except Exception:
            return
        data = captured.pop("bytes_data")
        with contextlib.suppress(Exception):
            self.bundle.write_bytes(f"screenshots/failure-{_sanitize_id(step_id)}.bmp", data)
        with contextlib.suppress(Exception):
            self.bundle.write_json("failure-observation.json", _trim_result(captured))

    async def _collect_logs(self, *, deadline: float | None = None) -> None:
        if self.backend is None:
            return
        for tool, rel in (("log_read", "logs/stderr.log"), ("console_read", "logs/stdout.log")):
            timeout = 2.0
            if deadline is not None:
                timeout = min(timeout, deadline - time.monotonic())
            if timeout <= 0:
                continue
            try:
                result = await asyncio.wait_for(self.backend.call_tool(tool, {"lines": 500}), timeout)
            except Exception:
                continue
            entries = result.get("entries") or []
            lines = []
            for entry in entries:
                if isinstance(entry, dict):
                    prefix = f"[{entry.get('kind')}] " if entry.get("kind") else ""
                    lines.append(prefix + str(entry.get("text", "")))
                else:
                    lines.append(str(entry))
            with contextlib.suppress(Exception):
                self.bundle.write_text(rel, "\n".join(lines) + ("\n" if lines else ""))

    async def _teardown(self) -> None:
        if self.teardown_done:
            return
        self.teardown_done = True
        if self.backend is None:
            return
        deadline = time.monotonic() + MAX_TEARDOWN_SECONDS
        self.teardown_deadline = deadline
        remaining = deadline - time.monotonic()
        if remaining > 0:
            with contextlib.suppress(Exception):
                self.final_state = await asyncio.wait_for(self.backend.call_tool("emu_status", {}), min(3.0, remaining))
        await self._collect_logs(deadline=deadline)
        timed_out = False
        for step in self.case.teardown:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                self.secondary_errors.append(
                    {
                        "step_id": step["id"],
                        "tool": step["tool"],
                        "error": "teardown_timeout",
                        "message": "Teardown deadline expired before this step.",
                    }
                )
                break
            timeout = min(int(step.get("timeout_ms") or STEP_TIMEOUTS["teardown"]), 30_000) / 1000.0
            timeout = min(timeout, remaining)
            try:
                await asyncio.wait_for(self.backend.call_tool(step["tool"], step.get("args") or {}), timeout)
            except asyncio.TimeoutError as exc:
                timed_out = True
                self.secondary_errors.append(
                    {
                        "step_id": step["id"],
                        "tool": step["tool"],
                        "error": "teardown_timeout",
                        "message": f"Teardown step exceeded {timeout:.1f} s.",
                    }
                )
            except Exception as exc:
                self.secondary_errors.append(
                    {
                        "step_id": step["id"],
                        "tool": step["tool"],
                        "error": getattr(exc, "code", type(exc).__name__),
                        "message": str(exc),
                    }
                )
        if timed_out:
            # A wedged server cannot run further teardown.  Closing the stdio
            # transport terminates the whole child process tree (bounded by the
            # same single teardown deadline).
            remaining = max(0.1, deadline - time.monotonic())
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self.backend.call_tool("emu_stop", {"force": True}), min(2.0, remaining))
            remaining = max(0.1, deadline - time.monotonic())
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self.backend.close(), min(15.0, remaining))
            self.backend = None

    def _finish(self, status: str) -> dict[str, Any]:
        assert self.bundle is not None
        duration_ms = _mono_ms(self.started_mono)
        try:
            self._flush_server_log()
        except ArtifactQuotaExceeded as exc:
            self.secondary_errors.append({"error": "artifact_quota_exceeded", "message": str(exc)})
        if self.recorder is not None:
            try:
                self.recorder.write(self.bundle)
            except ArtifactQuotaExceeded as exc:
                self.secondary_errors.append({"error": "artifact_quota_exceeded", "message": str(exc)})
        if self.step_records:
            try:
                self.bundle.write_text(
                    "logs/events.jsonl",
                    "".join(json.dumps(record, sort_keys=True) + "\n" for record in self.step_records),
                )
            except ArtifactQuotaExceeded as exc:
                self.secondary_errors.append({"error": "artifact_quota_exceeded", "message": str(exc)})
        if self.bundle.quota_exceeded and status in {"passed", "skipped"}:
            status = "error"
            if self.primary_failure is None:
                self.primary_failure = {
                    "error": "artifact_quota_exceeded",
                    "message": "Artifact bundle exceeded its size quota.",
                }
        failure = self.primary_failure
        if failure and self.secondary_errors:
            failure = {**failure, "secondary_errors": self.secondary_errors}
        failure = _redact_secrets(failure, self.case.secret_values)
        manifest = {
            "scenario": self.case.scenario_id,
            "scenario_hash": self.case.resolved_hash,
            "case_id": self.case.case_id,
            "axes": self.case.axes,
            "started_utc": self.started_utc,
            "finished_utc": _iso_utc_now(),
            "duration_ms": duration_ms,
            "determinism": determinism_report(
                self.case.determinism,
                {
                    "args": self.case.resolved.get("determinism_args"),
                    "residual": self.case.resolved.get("determinism_residual"),
                },
            ),
            "seed": int(self.case.resolved.get("seed", 0)),
            "exit_code": self.final_state.get("program_exit_code", self.final_state.get("returncode")),
            "crashed": bool(self.final_state.get("exception")),
            "last_success": self.last_success,
            "failure": failure,
            "secondary_errors": self.secondary_errors,
            "steps": self.step_records,
            "skip_reason": self.actual_skip_reason if status == "skipped" else None,
            "environment": "environment.json",
            "resolved_scenario": "scenario.resolved.json",
            "quota_exceeded": self.bundle.quota_exceeded,
            "host": host_metadata(),
            "mcp_version": __version__,
            "git_commit": git_commit(),
            "hatari_version": hatari_version(self.environment.get("HATARI_BIN")),
        }
        manifest = _redact_secrets(manifest, self.case.secret_values)
        final: Path | None = None
        for attempt in range(2):
            try:
                final = self.bundle.finalize(status, manifest)
                break
            except Exception as exc:
                self.secondary_errors.append(
                    {"error": "finalize_failed", "message": f"{type(exc).__name__}: {exc}"}
                )
                if status in {"passed", "skipped"}:
                    status = "error"
                    failure = {
                        "error": "finalize_failed",
                        "message": f"Artifact bundle could not be finalized: {exc}",
                    }
                    self.primary_failure = failure
                manifest["status"] = status
                manifest["failure"] = _redact_secrets(failure, self.case.secret_values)
                manifest["secondary_errors"] = self.secondary_errors
                if attempt == 1:
                    final = self.bundle.staging
        return {
            "case": self.case.case_id,
            "scenario": self.case.scenario_id,
            "status": status,
            "duration_ms": duration_ms,
            "failure": failure,
            "secondary_errors": self.secondary_errors,
            "artifacts": str(final),
            "manifest_status": status,
            "observables": self._observables(),
            "summary": {
                "steps": len(self.step_records),
                "last_success": self.last_success,
                "secondary_errors": len(self.secondary_errors),
            },
        }

    def _observables(self) -> dict[str, Any]:
        screens = [
            record.get("result", {}).get("rgba_sha256")
            for record in self.step_records
            if isinstance(record.get("result"), dict) and record["result"].get("rgba_sha256")
        ]
        return {
            "screen_rgba_sha256": screens,
            "exit_code": self.final_state.get("program_exit_code", self.final_state.get("returncode")),
            "crashed": bool(self.final_state.get("exception")),
        }


def _get_path(value: Any, path: str) -> Any:
    current = value
    for part in path.split("."):
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, list) and part.isdigit():
            index = int(part)
            current = current[index] if 0 <= index < len(current) else None
        else:
            return None
    return current


def _trim_value(value: Any, *, depth: int = 0, max_string: int = 20_000, max_items: int = 200) -> Any:
    if isinstance(value, str):
        return value if len(value) <= max_string else value[:max_string] + "...[truncated]"
    if isinstance(value, bytes):
        return f"<{len(value)} bytes>"
    if depth >= 8:
        return "<nested result truncated>"
    if isinstance(value, Mapping):
        items = list(value.items())[:max_items]
        return {str(key): (None if key == "bytes_data" else _trim_value(item, depth=depth + 1, max_string=max_string, max_items=max_items)) for key, item in items}
    if isinstance(value, (list, tuple)):
        return [_trim_value(item, depth=depth + 1, max_string=max_string, max_items=max_items) for item in value[:max_items]]
    return value


def _trim_result(result: Mapping[str, Any]) -> dict[str, Any]:
    return {
        str(key): _trim_value(value)
        for key, value in result.items()
        if key not in {"bytes_data"}
    }


def _looks_secret(key: str) -> bool:
    upper = key.upper()
    return any(
        word in upper
        for word in (
            "TOKEN",
            "SECRET",
            "PASSWORD",
            "API_KEY",
            "ACCESS_KEY",
            "PRIVATE_KEY",
            "CREDENTIAL",
            "SESSION_KEY",
        )
    )


def _looks_like_emutos(path: Path) -> bool:
    name = path.name.lower()
    return "emutos" in name or name.startswith("etos")


def host_metadata() -> dict[str, Any]:
    try:
        load = [round(value, 3) for value in os.getloadavg()]
    except (OSError, AttributeError):  # pragma: no cover - platform dependent
        load = []
    return {
        "system": platform.system(),
        "release": platform.release(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "cpu_count": os.cpu_count(),
        "load_at_start": load,
    }


def git_commit() -> str | None:
    env = os.environ.get("HATARI_MCP_GIT_COMMIT")
    if env:
        return env
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(Path(__file__).resolve().parents[3]),
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def hatari_version(hatari_bin: str | None = None) -> str | None:
    binary = hatari_bin
    if not binary:
        try:
            from ..config import Settings

            found = Settings.load().hatari_bin
            binary = str(found) if found else None
        except Exception:  # pragma: no cover - defensive
            binary = None
    if not binary:
        return None
    try:
        result = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=3, check=False)
        lines = [line.strip() for line in (result.stdout or result.stderr).splitlines() if line.strip()]
        return lines[0] if lines else None
    except (OSError, subprocess.SubprocessError):
        return None


# ---------------------------------------------------------------------------
# Engine, reports and exit codes
# ---------------------------------------------------------------------------


@dataclass
class RunResult:
    run_id: str
    report: dict[str, Any]
    artifacts: Path
    cases: list[dict[str, Any]]
    exit_code: int

    @property
    def ok(self) -> bool:
        return self.exit_code == EXIT_OK


def summarize(cases: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    summary = {"passed": 0, "failed": 0, "skipped": 0, "error": 0, "cancelled": 0}
    for case in cases:
        status = str(case.get("status", "error"))
        if status in summary:
            summary[status] += 1
        else:
            summary["error"] += 1
    return summary


def exit_code_for(summary: Mapping[str, int], *, fail_on_skip: bool = False, infra_error: bool = False, watchdog: bool = False) -> int:
    if watchdog:
        return EXIT_WATCHDOG
    if infra_error:
        return EXIT_INFRA
    if summary.get("error"):
        return EXIT_INTERNAL if not summary.get("failed") else EXIT_FAILED
    if summary.get("failed") or summary.get("cancelled"):
        return EXIT_FAILED
    if summary.get("skipped") and fail_on_skip:
        return EXIT_FAILED
    return EXIT_OK


def build_report(
    *,
    run_id: str,
    started_utc: str,
    finished_utc: str,
    cases: list[dict[str, Any]],
    environment: Mapping[str, Any] | None = None,
    scenario: str | None = None,
) -> dict[str, Any]:
    summary = summarize(cases)
    return {
        "schema": REPORT_SCHEMA,
        "run_id": run_id,
        "scenario": scenario,
        "started_utc": started_utc,
        "finished_utc": finished_utc,
        "environment": dict(environment or {}),
        "summary": summary,
        "cases": cases,
    }


def build_junit_xml(report: Mapping[str, Any], *, max_output_chars: int = 8192) -> str:
    summary = report.get("summary", {})
    cases = report.get("cases", [])
    suite = ET.Element(
        "testsuite",
        {
            "name": str(report.get("scenario") or "hatari-mcp"),
            "tests": str(len(cases)),
            "failures": str(summary.get("failed", 0) + summary.get("cancelled", 0)),
            "errors": str(summary.get("error", 0)),
            "skipped": str(summary.get("skipped", 0)),
            "time": f"{sum(int(case.get('duration_ms', 0)) for case in cases) / 1000.0:.3f}",
        },
    )
    properties = ET.SubElement(suite, "properties")
    for key, value in (
        ("run_id", report.get("run_id")),
        ("mcp_version", report.get("environment", {}).get("mcp_version")),
        ("hatari_version", report.get("environment", {}).get("hatari_version")),
        ("determinism", report.get("environment", {}).get("determinism")),
        ("resource_mechanism", report.get("environment", {}).get("resource_mechanism")),
    ):
        if value is None:
            continue
        prop = ET.SubElement(properties, "property", {"name": str(key), "value": str(value)})
        prop.text = ""
    seen_names: dict[str, int] = {}
    for case in cases:
        base_name = str(case.get("case"))
        seen_names[base_name] = seen_names.get(base_name, 0) + 1
        case_name = base_name if seen_names[base_name] == 1 else f"{base_name}-{seen_names[base_name]}"
        testcase = ET.SubElement(
            suite,
            "testcase",
            {
                "classname": str(case.get("scenario") or report.get("scenario") or "hatari-mcp"),
                "name": case_name,
                "time": f"{int(case.get('duration_ms', 0)) / 1000.0:.3f}",
            },
        )
        status = str(case.get("status"))
        failure = case.get("failure") or {}
        message = str(failure.get("message") or failure.get("error") or "")
        detail = _failure_detail(case, failure)
        if status == "skipped":
            ET.SubElement(testcase, "skipped", {"message": message or "skipped"})
        elif status in {"failed", "cancelled"}:
            element = ET.SubElement(
                testcase,
                "failure",
                {
                    "message": message[: max_output_chars],
                    "type": str(failure.get("error") or status),
                },
            )
            element.text = detail[:max_output_chars]
        elif status == "error":
            element = ET.SubElement(
                testcase,
                "error",
                {
                    "message": message[: max_output_chars],
                    "type": str(failure.get("error") or "error"),
                },
            )
            element.text = detail[:max_output_chars]
        stdout = ET.SubElement(testcase, "system-out")
        stdout.text = json.dumps(
            {"case": case.get("case"), "artifacts": case.get("artifacts"), "summary": case.get("summary")},
            sort_keys=True,
        )[:max_output_chars]
        stderr = ET.SubElement(testcase, "system-err")
        stderr.text = detail[:max_output_chars]
    ET.indent(suite, space="  ")
    return ET.tostring(suite, encoding="unicode")


def _failure_detail(case: Mapping[str, Any], failure: Mapping[str, Any]) -> str:
    parts = [
        f"case: {case.get('case')}",
        f"error: {failure.get('error')}",
        f"step_index: {failure.get('step_index')}",
        f"step_id: {failure.get('step_id')}",
        f"phase: {failure.get('phase')}",
        f"last_success: {failure.get('last_success')}",
        f"artifacts: {case.get('artifacts')}",
    ]
    if failure.get("message"):
        parts.append(f"message: {failure['message']}")
    if failure.get("secondary_errors"):
        parts.append(f"secondary_errors: {json.dumps(failure['secondary_errors'], sort_keys=True)}")
    return "\n".join(parts)


def build_annotations(report: Mapping[str, Any]) -> list[dict[str, Any]]:
    annotations: list[dict[str, Any]] = []
    for case in report.get("cases", []):
        status = case.get("status")
        if status in {"passed", "skipped"}:
            continue
        failure = case.get("failure") or {}
        level = "error" if status == "error" else "warning"
        annotations.append(
            {
                "level": level,
                "title": f"scenario {case.get('scenario')} / {case.get('case')}",
                "message": str(failure.get("message") or failure.get("error") or status),
                "file": None,
                "line": None,
            }
        )
    return annotations


def emit_github_annotations(report: Mapping[str, Any], stream: Any = None) -> None:
    import sys as _sys

    target = stream or _sys.stdout
    for annotation in build_annotations(report):
        level = "error" if annotation["level"] == "error" else "warning"
        message = str(annotation["message"]).replace("\n", "%0A")
        print(f"::{level}::{annotation['title']}: {message}", file=target)


class ScenarioEngine:
    def __init__(
        self,
        *,
        backend_factory: Callable[[], Backend] | None = None,
        server_python: str | None = None,
        server_cmd: str | None = None,
        environment: Mapping[str, str] | None = None,
        max_cases: int | None = None,
        fail_fast: bool = False,
        jobs: int = 1,
        fail_on_skip: bool = False,
        run_id: str | None = None,
        max_bundle_bytes: int = DEFAULT_MAX_BUNDLE_BYTES,
        max_run_bytes: int = DEFAULT_MAX_RUN_BYTES,
        max_case_bytes: int = DEFAULT_MAX_CASE_BYTES,
        record: bool = False,
        max_depth: int = MAX_SCENARIO_DEPTH,
    ) -> None:
        self.backend_factory = backend_factory or make_stdio_backend_factory(
            server_python=server_python, server_cmd=server_cmd, env=environment
        )
        self.environment = dict(os.environ if environment is None else environment)
        self.max_cases = max_cases
        self.fail_fast = fail_fast
        self.jobs = max(1, min(int(jobs), 8))
        self.fail_on_skip = fail_on_skip
        self.run_id = run_id or f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
        self.max_bundle_bytes = max_bundle_bytes
        self.max_run_bytes = max_run_bytes
        self.max_case_bytes = max_case_bytes
        self.record = record
        self.max_depth = max_depth
        self.interrupted = False
        self.watchdog = False

    def validate(self, path: str | Path) -> dict[str, Any]:
        return validate_scenario(path, max_cases=self.max_cases, environment=self.environment)

    async def run(self, path: str | Path, *, artifacts_root: str | Path | None = None) -> RunResult:
        depth = _SCENARIO_DEPTH.get()
        if depth >= self.max_depth:
            raise ScenarioError(
                "recursion_limit",
                f"Scenario nesting depth {depth + 1} exceeds the limit {self.max_depth}.",
                "Do not invoke scenario tools recursively from scenario steps.",
            )
        token = _SCENARIO_DEPTH.set(depth + 1)
        try:
            return await self._run(path, artifacts_root=artifacts_root)
        finally:
            _SCENARIO_DEPTH.reset(token)

    async def _run(self, path: str | Path, *, artifacts_root: str | Path | None = None) -> RunResult:
        scenario = load_scenario(path, environment=self.environment)
        cases = expand_matrix(scenario, max_cases=self.max_cases)
        raw_root = Path(artifacts_root).expanduser() if artifacts_root else Path(tempfile.gettempdir()) / "hatari-mcp-artifacts"
        if raw_root.is_symlink():
            raise ScenarioError("path_not_allowed", f"Artifact root must not be a symlink: {raw_root}", "Use a real directory.")
        root = raw_root.resolve()
        _reject_protected_path(root, "artifacts root", environment=self.environment)
        root.mkdir(parents=True, exist_ok=True)
        if root.is_symlink() or not root.is_dir():
            raise ScenarioError("path_not_allowed", f"Artifact root is not a real directory: {root}", "Use a real directory.")
        run_root = root / _sanitize_id(self.run_id, fallback="run")
        if run_root.is_symlink():
            raise ScenarioError("path_not_allowed", f"Run artifact directory is a symlink: {run_root}", "Remove the symlink.")
        run_root.mkdir(parents=True, exist_ok=True)
        os.chmod(run_root, 0o700)
        started_utc = _iso_utc_now()
        results: list[dict[str, Any]] = []
        run_secret_values: set[str] = set()

        effective_fail_fast = bool(self.fail_fast or scenario.matrix_meta.get("fail_fast"))
        semaphore = asyncio.Semaphore(self.jobs)
        failure_seen = asyncio.Event()
        run_budget = RunBudget(self.max_run_bytes)

        async def run_one(matrix_case: MatrixCase) -> dict[str, Any]:
            async with semaphore:
                if effective_fail_fast and failure_seen.is_set():
                    return {
                        "case": matrix_case.id,
                        "scenario": scenario.id,
                        "status": "skipped",
                        "duration_ms": 0,
                        "failure": {"error": "fail_fast", "message": "Skipped after an earlier failure."},
                        "artifacts": None,
                    }
                try:
                    run_secret_values.update(
                        _secret_values_for_case(scenario, matrix_case, self.environment)
                    )
                except Exception:
                    # Secret collection must never block a run; if it fails the
                    # case resolution below will still report the real error.
                    pass
                try:
                    resolved = resolve_case(matrix_case, environment=self.environment, check_paths=True)
                    run_secret_values.update(resolved.secret_values)
                except ScenarioError as exc:
                    return {
                        "case": matrix_case.id,
                        "scenario": scenario.id,
                        "status": "error",
                        "duration_ms": 0,
                        "failure": {
                            "error": exc.code,
                            "message": _redact_secrets(exc.message, run_secret_values),
                            "hint": _redact_secrets(exc.hint, run_secret_values),
                        },
                        "artifacts": None,
                    }
                retry_on = set(scenario.retry_on)
                result: dict[str, Any] = {}
                for attempt in range(1, max(1, scenario.attempts) + 1):
                    runner = CaseRun(
                        resolved,
                        run_id=self.run_id,
                        artifacts_root=run_root,
                        backend_factory=self.backend_factory,
                        environment=self.environment,
                        record=self.record,
                        scenario_source=scenario.source,
                        max_bundle_bytes=self.max_bundle_bytes,
                        max_case_bytes=self.max_case_bytes,
                        run_budget=run_budget,
                        attempt=attempt,
                    )
                    result = await runner.run()
                    result["attempt"] = attempt
                    if result["status"] not in {"failed", "error"} or attempt >= scenario.attempts:
                        break
                    error_code = str((result.get("failure") or {}).get("error") or "")
                    if error_code == "step_timeout" and error_code not in retry_on:
                        break
                    if retry_on and error_code not in retry_on:
                        break
                if result.get("status") in {"failed", "error", "cancelled"}:
                    failure_seen.set()
                return result

        tasks = [asyncio.create_task(run_one(case)) for case in cases]
        try:
            for finished in asyncio.as_completed(tasks):
                result = await finished
                result["artifacts"] = _relative_artifact(result.get("artifacts"), run_root)
                results.append(result)
        except asyncio.CancelledError:
            self.interrupted = True
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            if any(not task.done() for task in tasks):
                await asyncio.gather(*tasks, return_exceptions=True)

        order = {case.id: index for index, case in enumerate(cases)}
        results.sort(key=lambda item: order.get(item["case"], 10**9))
        if run_secret_values:
            results = _redact_secrets(results, run_secret_values)
        finished_utc = _iso_utc_now()
        environment = {
            "host": host_metadata(),
            "mcp_version": __version__,
            "git_commit": git_commit(),
            "hatari_version": hatari_version(self.environment.get("HATARI_BIN")),
            "determinism": determinism_report(scenario.determinism),
            "resource_mechanism": resource_mechanism(),
            "run_root": str(run_root),
            "run_budget": {"max_bytes": run_budget.max_bytes, "used_bytes": run_budget.used, "exceeded": run_budget.exceeded},
        }
        report = build_report(
            run_id=self.run_id,
            started_utc=started_utc,
            finished_utc=finished_utc,
            cases=results,
            environment=environment,
            scenario=scenario.id,
        )
        # CI output files written directly at the run root are part of the run
        # budget too.  Serialize them first, reserve their real sizes, then
        # re-render with the finalized budget accounting until stable.
        def _render_run_outputs() -> tuple[bytes, bytes, bytes]:
            report["environment"]["run_budget"] = {
                "max_bytes": run_budget.max_bytes,
                "used_bytes": run_budget.used,
                "exceeded": run_budget.exceeded,
            }
            return (
                (json.dumps(report, indent=2, sort_keys=True) + "\n").encode("utf-8"),
                (build_junit_xml(report) + "\n").encode("utf-8"),
                (json.dumps(build_annotations(report), indent=2, sort_keys=True) + "\n").encode("utf-8"),
            )

        reserved = 0
        payloads = _render_run_outputs()
        for _ in range(6):
            total = sum(len(payload) for payload in payloads)
            if total <= reserved:
                break
            run_budget.reserve(total - reserved, enforce=False)
            reserved = total
            payloads = _render_run_outputs()
        payloads = _render_run_outputs()
        for _ in range(3):
            total = sum(len(payload) for payload in payloads)
            if total <= reserved:
                break
            run_budget.reserve(total - reserved, enforce=False)
            reserved = total
            payloads = _render_run_outputs()
        report_bytes, junit_bytes, annotations_bytes = payloads
        (run_root / "report.json").write_bytes(report_bytes)
        (run_root / "junit.xml").write_bytes(junit_bytes)
        (run_root / "annotations.json").write_bytes(annotations_bytes)
        if os.environ.get("GITHUB_ACTIONS", "").lower() == "true":
            emit_github_annotations(report)
        exit_code = exit_code_for(report["summary"], fail_on_skip=self.fail_on_skip, watchdog=self.watchdog)
        return RunResult(run_id=self.run_id, report=report, artifacts=run_root, cases=results, exit_code=exit_code)


def _relative_artifact(value: Any, run_root: Path) -> Any:
    if not value:
        return value
    path = Path(str(value))
    with contextlib.suppress(ValueError):
        return str(path.relative_to(run_root))
    return str(path)


def resource_mechanism() -> str:
    if Path("/sys/fs/cgroup/cgroup.controllers").is_file():
        return "cgroup-v2-detected (caps not enforced by this runner)"
    return "rlimits-not-enforced (policy labels only)"


def measure_repeatability(
    *,
    runs: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    observables = [run.get("observables") or {} for run in runs]
    if not observables:
        return {"repeatable": False, "first_divergence": None, "runs": 0}
    first = observables[0]
    for index, current in enumerate(observables[1:], start=1):
        for key in ("screen_rgba_sha256", "exit_code", "crashed"):
            if current.get(key) != first.get(key):
                return {
                    "repeatable": False,
                    "first_divergence": {
                        "run_index": index,
                        "observable": key,
                        "expected": first.get(key),
                        "actual": current.get(key),
                    },
                    "runs": len(runs),
                }
    return {"repeatable": True, "first_divergence": None, "runs": len(runs)}
