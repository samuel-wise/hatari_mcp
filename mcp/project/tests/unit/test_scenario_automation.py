from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from hatari_mcp.tools import automation


class FakeToolServer:
    def __init__(self) -> None:
        self.tools: dict[str, Any] = {}

    def tool(self, **kwargs: Any):
        def decorator(function):
            self.tools[kwargs["name"]] = function
            return function

        return decorator


class FakeApp:
    def __init__(self, root: Path) -> None:
        self.root = root

    def guard_path(self, path: str | Path, *, must_exist: bool = False) -> Path:
        target = Path(path).expanduser().resolve()
        if must_exist and not target.exists():
            raise FileNotFoundError(str(target))
        return target


def test_automation_tools_register_and_validate(tmp_path):
    scenario = tmp_path / "scenario.json"
    scenario.write_text(
        json.dumps(
            {
                "version": 1,
                "id": "automation",
                "name": "Automation",
                "steps": [{"id": "status", "tool": "emu_status", "args": {}}],
            }
        ),
        encoding="utf-8",
    )
    server = FakeToolServer()
    automation.register(server, FakeApp(tmp_path))

    assert set(server.tools) == {
        "scenario_validate",
        "scenario_run",
        "scenario_list_corpus",
        "scenario_replay",
    }

    result = asyncio.run(server.tools["scenario_validate"](str(scenario)))
    assert result["ok"] is True
    assert result["cases"] == 1

    corpus = asyncio.run(server.tools["scenario_list_corpus"]())
    assert corpus["read_only"] is True
