"""Run the cockpit with the examples directory importable as ``common``."""

from __future__ import annotations

import sys
from pathlib import Path

PROGRAMS = Path(__file__).resolve().parent.parent
if str(PROGRAMS) not in sys.path:
    sys.path.insert(0, str(PROGRAMS))

from .app import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
