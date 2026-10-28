"""Program 2c: Hatari game crash detective.

The package separates pure evidence logic (``analysis``), catalog helpers
(``discovery``), the blocking MCP workflow (``controller``) and the Tk UI
(``app``).  Importing the package does not create a Tk root or start an MCP
server.
"""

from __future__ import annotations

from .analysis import (
    bmp_stats,
    bmp_to_ppm_bytes,
    build_manifest,
    build_report,
    classify,
    diagnose,
    load_outcomes,
    record_outcome,
    verify_bundle,
    write_bundle,
)
from .controller import CrashDetective, default_state_dir, resolve_state_dir
from .discovery import PrgTarget, filter_entries, prg_target

__all__ = [
    "CrashDetective",
    "PrgTarget",
    "bmp_stats",
    "bmp_to_ppm_bytes",
    "build_manifest",
    "build_report",
    "classify",
    "default_state_dir",
    "diagnose",
    "filter_entries",
    "load_outcomes",
    "prg_target",
    "record_outcome",
    "resolve_state_dir",
    "verify_bundle",
    "write_bundle",
]
