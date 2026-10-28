import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
import zipfile
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parents[2]


def test_labs_package_is_inert_without_guard():
    env = os.environ.copy()
    env.pop("HATARI_MCP_LABS", None)
    code = "import labs; print(labs.describe()['shipping'], labs.describe()['registered_mcp_tools'])"
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=PROJECT,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert proc.stdout.strip() == "False 0"


def test_labs_submodule_requires_guard():
    env = os.environ.copy()
    env.pop("HATARI_MCP_LABS", None)
    proc = subprocess.run(
        [sys.executable, "-c", "import labs.harness"],
        cwd=PROJECT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode != 0
    assert "HATARI_MCP_LABS=1" in proc.stderr


def test_packaging_source_is_src_only():
    data = tomllib.loads((PROJECT / "pyproject.toml").read_text(encoding="utf-8"))
    where = data["tool"]["setuptools"]["packages"]["find"]["where"]
    assert where == ["src"]
    assert (PROJECT / "labs").is_dir()
    assert not str((PROJECT / "labs").relative_to(PROJECT)).startswith("src")


def _tool_names() -> set[str]:
    sys.path.insert(0, str(PROJECT / "src"))
    try:
        from hatari_mcp.server import server
    finally:
        sys.path.pop(0)
    manager = getattr(server, "_tool_manager", None)
    if manager is not None and hasattr(manager, "_tools"):
        return set(manager._tools)
    if hasattr(server, "tools"):
        return set(server.tools)
    raise AssertionError("cannot enumerate MCP server tools with this mcp version")


def test_lab_import_does_not_register_or_change_server_tools():
    before = _tool_names()
    assert before
    assert not any("lab" in name.lower() for name in before)
    os.environ["HATARI_MCP_LABS"] = "1"
    import labs.harness  # noqa: F401
    after = _tool_names()
    assert after == before


def _wheel_builder() -> str | None:
    candidates = [sys.executable, shutil.which("python3"), shutil.which("python")]
    for candidate in candidates:
        if not candidate:
            continue
        proc = subprocess.run(
            [candidate, "-c", "import setuptools, wheel"],
            capture_output=True,
            text=True,
        )
        if proc.returncode == 0:
            return candidate
    return None


def test_wheel_excludes_labs_and_keeps_server():
    builder = _wheel_builder()
    if builder is None:
        pytest.skip("no Python with setuptools and wheel available to build the package")
    with tempfile.TemporaryDirectory(prefix="p8-wheel-") as tmp:
        workspace = Path(tmp) / "project"
        workspace.mkdir()
        shutil.copy2(PROJECT / "pyproject.toml", workspace / "pyproject.toml")
        shutil.copytree(
            PROJECT / "src",
            workspace / "src",
            ignore=shutil.ignore_patterns("__pycache__", "*.egg-info"),
        )
        shutil.copytree(
            PROJECT / "labs",
            workspace / "labs",
            ignore=shutil.ignore_patterns("__pycache__"),
        )
        wheel_dir = Path(tmp) / "wheel"
        wheel_dir.mkdir()
        proc = subprocess.run(
            [
                builder,
                "-m",
                "pip",
                "wheel",
                ".",
                "--no-deps",
                "--no-build-isolation",
                "--wheel-dir",
                str(wheel_dir),
            ],
            cwd=workspace,
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0, proc.stderr[-2000:]
        wheels = list(wheel_dir.glob("*.whl"))
        assert len(wheels) == 1
        names = zipfile.ZipFile(wheels[0]).namelist()
    assert "hatari_mcp/server.py" in names
    assert not any(name.startswith("labs/") or "/labs/" in name for name in names)
