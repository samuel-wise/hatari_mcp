"""Private short-path runtime directories and generated Hatari config."""

from __future__ import annotations

import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

CONFIG_TEXT = """[Log]
nTextLogLevel = 3
nAlertDlgLogLevel = 1
bConfirmQuit = FALSE
bConsoleWindow = FALSE

[Debugger]
nNumberBase = 16
nSymbolLines = 100000
nMemdumpLines = 100000
nFindLines = 100000
nDisasmLines = 100000
nBacktraceLines = 1000
nDisasmOptions = 103
"""


@dataclass
class RuntimeDir:
    path: Path
    socket_path: Path
    config_path: Path

    def cleanup(self) -> None:
        try:
            self.socket_path.unlink(missing_ok=True)
        except OSError:
            pass
        shutil.rmtree(self.path, ignore_errors=True)


def _short_dir(root: Path) -> Path:
    path = Path(tempfile.mkdtemp(prefix="hmcp-", dir=str(root)))
    os.chmod(path, 0o700)
    return path


def create_runtime_dir(root: Path = Path("/tmp")) -> RuntimeDir:
    path = _short_dir(root)
    socket_path = path / "control.sock"
    # Linux sun_path is 108 bytes including the terminating NUL.
    if len(os.fsencode(socket_path)) >= 108:
        shutil.rmtree(path, ignore_errors=True)
        path = _short_dir(Path("/tmp"))
        socket_path = path / "control.sock"
    config_path = path / "hatari.cfg"
    config_path.write_text(CONFIG_TEXT, encoding="utf-8")
    return RuntimeDir(path=path, socket_path=socket_path, config_path=config_path)
