#!/usr/bin/env python3
"""Build the bundled Atari sample target with the MCP AHCC tool."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROGRAMS = Path(__file__).resolve().parents[1]
if str(PROGRAMS) not in sys.path:
    sys.path.insert(0, str(PROGRAMS))

from common.mcp_client import MCPClient  # noqa: E402
from time_travel_debugger.sample import SAMPLE_SOURCE, build_sample  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true", help="rebuild even if a PRG exists")
    args = parser.parse_args(argv)
    with MCPClient() as client:
        try:
            output = build_sample(
                client,
                force=args.force,
                log=lambda message: print(message, flush=True),
            )
        except BaseException as exc:  # noqa: BLE001 - CLI boundary
            print(f"FAIL: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 1
    print(f"source: {SAMPLE_SOURCE}")
    print(f"output: {output} ({output.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
