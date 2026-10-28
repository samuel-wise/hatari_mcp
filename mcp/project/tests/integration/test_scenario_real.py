from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from hatari_mcp.config import Settings
from hatari_mcp.services.scenario import ScenarioEngine
from hatari_mcp.tools import frames

SCENARIO = Path(__file__).resolve().parents[2] / "examples" / "scenarios" / "boot-capture-stop.json"


def test_real_boot_capture_stop_scenario(tmp_path):
    if not SCENARIO.is_file():
        pytest.skip("scenario example is missing")
    settings = Settings.load()
    if not settings.hatari_bin or not settings.hatari_bin.is_file():
        pytest.skip("Hatari binary is not available")
    if not settings.default_tos or not settings.default_tos.is_file():
        pytest.skip("EmuTOS/TOS image is not available")

    environment = dict(os.environ)
    environment["HATARI_BIN"] = str(settings.hatari_bin)
    environment["HATARI_TOS"] = str(settings.default_tos)
    run_id = f"integration-{uuid.uuid4().hex[:8]}"
    engine = ScenarioEngine(environment=environment, run_id=run_id, fail_fast=True)

    start = time.monotonic()
    result = asyncio.run(
        asyncio.wait_for(
            engine.run(SCENARIO, artifacts_root=tmp_path / "artifacts"),
            timeout=55,
        )
    )
    elapsed = time.monotonic() - start

    assert elapsed < 60
    assert result.exit_code == 0, result.report
    assert result.report["summary"]["passed"] == 1
    case = result.cases[0]
    assert case["status"] == "passed"
    assert case["failure"] is None

    bundle = Path(result.artifacts) / case["artifacts"]
    assert (bundle / ".complete").is_file()
    manifest = json.loads((bundle / "manifest.json").read_text())
    assert manifest["status"] == "passed"
    assert manifest["scenario"] == "boot-capture-stop"
    assert manifest["determinism"]["mode"] == "tolerant"
    assert manifest["hatari_version"]

    screenshot = bundle / "screenshots" / "desktop.bmp"
    assert screenshot.is_file()
    image = frames.decode_bmp(screenshot.read_bytes())
    step_hashes = [
        step["result"]["rgba_sha256"]
        for step in manifest["steps"]
        if step.get("result", {}).get("rgba_sha256")
    ]
    assert image.sha256() in step_hashes

    for artifact in manifest["artifacts"]:
        target = bundle / artifact["path"]
        assert target.is_file()
        assert hashlib.sha256(target.read_bytes()).hexdigest() == artifact["sha256"]

    junit = ET.parse(result.artifacts / "junit.xml").getroot()
    assert junit.attrib["tests"] == "1"
    assert junit.attrib["failures"] == "0"

    # The emulator process and its private runtime directory must be gone.
    start_step = next(step for step in manifest["steps"] if step["id"] == "start")
    pid = start_step["result"]["pid"]
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    socket_path = Path(start_step["result"]["socket"])
    assert not socket_path.parent.exists()
