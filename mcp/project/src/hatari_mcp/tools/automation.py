"""Scenario automation tools (P6).

The heavy lifting lives in :mod:`hatari_mcp.services.scenario`; these tools are
thin, bounded wrappers so a model can validate and run a scenario without
shelling out.  A run spawns its own MCP stdio server per case, exactly like the
CLI does, and is always bounded by ``timeout_ms``.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from ..app import App
from ..corpus import discover_corpus
from ..errors import error
from ..services.scenario import (
    EXIT_OK,
    ScenarioEngine,
    replay_bundle,
    validate_scenario,
)
from ._common import guarded


def register(server: MCPServer, app: App) -> None:
    @server.tool(
        name="scenario_validate",
        description=(
            "Validate a JSON/YAML scenario without running Hatari. Checks schema v1, unknown keys, "
            "duplicate step ids, ${VAR} declarations, path confinement, unsafe Hatari arguments, "
            "matrix size and deterministic case ids."
        ),
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False),
    )
    @guarded
    async def scenario_validate(path: str, max_cases: int | None = None) -> dict[str, Any]:
        target = app.guard_path(path, must_exist=True)
        return await asyncio.to_thread(validate_scenario, target, max_cases=max_cases)

    @server.tool(
        name="scenario_run",
        description=(
            "Run a scenario and its matrix over the MCP stdio server. Writes an atomic artifact bundle "
            "with manifest/checksums, report.json and junit.xml, then returns the aggregate report. "
            "Bounded by timeout_ms; teardown always runs."
        ),
        annotations=ToolAnnotations(destructiveHint=True, openWorldHint=False),
    )
    @guarded
    async def scenario_run(
        path: str,
        artifacts: str | None = None,
        max_cases: int | None = 32,
        fail_fast: bool = True,
        jobs: int = 1,
        timeout_ms: int = 120_000,
        record: bool = False,
    ) -> dict[str, Any]:
        target = app.guard_path(path, must_exist=True)
        if timeout_ms < 1000 or timeout_ms > 3_600_000:
            raise error("bad_option", "timeout_ms must be between 1000 and 3600000.", "Use a bounded timeout.")
        if jobs < 1 or jobs > 8:
            raise error("bad_option", "jobs must be between 1 and 8.", "Use a small worker count.")
        artifacts_root = (
            Path(artifacts).expanduser().resolve()
            if artifacts
            else Path(tempfile.gettempdir()) / "hatari-mcp-artifacts"
        )
        engine = ScenarioEngine(
            max_cases=max_cases,
            fail_fast=fail_fast,
            jobs=jobs,
            record=record,
            environment=os.environ,
        )
        try:
            result = await asyncio.wait_for(engine.run(target, artifacts_root=artifacts_root), timeout_ms / 1000.0)
        except asyncio.TimeoutError:
            raise error(
                "timeout",
                f"Scenario run exceeded {timeout_ms} ms.",
                "Lower the scenario timeout or split the matrix; teardown is attempted on cancellation.",
            ) from None
        return {
            **result.report,
            "artifacts": str(result.artifacts),
            "exit_code": result.exit_code,
            "ok": result.exit_code == EXIT_OK,
        }

    @server.tool(
        name="scenario_list_corpus",
        description=(
            "Discover Okami-Shell, FreeMiNT and GodLib under their env/default roots. Reports missing "
            "prerequisites and skip reasons; never copies or writes into the corpus."
        ),
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False),
    )
    @guarded
    async def scenario_list_corpus() -> dict[str, Any]:
        return discover_corpus(os.environ)

    @server.tool(
        name="scenario_replay",
        description=(
            "Replay a recorded replay/ bundle and compare the final decoded-RGBA screen checkpoint. "
            "Fidelity is always reported as approximate (no frame-locked input without P7.4/P7.5)."
        ),
        annotations=ToolAnnotations(destructiveHint=True, openWorldHint=False),
    )
    @guarded
    async def scenario_replay(
        bundle: str,
        artifacts: str | None = None,
        timeout_ms: int = 120_000,
    ) -> dict[str, Any]:
        if timeout_ms < 1000 or timeout_ms > 3_600_000:
            raise error("bad_option", "timeout_ms must be between 1000 and 3600000.", "Use a bounded timeout.")
        target = app.guard_path(bundle, must_exist=True)
        if not target.is_dir():
            raise error("invalid_path", f"Replay bundle is not a directory: {target}", "Pass a run/case/attempt directory.")
        artifacts_root = (
            Path(artifacts).expanduser().resolve()
            if artifacts
            else Path(tempfile.gettempdir()) / "hatari-mcp-replays"
        )
        result = await asyncio.wait_for(
            replay_bundle(target, artifacts_root=artifacts_root, timeout_ms=timeout_ms),
            timeout_ms / 1000.0,
        )
        return result
