#!/usr/bin/env python3
"""Prove the synchronous MCP wrapper with a real headless EmuTOS boot.

Usage:
    mcp/.venv/bin/python mcp/project/examples/programs/common/smoke.py
    python -m common.smoke          # from examples/programs
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

try:  # package import
    from . import paths
    from .game_runner import GameSession
    from .mcp_client import MCPClient
except ImportError:  # direct script import
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import paths  # type: ignore[no-redef]
    from game_runner import GameSession  # type: ignore[no-redef]
    from mcp_client import MCPClient  # type: ignore[no-redef]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tos", help="TOS/EmuTOS image (default: discovered EmuTOS)")
    parser.add_argument("--machine", default="st", choices=["st", "ste", "tt", "falcon"])
    parser.add_argument("--timeout", type=float, default=45.0, help="boot timeout in seconds")
    parser.add_argument(
        "--frame",
        default=str(Path(tempfile.gettempdir()) / "hatari_mcp_examples_smoke.bmp"),
        help="where to write the proof frame",
    )
    args = parser.parse_args(argv)

    tos = Path(args.tos).expanduser() if args.tos else paths.find_tos("emutos")
    if tos is None or not tos.is_file():
        print("FAIL: no EmuTOS/TOS image found (pass --tos or set HATARI_TOS)", file=sys.stderr)
        return 2

    frame = Path(args.frame).expanduser()
    frame.unlink(missing_ok=True)
    result = None
    try:
        with MCPClient() as client:
            tools = client.list_tools()
            print(f"tools: {len(tools)}")
            session = GameSession(client)
            result = session.start_emutos(
                machine=args.machine, tos=tos, timeout=args.timeout
            )
            print(f"boot: {'ok' if result.ok else 'failed'} ({result.status})")
            print(f"message: {result.message}")
            if result.evidence.get("status"):
                status = result.evidence["status"]
                print(f"emulator_state: {status.get('state')} pid={status.get('pid')}")
            stored = result.frame_path
            print(f"detected_frame: {stored or '-'}")
            if result.resolution:
                print(f"resolution: {result.resolution[0]}x{result.resolution[1]}")
            capture = client.call_tool(
                "screen_frame_capture", path=str(frame), format="bmp", timeout=20.0
            )
            print(f"frame: {capture.get('path')} {capture.get('width')}x{capture.get('height')}")
            status = client.call_tool("emu_status", timeout=10.0)
            print(f"status: {status.get('state')}")
            stop = session.stop()
            print(f"stop: {stop.get('state', stop.get('error', 'unknown'))}")
        return 0 if result is not None and result.ok and frame.is_file() else 1
    except BaseException as exc:  # noqa: BLE001 - CLI boundary
        print(f"FAIL: {type(exc).__name__}: {exc}", file=sys.stderr)
        if result is not None and result.evidence:
            print(json.dumps(result.evidence, indent=2, default=str)[:4000], file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
