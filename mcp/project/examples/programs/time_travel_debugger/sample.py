"""Locate or build the bundled Atari sample target through MCP AHCC."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

SAMPLE_DIR = Path(__file__).resolve().parent / "sample"
SAMPLE_SOURCE = SAMPLE_DIR / "tt_sample.c"
SAMPLE_OUTPUT = SAMPLE_DIR / "tt_sample.prg"


class SampleBuildError(RuntimeError):
    pass


def sample_source() -> Path:
    return SAMPLE_SOURCE


def existing_sample() -> Path | None:
    for candidate in (SAMPLE_OUTPUT, SAMPLE_DIR / "tt_sample.tos", SAMPLE_DIR / "TT_SAMPLE.PRG"):
        if candidate.is_file():
            return candidate
    return None


def build_sample(
    client: Any,
    *,
    force: bool = False,
    timeout_ms: int = 240000,
    log: Callable[[str], None] | None = None,
) -> Path:
    """Return the sample PRG, compiling it with the MCP AHCC tool if needed."""
    if not SAMPLE_SOURCE.is_file():
        raise SampleBuildError(f"sample source is missing: {SAMPLE_SOURCE}")
    if not force:
        existing = existing_sample()
        if existing is not None:
            return existing

    def emit(message: str) -> None:
        if log is not None:
            log(message)

    status = client.call_tool("ahcc_status", timeout=30.0)
    if not status.get("available"):
        raise SampleBuildError(
            "AHCC is not available; set HATARI_AHCC_DIR or install the "
            "gitignored toolchain, then retry"
        )
    emit("compiling bundled sample with MCP AHCC...")
    if SAMPLE_OUTPUT.exists() and force:
        SAMPLE_OUTPUT.unlink()
    result = client.call_tool(
        "ahcc_compile",
        source=str(SAMPLE_SOURCE),
        output=str(SAMPLE_OUTPUT),
        timeout_ms=timeout_ms,
        timeout=(timeout_ms / 1000.0) + 20.0,
    )
    output = Path(str(result.get("output") or SAMPLE_OUTPUT))
    if not output.is_file():
        raise SampleBuildError(
            f"AHCC reported success but produced no file: {output}"
        )
    emit(f"sample built: {output} ({output.stat().st_size} bytes)")
    return output
