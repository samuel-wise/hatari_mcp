#!/usr/bin/env bash
# Run the Atari ST game launcher against the project virtualenv.
set -euo pipefail

here="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
programs="$(cd -- "$here/.." && pwd)"
repo="$(cd -- "$programs/../../../.." && pwd)"
python="${HATARI_MCP_PYTHON:-$repo/mcp/.venv/bin/python}"

if [[ ! -x "$python" ]]; then
    printf 'Python virtualenv not found: %s\n' "$python" >&2
    printf 'Run the MCP project setup first or set HATARI_MCP_PYTHON.\n' >&2
    exit 1
fi

if [[ -z "${DISPLAY:-}" ]]; then
    printf 'DISPLAY is not set. Start TigerVNC (or another X server), or run:\n' >&2
    printf '  xvfb-run -a %q\n' "$0" >&2
    exit 1
fi

export PYTHONPATH="$programs${PYTHONPATH:+:$PYTHONPATH}"
export HATARI_MCP_REPO="${HATARI_MCP_REPO:-$repo}"
exec "$python" -m game_launcher "$@"
