"""Evidence classification, hypotheses and triage bundles for the crash detective.

Everything in this module is pure host-side logic.  It never talks to the MCP
server, so the classification rules, diagnosis ranking and bundle writer can be
tested without an emulator.  Hypotheses are explicitly labelled as hypotheses:
the module records the observations that produced them and never claims a
confirmed root cause.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import struct
import threading
import time
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

try:  # package import
    from common import config as config_module
except ImportError:  # direct script import
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from common import config as config_module  # type: ignore[no-redef]

SCHEMA_VERSION = 1
NONBLACK_THRESHOLD = 0.01
BLANK_THRESHOLD = 0.02

# Atari ST family visible modes and Hatari's border-inclusive captures.  The
# range is deliberately broad: the useful "bad resolution" hypothesis is for
# wildly wrong values, not for legal overscan variants.
_MIN_WIDTH = 160
_MAX_WIDTH = 2048
_MIN_HEIGHT = 100
_MAX_HEIGHT = 1200

EXCEPTION_RE = re.compile(
    r"\b(?:bus\s*(?:error|err)|address\s*error|illegal\s+instruction|zero\s*divide|"
    r"chk\s*(?:exception)?|trapv(?:\s+exception)?|privilege\s+violation|"
    r"trace\s+exception|line\s*[af]\s*(?:exception|emulator)|mmu\s+(?:exception|fault)|"
    r"fatal\s+exception|guest\s+exception)\b",
    re.IGNORECASE,
)
DISK_ERROR_RE = re.compile(
    r"\b(?:no\s+disk|insert\s+(?:a\s+)?disk|disk\s+not|no\s+boot|not\s+bootable|"
    r"bad\s+boot|boot\s+sector|drive\s+[ab]\b|read\s+error|seek\s+error|"
    r"fdc\s+error|crc\s+error)\b",
    re.IGNORECASE,
)
GEMDOS_RE = re.compile(
    r"\b(?:gemdos|harddrive|hard\s+disk|workspace|autostart|autoexec|"
    r"drive\s+[c-z]\b|pterm|\.prg\b)\b",
    re.IGNORECASE,
)
FATAL_RE = re.compile(
    r"\b(?:fatal|panic|unhandled|segmentation|crash|exception|bus\s*error|"
    r"address\s*error|illegal\s+instruction|i/o\s+error|read\s+error|"
    r"write\s+error|insert\s+(?:a\s+)?disk|no\s+disk|not\s+bootable|"
    r"bad\s+boot|missing|not\s+found|failed)\b",
    re.IGNORECASE,
)
RESET_RE = re.compile(
    r"\b(?:(?:cold|warm|hard|soft)\s+reset|reset\s+(?:requested|issued)|"
    r"machine\s+reset)\b",
    re.IGNORECASE,
)
TERMINAL_STATES = {"stopped", "stopping", "exited", "crashed", "failed"}
ALIVE_STATES = {"starting", "running", "at_prompt", "remote_paused", "unresponsive"}

# Hatari's --symload exec prints these while it loads the autostarted program.
# They are bookkeeping noise, not guest failures.
NOISE_PATTERNS = (
    "reading symbols from",
    "symbol table missing",
    "reading symbols from program",
    "can not parse relocation table",
    "tos executable",
    "screen dump to",
)


# ---------------------------------------------------------------------------
# Small file helpers
# ---------------------------------------------------------------------------


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 256), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(data)
    os.replace(temporary, path)


def _jsonable(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def bmp_stats(path: str | Path) -> dict[str, Any]:
    """Sample a 24/32-bit uncompressed BMP and report non-black coverage."""
    data = Path(path).read_bytes()
    if len(data) < 54 or data[:2] != b"BM":
        raise ValueError("not a BMP frame")
    offset = struct.unpack_from("<I", data, 10)[0]
    width, raw_height = struct.unpack_from("<ii", data, 18)
    bpp = struct.unpack_from("<H", data, 28)[0]
    compression = struct.unpack_from("<I", data, 30)[0]
    if width <= 0 or raw_height == 0 or bpp not in (24, 32) or compression != 0:
        raise ValueError("unsupported BMP frame")
    height = abs(raw_height)
    top_down = raw_height < 0
    stride = ((width * bpp + 31) // 32) * 4
    bytes_per_pixel = bpp // 8
    if offset + stride * height > len(data):
        raise ValueError("truncated BMP frame")

    step_x = max(1, width // 160)
    step_y = max(1, height // 120)
    sampled = 0
    nonblack = 0
    colors: set[int] = set()
    for y in range(0, height, step_y):
        source_row = y if top_down else height - 1 - y
        row = offset + source_row * stride
        for x in range(0, width, step_x):
            pixel = row + x * bytes_per_pixel
            blue, green, red = data[pixel], data[pixel + 1], data[pixel + 2]
            sampled += 1
            if max(red, green, blue) >= 16:
                nonblack += 1
            colors.add((red << 16) | (green << 8) | blue)
    return {
        "width": width,
        "height": height,
        "bpp": bpp,
        "sampled_pixels": sampled,
        "nonblack_pixels": nonblack,
        "nonblack_ratio": round(nonblack / sampled, 6) if sampled else 0.0,
        "distinct_colors": len(colors),
    }


def bmp_to_ppm_bytes(path: str | Path) -> bytes:
    """Convert an uncompressed BMP capture to binary PPM for Tk previews."""
    data = Path(path).read_bytes()
    if len(data) < 54 or data[:2] != b"BM":
        raise ValueError("not a BMP frame")
    offset = struct.unpack_from("<I", data, 10)[0]
    width, raw_height = struct.unpack_from("<ii", data, 18)
    bpp = struct.unpack_from("<H", data, 28)[0]
    compression = struct.unpack_from("<I", data, 30)[0]
    if width <= 0 or raw_height == 0 or bpp not in (24, 32) or compression != 0:
        raise ValueError("unsupported BMP frame")
    height = abs(raw_height)
    top_down = raw_height < 0
    stride = ((width * bpp + 31) // 32) * 4
    pixel_bytes = bpp // 8
    if offset + stride * height > len(data):
        raise ValueError("truncated BMP frame")
    rows = bytearray()
    for y in range(height):
        source_row = y if top_down else height - 1 - y
        row = offset + source_row * stride
        for x in range(width):
            pixel = row + x * pixel_bytes
            rows.extend((data[pixel + 2], data[pixel + 1], data[pixel]))
    return b"P6\n%d %d\n255\n" % (width, height) + bytes(rows)


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def _entry_texts(value: Any) -> list[str]:
    texts: list[str] = []
    if isinstance(value, str):
        texts.append(value)
    elif isinstance(value, Mapping):
        for key in ("text", "line", "message", "raw", "error"):
            item = value.get(key)
            if isinstance(item, str):
                texts.append(item)
    elif isinstance(value, Sequence):
        for item in value:
            texts.extend(_entry_texts(item))
    return texts


def log_lines(evidence: Mapping[str, Any], channel: str | None = None) -> list[str]:
    logs = evidence.get("logs") or {}
    if not isinstance(logs, Mapping):
        return []
    lines: list[str] = []
    for name in (("stderr", "console") if channel is None else (channel,)):
        lines.extend(_entry_texts(logs.get(name)))
    return lines


def _noise(line: str) -> bool:
    lowered = line.casefold()
    return any(pattern in lowered for pattern in NOISE_PATTERNS)


def _fatal_lines(lines: Iterable[str]) -> list[str]:
    return [line for line in lines if not _noise(line) and FATAL_RE.search(line)]


def _exception_text(evidence: Mapping[str, Any]) -> str | None:
    status = evidence.get("status") or {}
    if isinstance(status, Mapping):
        exception = status.get("exception")
        if isinstance(exception, Mapping) and exception:
            pieces = [str(exception.get(key)) for key in ("type", "line", "message") if exception.get(key)]
            return " ".join(pieces) or json.dumps(exception, sort_keys=True)
        if isinstance(exception, str) and exception.strip():
            return exception.strip()
    result = evidence.get("result") or {}
    if isinstance(result, Mapping):
        reason = str(result.get("failure_reason") or "")
        message = str(result.get("message") or "")
        if reason == "exception":
            return message or "guest exception"
    for line in log_lines(evidence):
        if EXCEPTION_RE.search(line):
            return line.strip()
    return None


def _frame_list(evidence: Mapping[str, Any]) -> list[dict[str, Any]]:
    frames = evidence.get("frames")
    if not isinstance(frames, list):
        frames = []
    result: list[dict[str, Any]] = []
    for frame in frames:
        if isinstance(frame, Mapping):
            result.append(dict(frame))
    return result


def _timeline(evidence: Mapping[str, Any]) -> list[dict[str, Any]]:
    for key in ("timeline", "samples"):
        samples = evidence.get(key)
        if isinstance(samples, list):
            return [dict(item) for item in samples if isinstance(item, Mapping)]
    result = evidence.get("result") or {}
    if isinstance(result, Mapping):
        value = result.get("samples")
        if isinstance(value, list):
            return [dict(item) for item in value if isinstance(item, Mapping)]
    return []


def _max_ratio(frames: Sequence[Mapping[str, Any]], timeline: Sequence[Mapping[str, Any]]) -> float:
    ratios = [float(frame.get("nonblack_ratio") or 0.0) for frame in frames]
    ratios += [float(sample.get("nonblack_ratio") or 0.0) for sample in timeline]
    return max(ratios) if ratios else 0.0


def _reset_lines(lines: Sequence[str]) -> list[str]:
    return [line for line in lines if RESET_RE.search(line)]


def _disk_error_lines(lines: Sequence[str]) -> list[str]:
    return [line for line in lines if not _noise(line) and DISK_ERROR_RE.search(line)]


def _is_terminal(status: Mapping[str, Any]) -> bool:
    return str(status.get("state") or "") in TERMINAL_STATES


def classify(evidence: Mapping[str, Any]) -> dict[str, Any]:
    """Classify a run as booted, failed or undetermined from observed evidence."""
    result = evidence.get("result") or {}
    status = evidence.get("status") or {}
    if not isinstance(result, Mapping):
        result = {}
    if not isinstance(status, Mapping):
        status = {}
    frames = _frame_list(evidence)
    timeline = _timeline(evidence)
    lines = log_lines(evidence)
    signal_list: list[dict[str, Any]] = []

    def signal(code: str, label: str, weight: float, detail: str) -> None:
        signal_list.append(
            {
                "code": code,
                "label": label,
                "weight": round(max(0.0, min(1.0, float(weight))), 3),
                "detail": detail,
            }
        )

    reason = str(result.get("failure_reason") or "")
    message = str(result.get("message") or "")
    state = str(status.get("state") or "unknown")
    ok = bool(result.get("ok"))
    exception = _exception_text(evidence)
    fatal = _fatal_lines(lines)
    disk_errors = _disk_error_lines(lines)
    resets = _reset_lines(lines)
    max_ratio = _max_ratio(frames, timeline)
    capture_errors = [
        str(frame.get("capture_error"))
        for frame in frames
        if frame.get("capture_error")
    ] + [
        str(sample.get("capture_error"))
        for sample in timeline
        if sample.get("capture_error")
    ]

    if reason in {"start_failed", "status_failed"}:
        signal("start_failure", "Emulator failed to start", 0.99, message or reason)
        return _finish(False, reason, 0.99, signal_list, evidence)
    if reason == "disk_prepare_failed":
        signal("disk_prepare_failed", "Disk could not be prepared", 0.99, message)
        return _finish(False, "disk_prepare_failed", 0.99, signal_list, evidence)

    if exception:
        signal("guest_exception", "Guest CPU exception recorded", 0.97, exception)
    if reason == "exception":
        signal("guest_exception", "Session reported an exception", 0.97, message)
    if _is_terminal(status) and state not in {"stopped", "stopping"}:
        detail = f"state={state} exit_reason={status.get('exit_reason')} returncode={status.get('returncode')}"
        signal("emulator_exit", "Emulator left the running state", 0.86, detail)
    if status.get("at_prompt") or state == "at_prompt":
        signal("debugger_prompt", "Emulator entered the debugger prompt", 0.8, message or "at_prompt")
    if state == "unresponsive" or reason == "emulator_unresponsive":
        signal("unresponsive", "Emulator stopped answering heartbeats", 0.82, message or state)
    if reason == "capture_failed" or len([item for item in capture_errors if item]) >= 3:
        detail = "; ".join(item for item in capture_errors[:3] if item) or message
        signal("capture_failed", "Screen capture failed repeatedly", 0.78, detail)
    if disk_errors:
        signal("disk_error", "Disk/drive error in console or log", 0.82, disk_errors[-1])
    if fatal:
        signal("console_error", "Fatal-looking console/log line", 0.62, fatal[-1])
    if len(resets) >= 2:
        signal("repeated_resets", "Repeated machine resets in console/log", 0.6, f"{len(resets)} reset lines; last: {resets[-1]}")
    if frames:
        signal("frames_captured", "Screen frames were captured", 0.3, f"{len(frames)} frame(s)")
    if max_ratio >= NONBLACK_THRESHOLD:
        signal("visible_frame", "A non-black frame was observed", 0.4, f"max non-black ratio {max_ratio:.3f}")
    else:
        if frames:
            signal("blank_screen", "Captured frames were black/blank", 0.86, f"max non-black ratio {max_ratio:.3f}")
        else:
            signal("no_visible_frame", "No usable screen frame was captured", 0.82, message or "no frames")

    timeline_changed = False
    digests = [str(sample.get("digest") or "") for sample in timeline if sample.get("digest")]
    if len(set(digests)) > 1:
        timeline_changed = True

    if reason == "timeout_blank_screen":
        category = "blank_screen"
    elif reason == "timeout_no_stable_frame":
        category = "stalled_boot"
    elif reason == "debugger_prompt":
        category = "debugger_prompt"
    elif reason == "emulator_unresponsive":
        category = "unresponsive"
    elif reason == "capture_failed":
        category = "capture_failed"
    elif reason in {"emulator_exited", "process_exit"}:
        category = "emulator_exit"
    elif exception:
        category = "exception"
    elif _is_terminal(status) and state not in {"stopped", "stopping"}:
        category = "emulator_exit"
    elif status.get("at_prompt") or state == "at_prompt":
        category = "debugger_prompt"
    elif state == "unresponsive":
        category = "unresponsive"
    elif reason == "capture_failed":
        category = "capture_failed"
    elif disk_errors and not ok:
        category = "disk_error"
    elif fatal and not ok:
        category = "console_error"
    elif len(resets) >= 2 and not ok:
        category = "repeated_resets"
    elif ok:
        category = "booted_with_warnings" if fatal else "booted_visible"
    elif frames and max_ratio < BLANK_THRESHOLD:
        category = "blank_screen"
    elif not frames:
        category = "no_visible_frame"
    elif max_ratio < NONBLACK_THRESHOLD:
        category = "blank_screen"
    elif not timeline_changed:
        category = "stalled_boot"
    else:
        category = "stalled_boot"

    booted: bool | None
    if category in {"booted_visible", "booted_with_warnings"}:
        booted = True
    elif category == "unknown":
        booted = None
    else:
        booted = False
    return _finish(booted, category, None, signal_list, evidence)


def _finish(
    booted: bool | None,
    category: str,
    forced_confidence: float | None,
    signals: list[dict[str, Any]],
    evidence: Mapping[str, Any],
) -> dict[str, Any]:
    if forced_confidence is not None:
        confidence = forced_confidence
    elif booted is True:
        warnings = len([item for item in signals if item["code"] in {"console_error", "repeated_resets"}])
        confidence = max(0.5, 0.9 - 0.08 * warnings)
    else:
        weights = [float(item["weight"]) for item in signals]
        confidence = max(weights) if weights else 0.2
        confidence = min(0.99, confidence + 0.01 * max(0, len(set(item["code"] for item in signals)) - 1))
    category_label = {
        "booted_visible": "Booted: visible frame",
        "booted_with_warnings": "Booted with warnings",
        "exception": "Failed: guest exception",
        "emulator_exit": "Failed: emulator exited",
        "debugger_prompt": "Failed: debugger prompt",
        "unresponsive": "Failed: emulator unresponsive",
        "capture_failed": "Failed: screen capture failure",
        "disk_error": "Failed: disk/drive error",
        "console_error": "Failed: fatal console/log error",
        "repeated_resets": "Failed: repeated resets",
        "blank_screen": "Failed: black/blank screen",
        "no_visible_frame": "Failed: no visible frame",
        "stalled_boot": "Failed: stalled boot",
        "start_failed": "Failed: emulator did not start",
        "status_failed": "Failed: status poll failed",
        "disk_prepare_failed": "Failed: disk preparation",
        "unknown": "Undetermined",
    }.get(category, category)
    result = evidence.get("result") or {}
    message = str(result.get("message") or "") if isinstance(result, Mapping) else ""
    summary = category_label
    if message:
        summary += f" — {message}"
    return {
        "schema": SCHEMA_VERSION,
        "booted": booted,
        "category": category,
        "category_label": category_label,
        "confidence": round(float(confidence), 3),
        "summary": summary,
        "signals": signals,
        "observations": {
            "state": (evidence.get("status") or {}).get("state") if isinstance(evidence.get("status"), Mapping) else None,
            "max_nonblack_ratio": round(_max_ratio(_frame_list(evidence), _timeline(evidence)), 6),
            "frame_count": len(_frame_list(evidence)),
            "fatal_log_lines": len(_fatal_lines(log_lines(evidence))),
            "reset_lines": len(_reset_lines(log_lines(evidence))),
            "disk_error_lines": len(_disk_error_lines(log_lines(evidence))),
        },
    }


# ---------------------------------------------------------------------------
# Hypotheses
# ---------------------------------------------------------------------------


def _fmt(value: Any, fallback: str = "unknown") -> str:
    if value is None or value == "":
        return fallback
    return str(value)


def _game_id(evidence: Mapping[str, Any]) -> str:
    game = evidence.get("game") or {}
    return str(game.get("id") or game.get("path") or "unknown") if isinstance(game, Mapping) else "unknown"


def diagnose(
    evidence: Mapping[str, Any], classification: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """Return ranked hypotheses.  Every hypothesis carries its exact evidence."""
    result = evidence.get("result") or {}
    status = evidence.get("status") or {}
    launch = evidence.get("launch") or {}
    source = evidence.get("source") or {}
    game = evidence.get("game") or {}
    debug = evidence.get("debug") or {}
    if not isinstance(result, Mapping):
        result = {}
    if not isinstance(status, Mapping):
        status = {}
    if not isinstance(launch, Mapping):
        launch = {}
    if not isinstance(source, Mapping):
        source = {}
    if not isinstance(game, Mapping):
        game = {}
    if not isinstance(debug, Mapping):
        debug = {}
    category = str(classification.get("category") or "unknown")
    booted = classification.get("booted")
    frames = _frame_list(evidence)
    lines = log_lines(evidence)
    max_ratio = _max_ratio(frames, _timeline(evidence))
    exception = _exception_text(evidence)
    hypotheses: list[dict[str, Any]] = []

    def add(
        identifier: str,
        title: str,
        confidence: float,
        explanation: str,
        items: Iterable[str],
    ) -> None:
        evidence_items = [str(item).strip() for item in items if str(item).strip()]
        hypotheses.append(
            {
                "id": identifier,
                "title": title,
                "label": "hypothesis",
                "confidence": round(max(0.01, min(0.99, float(confidence))), 3),
                "explanation": explanation,
                "evidence": evidence_items,
            }
        )

    if booted is True or category in {"booted_visible", "booted_with_warnings"}:
        return []

    registers = debug.get("registers") or {}
    register_map = registers.get("registers") if isinstance(registers, Mapping) else {}
    if not isinstance(register_map, Mapping):
        register_map = {}
    pc = register_map.get("pc")
    if pc is None and isinstance(debug.get("pc"), int):
        pc = debug.get("pc")
    pc_text = f"0x{int(pc):08x}" if isinstance(pc, int) else _fmt(pc, "unavailable")

    if exception or category == "exception":
        confidence = 0.9
        items = [f"exception: {exception or result.get('message') or 'session reported exception'}"]
        items.append(f"PC: {pc_text}")
        instructions = debug.get("disassembly") or []
        if isinstance(instructions, Mapping):
            instructions = instructions.get("instructions") or instructions.get("lines") or []
        if isinstance(instructions, list) and instructions:
            first = instructions[0]
            if isinstance(first, Mapping):
                first = first.get("text") or first.get("raw") or json.dumps(first, sort_keys=True)
            items.append(f"disassembly at PC: {first}")
        stack = debug.get("stack") or {}
        if isinstance(stack, Mapping):
            words = stack.get("hex_words") or stack.get("words") or []
            if words:
                items.append("stack top: " + " ".join(str(word) for word in words[:6]))
        add(
            "boot_crash",
            "Guest crash at boot (CPU exception)",
            0.92 if pc is not None else confidence,
            "The emulated CPU raised an exception.  The exact faulting instruction, "
            "register file and stack in this bundle are the evidence; the underlying "
            "cause (bad disk code, incompatible TOS or copy protection) is not proven.",
            items,
        )

    zip_members = source.get("zip_members")
    materialized = source.get("materialized") or source.get("materialized_path")
    if (evidence.get("kind") == "disk"):
        if category == "disk_prepare_failed":
            add(
                "missing_or_unreadable_disk",
                "Disk image is missing or unsupported",
                0.96,
                "The disk could not be materialized for Hatari.  This is an observed "
                "preparation failure, not a guess about the game code.",
                [
                    f"source: {_fmt(source.get('path') or game.get('path'))}",
                    f"materialized: {_fmt(materialized, 'not created')}",
                    f"error: {_fmt(result.get('message'))}",
                ],
            )
        elif isinstance(zip_members, list) and not zip_members:
            add(
                "missing_or_unreadable_disk",
                "Zip has no supported disk image",
                0.95,
                "The archive was readable but contains no .st/.msa/.stx/.dim/.ipf member.",
                [f"source: {_fmt(source.get('path'))}", "zip members: []"],
            )
        elif category in {"disk_error", "no_visible_frame", "blank_screen", "stalled_boot", "repeated_resets"}:
            disk_error = _disk_error_lines(lines)
            confidence = 0.9 if disk_error else 0.5
            items = [
                f"source: {_fmt(source.get('path') or game.get('path'))}",
                f"materialized: {_fmt(materialized, 'unknown')}",
                f"max non-black ratio: {max_ratio:.3f}",
            ]
            if disk_error:
                items.append(f"log: {disk_error[-1]}")
            if isinstance(zip_members, list):
                items.append(f"zip members: {len(zip_members)}")
            add(
                "missing_or_unreadable_disk",
                "Disk may be missing, non-bootable or corrupt",
                confidence,
                "The machine did not produce a usable game frame.  This can be caused "
                "by an absent/non-bootable image, a corrupt boot sector, or a disk that "
                "needs another drive/TOS.  The bundle records which image was actually used.",
                items,
            )

    if category in {"blank_screen", "no_visible_frame"}:
        capture_errors = [frame.get("capture_error") for frame in frames if frame.get("capture_error")]
        add(
            "blank_or_black_frame",
            "Black or blank display",
            0.82 if category == "blank_screen" else 0.72,
            "Hatari screenshots were captured but show essentially no non-black pixels, "
            "or no usable screenshot could be taken.  The emulator may be running fine "
            "with video output disabled, or the guest may be stuck in a blank state.",
            [
                f"frames: {len(frames)}",
                f"max non-black ratio: {max_ratio:.3f}",
                f"capture errors: {len(capture_errors)}",
                f"failure reason: {_fmt(result.get('failure_reason'))}",
            ],
        )

    if category in {"stalled_boot", "repeated_resets"}:
        resets = _reset_lines(lines)
        items = [
            f"failure reason: {_fmt(result.get('failure_reason'))}",
            f"max non-black ratio: {max_ratio:.3f}",
        ]
        if resets:
            items.append(f"reset lines: {len(resets)}; last: {resets[-1]}")
        add(
            "stalled_boot",
            "Boot stalled or reset loop",
            0.62,
            "The machine stayed alive but never produced a stable, changing frame within "
            "the timeout.  Repeated reset lines suggest a boot loop; without them the "
            "emulator may simply be waiting on an input or a slow loader.",
            items,
        )

    if category == "emulator_exit":
        add(
            "emulator_exit",
            "Emulator process exited during boot",
            0.66,
            "Hatari itself left the running state.  The exit reason and return code are "
            "recorded; this alone does not identify whether the guest or the host failed.",
            [
                f"state: {_fmt(status.get('state'))}",
                f"exit reason: {_fmt(status.get('exit_reason'))}",
                f"return code: {_fmt(status.get('returncode'))}",
                f"last error: {_fmt(status.get('last_error'))}",
            ],
        )

    if category == "debugger_prompt":
        add(
            "debugger_prompt",
            "Boot stopped at the Hatari debugger prompt",
            0.58,
            "The debugger was entered before a game frame was observed.  This may be an "
            "explicit breakpoint, an exception breakpoint, or a leftover debugger state.",
            [
                f"at_prompt: {status.get('at_prompt')}",
                f"last error: {_fmt(status.get('last_error'))}",
                f"failure reason: {_fmt(result.get('failure_reason'))}",
            ],
        )

    if category == "unresponsive":
        add(
            "emulator_unresponsive",
            "Emulator stopped responding",
            0.6,
            "Control-socket heartbeats stopped.  The guest may be spinning with interrupts "
            "disabled or the host process may be hung; no root cause is assumed.",
            [
                f"state: {_fmt(status.get('state'))}",
                f"heartbeat: {json.dumps(status.get('heartbeat'), sort_keys=True, default=str)}",
            ],
        )

    if category == "capture_failed":
        add(
            "capture_failure",
            "Screen capture failed",
            0.55,
            "Hatari could not produce screenshots repeatedly.  This is a host-side "
            "observation and does not by itself mean the guest failed to boot.",
            [f"failure reason: {_fmt(result.get('failure_reason'))}", f"error: {_fmt(result.get('message'))}"],
        )

    if category in {"console_error", "disk_error", "repeated_resets", "stalled_boot"}:
        fatal = _fatal_lines(lines)
        if fatal:
            add(
                "console_error",
                "Fatal-looking console or log output",
                0.55,
                "Hatari's log or the guest console contains error text.  The exact lines "
                "are quoted below; some games print harmless warnings, so this remains a "
                "hypothesis rather than a confirmed cause.",
                [f"log: {line}" for line in fatal[-5:]],
            )

    machine = launch.get("machine") or status.get("machine")
    tos = launch.get("tos") or evidence.get("tos")
    if machine or tos:
        if category in {"exception", "emulator_exit", "blank_screen", "no_visible_frame", "stalled_boot", "repeated_resets"}:
            add(
                "unsupported_tos_machine",
                "TOS/machine mismatch is possible",
                0.34 if category not in {"exception", "emulator_exit"} else 0.4,
                "The launch used a specific machine profile and TOS image.  A mismatch can "
                "produce a black screen, an early exception or a boot loop, but this "
                "evidence alone cannot prove it.  Try another TOS/machine profile to test "
                "the hypothesis.",
                [
                    f"machine: {_fmt(machine)}",
                    f"TOS: {_fmt(tos)}",
                    f"profile: {_fmt(launch.get('profile'))}",
                    f"classification: {category}",
                ],
            )

    resolution = result.get("resolution")
    if isinstance(resolution, (list, tuple)) and len(resolution) == 2:
        width, height = int(resolution[0]), int(resolution[1])
        ratio = width / height if height else 0.0
        if not (_MIN_WIDTH <= width <= _MAX_WIDTH and _MIN_HEIGHT <= height <= _MAX_HEIGHT) or not (1.0 <= ratio <= 2.0):
            add(
                "bad_resolution",
                "Unusual video resolution",
                0.38,
                "The captured frame has dimensions outside the expected ST/STE/TT/Falcon "
                "range.  This is a signal that the video mode may be misconfigured; it is "
                "not proof of a game bug.",
                [f"resolution: {width}x{height}", f"aspect ratio: {ratio:.3f}"],
            )

    if evidence.get("kind") == "prg":
        workspace = launch.get("workspace") or source.get("workspace")
        autostart = launch.get("autostart") or source.get("autostart")
        gemdos_lines = [line for line in lines if GEMDOS_RE.search(line) and not _noise(line)]
        if category not in {"booted_visible", "booted_with_warnings"}:
            confidence = 0.66 if gemdos_lines and any("error" in line.casefold() for line in gemdos_lines) else 0.42
            items = [
                f"PRG: {_fmt(source.get('path') or game.get('path'))}",
                f"workspace: {_fmt(workspace, 'managed by DebugSession')}",
                f"autostart: {_fmt(autostart, 'derived from workspace')}",
            ]
            if gemdos_lines:
                items.extend(f"log: {line}" for line in gemdos_lines[-4:])
            add(
                "bad_gemdos_config",
                "GEMDOS autostart/workspace configuration may be wrong",
                confidence,
                "PRG runs use a GEMDOS hard-disk workspace.  A wrong workspace path, "
                "case/time conversion or autostart path can fail before the program runs. "
                "The recorded launch configuration and GEMDOS lines are the evidence.",
                items,
            )

    if debug.get("errors"):
        add(
            "debugger_evidence_incomplete",
            "Some debugger evidence could not be collected",
            0.25,
            "The bundle is still valid, but one or more debugger reads failed while the "
            "emulator was running, stopped or at the prompt.",
            [str(item) for item in debug.get("errors", [])[:6]],
        )

    if not hypotheses:
        add(
            "undetermined",
            "No diagnostic signal matched",
            0.18,
            "The recorded evidence does not support a specific hypothesis.  Inspect the "
            "screenshots, log tails and debugger context before drawing conclusions.",
            [
                f"classification: {category}",
                f"failure reason: {_fmt(result.get('failure_reason'))}",
                f"message: {_fmt(result.get('message'))}",
            ],
        )

    # De-duplicate by id while keeping the highest-confidence instance.
    best: dict[str, dict[str, Any]] = {}
    for hypothesis in hypotheses:
        current = best.get(hypothesis["id"])
        if current is None or hypothesis["confidence"] > current["confidence"]:
            best[hypothesis["id"]] = hypothesis
    ranked = sorted(best.values(), key=lambda item: (-item["confidence"], item["id"]))
    return ranked


# ---------------------------------------------------------------------------
# Reports and bundles
# ---------------------------------------------------------------------------


def _frame_table(frames: Sequence[Mapping[str, Any]]) -> str:
    rows = ["| # | file | size | ratio | sha256 |", "| --- | --- | --- | --- | --- |"]
    for index, frame in enumerate(frames, 1):
        path = Path(str(frame.get("path") or ""))
        rows.append(
            "| {index} | {name} | {width}x{height} | {ratio} | `{digest}` |".format(
                index=index,
                name=path.name or "?",
                width=frame.get("width", "?"),
                height=frame.get("height", "?"),
                ratio=frame.get("nonblack_ratio", "?"),
                digest=str(frame.get("sha256") or "?")[:16],
            )
        )
    return "\n".join(rows)


def _fenced(lines: Sequence[str], limit: int = 120) -> str:
    text = "\n".join(str(line) for line in list(lines)[-limit:]) or "(empty)"
    return "```text\n" + text + "\n```"


def build_report(
    evidence: Mapping[str, Any],
    classification: Mapping[str, Any],
    hypotheses: Sequence[Mapping[str, Any]],
) -> str:
    result = evidence.get("result") or {}
    launch = evidence.get("launch") or {}
    game = evidence.get("game") or {}
    source = evidence.get("source") or {}
    status = evidence.get("status") or {}
    debug = evidence.get("debug") or {}
    frames = _frame_list(evidence)
    lines = log_lines(evidence, "stderr")
    console = log_lines(evidence, "console")
    lines_out = [
        "# Hatari Crash Detective report",
        "",
        "> Hypotheses are labelled and ranked. They are not confirmed root causes.",
        "",
        "## Summary",
        "",
        f"- Booted: **{classification.get('booted')}**",
        f"- Classification: **{classification.get('category')}** ({classification.get('confidence')})",
        f"- Summary: {classification.get('summary')}",
        f"- Result message: {result.get('message', '')}",
        f"- Failure reason: {result.get('failure_reason') or 'none'}",
        f"- Duration: {result.get('duration_ms', '?')} ms",
        "",
        "## Target",
        "",
        f"- Kind: {evidence.get('kind', 'unknown')}",
        f"- Game: {game.get('title') or game.get('filename') or source.get('path') or 'unknown'}",
        f"- Game ID: {game.get('id') or 'unknown'}",
        f"- Source: {source.get('path') or game.get('path') or 'unknown'}",
        f"- Materialized disk: {source.get('materialized') or source.get('materialized_path') or 'n/a'}",
        f"- Zip members: {source.get('zip_members', 'n/a')}",
        "",
        "## Launch configuration",
        "",
        f"- Machine: {launch.get('machine', '?')}",
        f"- Profile: {launch.get('profile', '?')}",
        f"- TOS: {launch.get('tos', '?')}",
        f"- Args: {launch.get('args', [])}",
        f"- Timeout: {launch.get('timeout', '?')} s",
        f"- Debug exceptions: {launch.get('debug_exceptions')}",
        f"- Workspace: {launch.get('workspace') or source.get('workspace') or 'n/a'}",
        "",
        "## Final emulator status",
        "",
        "```json",
        json.dumps(status, indent=2, sort_keys=True, default=str),
        "```",
        "",
        "## Classification signals",
        "",
    ]
    for signal in classification.get("signals", []):
        lines_out.append(
            f"- `{signal.get('code')}` ({signal.get('weight')}): {signal.get('label')} — {signal.get('detail')}"
        )
    lines_out += [
        "",
        "## Hypotheses (not confirmed)",
        "",
    ]
    if hypotheses:
        for index, hypothesis in enumerate(hypotheses, 1):
            lines_out.append(
                f"### {index}. {hypothesis.get('title')} — confidence {hypothesis.get('confidence')}"
            )
            lines_out.append("")
            lines_out.append(str(hypothesis.get("explanation") or ""))
            lines_out.append("")
            lines_out.append("Evidence:")
            for item in hypothesis.get("evidence", []):
                lines_out.append(f"- {item}")
            lines_out.append("")
    else:
        lines_out.append("(none: the run is classified as booted)")
    lines_out += [
        "## Screenshots",
        "",
        _frame_table(frames),
        "",
        "## Debugger context",
        "",
        "```json",
        json.dumps(debug, indent=2, sort_keys=True, default=str)[:24000],
        "```",
        "",
        "## Hatari log tail",
        "",
        _fenced(lines),
        "",
        "## Guest console tail",
        "",
        _fenced(console),
        "",
        "## Evidence files",
        "",
        "The complete machine-readable evidence is in `report.json`; checksums are in",
        "`manifest.json`.  Screenshots are copied into `screenshots/`, log tails into",
        "`logs/` and debugger output into `debug/`.",
        "",
    ]
    return "\n".join(lines_out)


def _collect_screenshots(bundle: Path, evidence: Mapping[str, Any]) -> list[dict[str, Any]]:
    screenshots_dir = bundle / "screenshots"
    copied: list[dict[str, Any]] = []
    seen: set[str] = set()
    frames = _frame_list(evidence)
    result = evidence.get("result") or {}
    if isinstance(result, Mapping) and result.get("frame_path"):
        frames.append({"path": result.get("frame_path")})
    for frame in frames:
        raw_path = frame.get("path")
        if not raw_path:
            continue
        source = Path(str(raw_path)).expanduser()
        if not source.is_file():
            continue
        key = str(source.resolve())
        if key in seen:
            continue
        seen.add(key)
        suffix = source.suffix or ".bmp"
        target = screenshots_dir / f"frame-{len(copied) + 1:03d}{suffix}"
        screenshots_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        info = dict(frame)
        info["path"] = str(target)
        info["source_path"] = str(source)
        info["bundle_path"] = str(target.relative_to(bundle))
        info["size"] = target.stat().st_size
        info["sha256"] = sha256_file(target)
        try:
            stats = bmp_stats(target)
            info.setdefault("width", stats["width"])
            info.setdefault("height", stats["height"])
            info.setdefault("nonblack_ratio", stats["nonblack_ratio"])
        except (OSError, ValueError, struct.error):
            pass
        copied.append(info)
    return copied


def _manifest_files(bundle: Path) -> list[dict[str, Any]]:
    files: list[dict[str, Any]] = []
    for path in sorted(bundle.rglob("*")):
        if not path.is_file() or path.name == "manifest.json":
            continue
        relative = path.relative_to(bundle).as_posix()
        files.append(
            {
                "path": relative,
                "size": path.stat().st_size,
                "sha256": sha256_file(path),
            }
        )
    return files


def build_manifest(bundle: str | Path, metadata: Mapping[str, Any] | None = None) -> dict[str, Any]:
    root = Path(bundle).expanduser()
    return {
        "schema": SCHEMA_VERSION,
        "bundle": str(root),
        "created_at": int(time.time()),
        "metadata": _jsonable(dict(metadata or {})),
        "files": _manifest_files(root),
    }


def write_bundle(
    state_dir: str | Path,
    evidence: Mapping[str, Any],
    classification: Mapping[str, Any],
    hypotheses: Sequence[Mapping[str, Any]],
    *,
    run_id: str | None = None,
) -> Path:
    """Write a self-contained triage bundle and return its directory."""
    state_root = Path(state_dir).expanduser().resolve()
    identifier = run_id or str(evidence.get("run_id") or f"run-{int(time.time() * 1000)}")
    bundle = state_root / "runs" / identifier / "bundle"
    bundle.mkdir(parents=True, exist_ok=True)
    (bundle / "logs").mkdir(parents=True, exist_ok=True)
    (bundle / "debug").mkdir(parents=True, exist_ok=True)
    (bundle / "screenshots").mkdir(parents=True, exist_ok=True)

    screenshots = _collect_screenshots(bundle, evidence)
    logs = evidence.get("logs") or {}
    stderr_lines = _entry_texts(logs.get("stderr")) if isinstance(logs, Mapping) else []
    console_lines = _entry_texts(logs.get("console")) if isinstance(logs, Mapping) else []
    _atomic_write_bytes(
        bundle / "logs" / "stderr.log",
        ("\n".join(stderr_lines) + ("\n" if stderr_lines else "")).encode("utf-8"),
    )
    _atomic_write_bytes(
        bundle / "logs" / "console.log",
        ("\n".join(console_lines) + ("\n" if console_lines else "")).encode("utf-8"),
    )

    debug = evidence.get("debug") or {}
    if not isinstance(debug, Mapping):
        debug = {"value": debug}
    config_module.atomic_write_json(bundle / "debug" / "debug.json", _jsonable(debug))
    disassembly = debug.get("disassembly") or []
    if isinstance(disassembly, Mapping):
        disassembly = disassembly.get("instructions") or disassembly.get("lines") or []
    if isinstance(disassembly, list):
        rendered = []
        for item in disassembly:
            if isinstance(item, Mapping):
                rendered.append(str(item.get("text") or item.get("raw") or json.dumps(item, sort_keys=True)))
            else:
                rendered.append(str(item))
        _atomic_write_bytes(
            bundle / "debug" / "disassembly.txt",
            ("\n".join(rendered) + ("\n" if rendered else "")).encode("utf-8"),
        )
    backtrace = debug.get("backtrace") or {}
    if isinstance(backtrace, Mapping):
        backtrace_lines = backtrace.get("lines") or []
        if backtrace_lines:
            _atomic_write_bytes(
                bundle / "debug" / "backtrace.txt",
                ("\n".join(str(line) for line in backtrace_lines) + "\n").encode("utf-8"),
            )

    payload = _jsonable(dict(evidence))
    payload["schema"] = SCHEMA_VERSION
    payload["classification"] = _jsonable(dict(classification))
    payload["hypotheses"] = _jsonable(list(hypotheses))
    payload["bundle"] = {
        "path": str(bundle),
        "run_id": identifier,
        "screenshots": screenshots,
        "generated_at": int(time.time()),
    }
    config_module.atomic_write_json(bundle / "report.json", payload)
    _atomic_write_bytes(
        bundle / "report.md",
        build_report(evidence, classification, hypotheses).encode("utf-8"),
    )
    _atomic_write_bytes(
        bundle / "README.txt",
        (
            "Hatari Crash Detective triage bundle\n"
            "===================================\n\n"
            "report.md    human-readable report with ranked hypotheses\n"
            "report.json  complete machine-readable evidence\n"
            "screenshots/ captured frames (copied; hashes are in report.json)\n"
            "logs/        stderr and guest console tails\n"
            "debug/       registers, PC, disassembly, stack and backtrace context\n"
            "manifest.json sizes and SHA-256 checksums for every file above\n\n"
            "Hypotheses are evidence-based suggestions, not confirmed root causes.\n"
        ).encode("utf-8"),
    )
    manifest = build_manifest(
        bundle,
        {
            "run_id": identifier,
            "game_id": _game_id(evidence),
            "classification": classification.get("category"),
            "booted": classification.get("booted"),
            "confidence": classification.get("confidence"),
        },
    )
    config_module.atomic_write_json(bundle / "manifest.json", manifest)
    return bundle


def verify_bundle(bundle: str | Path) -> dict[str, Any]:
    root = Path(bundle).expanduser()
    manifest_path = root / "manifest.json"
    if not manifest_path.is_file():
        return {"ok": False, "errors": ["manifest.json is missing"], "files": 0}
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return {"ok": False, "errors": [f"manifest unreadable: {exc}"], "files": 0}
    errors: list[str] = []
    listed: set[str] = set()
    for item in manifest.get("files", []):
        if not isinstance(item, Mapping):
            continue
        relative = str(item.get("path") or "")
        listed.add(relative)
        path = root / relative
        if not path.is_file():
            errors.append(f"missing: {relative}")
            continue
        if int(item.get("size", -1)) != path.stat().st_size:
            errors.append(f"size mismatch: {relative}")
        digest = sha256_file(path)
        if digest != item.get("sha256"):
            errors.append(f"checksum mismatch: {relative}")
    actual = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and path.name != "manifest.json"
    }
    for extra in sorted(actual - listed):
        errors.append(f"not in manifest: {extra}")
    return {"ok": not errors, "errors": errors, "files": len(listed)}


# ---------------------------------------------------------------------------
# User outcomes
# ---------------------------------------------------------------------------

_OUTCOME_LOCK = threading.RLock()


def outcomes_path(state_dir: str | Path) -> Path:
    return Path(state_dir).expanduser().resolve() / "outcomes.json"


def load_outcomes(state_dir: str | Path) -> dict[str, Any]:
    path = outcomes_path(state_dir)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return {"version": 1, "runs": {}}
    if not isinstance(value, dict) or not isinstance(value.get("runs"), dict):
        return {"version": 1, "runs": {}}
    return value


def record_outcome(
    state_dir: str | Path,
    run_id: str,
    game_id: str,
    outcome: str,
    *,
    title: str = "",
    note: str = "",
    bundle: str | None = None,
    detected: str | None = None,
) -> dict[str, Any]:
    """Persist a user's success/failure mark for one run."""
    if outcome not in {"success", "failure"}:
        raise ValueError("outcome must be 'success' or 'failure'")
    entry = {
        "run_id": str(run_id),
        "game_id": str(game_id),
        "title": str(title),
        "outcome": outcome,
        "note": str(note),
        "bundle": str(bundle) if bundle else None,
        "detected": detected,
        "marked_at": int(time.time()),
    }
    with _OUTCOME_LOCK:
        data = load_outcomes(state_dir)
        data["version"] = 1
        runs = data.setdefault("runs", {})
        runs[str(run_id)] = entry
        config_module.atomic_write_json(outcomes_path(state_dir), data)
    return entry
