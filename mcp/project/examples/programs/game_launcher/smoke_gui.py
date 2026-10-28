"""Headless GUI smoke: construct the window, load the catalog, exit via after()."""

from __future__ import annotations

import argparse
import sys
import time
import tkinter as tk
from pathlib import Path

_PROGRAMS = Path(__file__).resolve().parent.parent
if str(_PROGRAMS) not in sys.path:
    sys.path.insert(0, str(_PROGRAMS))

from common import Config  # noqa: E402

from .app import GameLauncherApp  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--games", action="append", metavar="DIR")
    parser.add_argument("--config")
    parser.add_argument("--state")
    parser.add_argument("--timeout", type=float, default=8.0)
    parser.add_argument("--min-entries", type=int, default=1)
    parser.add_argument("--page-size", type=int, default=25)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    state_dir = Path(args.state).expanduser() if args.state else None
    config = Config(args.config)
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        print(f"gui_smoke: no display: {exc}", file=sys.stderr)
        return 2

    app = GameLauncherApp(
        root,
        roots=args.games,
        config=config,
        state_dir=state_dir,
        page_size=args.page_size,
    )
    started = time.monotonic()
    result = {"code": 1, "printed": False}

    def finish() -> None:
        if result["printed"]:
            return
        result["printed"] = True
        app._refresh_list()
        entries = len(app.catalog)
        scanned = app.catalog.scanned_files
        ready = app.catalog.ready
        children = len(app.tree.get_children())
        selected = None
        if children:
            first = app.tree.get_children()[0]
            app.tree.selection_set(first)
            app.tree.focus(first)
            app._on_tree_select()
            selected = app.title_var.get()
        print(
            "gui_smoke: "
            f"entries={entries} scanned={scanned} ready={ready} "
            f"tree_rows={children} selected={selected!r} "
            f"roots={len(app.catalog.roots)}",
            flush=True,
        )
        result["code"] = 0 if entries >= args.min_entries else 1

    def check() -> None:
        if result["printed"]:
            return
        elapsed = time.monotonic() - started
        if app.catalog.scanned_files > 0 or app.catalog.ready or elapsed >= args.timeout:
            finish()
            app.close_now()
            return
        root.after(100, check)

    root.after(100, check)
    try:
        root.mainloop()
    finally:
        if not result["printed"]:
            finish()
        if app._closing is False:
            app.close_now()
    return int(result["code"])


if __name__ == "__main__":
    raise SystemExit(main())
