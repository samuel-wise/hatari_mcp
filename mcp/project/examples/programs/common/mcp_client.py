"""Synchronous MCP stdio client used by the Tkinter example programs.

The MCP SDK is asyncio-first. This wrapper owns one asyncio event loop in a
daemon thread and one long-lived coroutine that opens and closes the stdio
session. Keeping connect, calls and close in that single task avoids AnyIO
cancel-scope ownership errors that appear when a context manager is exited
from a different task.

All public methods are blocking and safe to call from a Tk callback or another
thread. Shutdown is bounded and falls back to killing the server process tree
so a hung Hatari child cannot keep the host application alive.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import json
import os
import re
import shutil
import signal
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Coroutine

try:  # package import
    from . import paths
except ImportError:  # direct script import
    import paths  # type: ignore[no-redef]

DEFAULT_CALL_TIMEOUT = 60.0
DEFAULT_START_TIMEOUT = 45.0
DEFAULT_SHUTDOWN_TIMEOUT = 30.0
_ERROR_RE = re.compile(r"^([a-z][a-z0-9_]*):\s*(.*?)(?:\s+Hint:\s*(.*))?$", re.DOTALL)


class MCPClientError(RuntimeError):
    """Base error for the synchronous wrapper."""


class MCPTimeout(MCPClientError):
    """A request or shutdown exceeded its configured deadline."""


class MCPToolError(MCPClientError):
    """The MCP server returned an error result for a tool call."""

    def __init__(
        self,
        tool: str,
        message: str,
        *,
        code: str | None = None,
        hint: str | None = None,
        raw: str | None = None,
    ):
        self.tool = tool
        self.code = code
        self.hint = hint
        self.raw = raw
        self.message = message
        text = f"{tool}: {message}"
        if code:
            text = f"{tool}: {code}: {message}"
        if hint:
            text += f" Hint: {hint}"
        super().__init__(text)


def _text_parts(result: Any) -> list[str]:
    parts: list[str] = []
    for item in getattr(result, "content", None) or []:
        if getattr(item, "type", None) == "text" and isinstance(getattr(item, "text", None), str):
            parts.append(item.text)
    return parts


def normalize_tool_result(result: Any, *, tool: str = "<tool>") -> dict[str, Any]:
    """Turn an SDK ``CallToolResult`` into a plain dictionary.

    Structured content wins. Otherwise a single JSON text block is parsed, and
    plain text is returned under ``text``. A result flagged as an error raises
    :class:`MCPToolError` with the structured or textual diagnostic.
    """
    structured = getattr(result, "structured_content", None)
    data: dict[str, Any]
    if isinstance(structured, dict):
        data = dict(structured)
    else:
        text = "\n".join(_text_parts(result)).strip()
        if text:
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, dict):
                data = parsed
            else:
                data = {"ok": True, "text": text}
        else:
            data = {"ok": True}

    if getattr(result, "is_error", False) or data.get("ok") is False:
        text = "\n".join(_text_parts(result)).strip()
        if not text:
            try:
                text = json.dumps(data, sort_keys=True)
            except (TypeError, ValueError):
                text = str(data)
        code = data.get("error") if isinstance(data.get("error"), str) else None
        hint = data.get("hint") if isinstance(data.get("hint"), str) else None
        message = data.get("message") if isinstance(data.get("message"), str) else text
        match = _ERROR_RE.match(text)
        if match:
            code = code or match.group(1)
            message = match.group(2) or message
            hint = hint or match.group(3)
        raise MCPToolError(tool, message or "tool failed", code=code, hint=hint, raw=text)
    return data


def _read_proc_stat(pid: int) -> tuple[int, int] | None:
    """Return ``(ppid, pgrp)`` for a Linux process, or ``None``."""
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    close = raw.rfind(")")
    if close < 0:
        return None
    fields = raw[close + 2 :].split()
    try:
        return int(fields[1]), int(fields[2])
    except (IndexError, ValueError):
        return None


def _cmdline(pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(
            "utf-8", errors="replace"
        )
    except OSError:
        return ""


def _direct_children(ppid: int | None = None) -> set[int]:
    ppid = os.getpid() if ppid is None else ppid
    if not Path("/proc").is_dir():
        return set()
    children: set[int] = set()
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        stat = _read_proc_stat(pid)
        if stat and stat[0] == ppid:
            children.add(pid)
    return children


def _descendants(pid: int) -> set[int]:
    found: set[int] = set()
    pending = [pid]
    while pending:
        current = pending.pop()
        for child in _direct_children(current):
            if child not in found:
                found.add(child)
                pending.append(child)
    return found


def _kill_pid(pid: int, sig: int = signal.SIGKILL) -> None:
    if pid <= 0 or pid == os.getpid():
        return
    stat = _read_proc_stat(pid)
    if stat and stat[1] != os.getpgrp():
        with contextlib.suppress(OSError):
            os.killpg(stat[1], sig)
    with contextlib.suppress(OSError):
        os.kill(pid, sig)


@dataclass
class _Request:
    future: concurrent.futures.Future[Any]
    label: str
    fn: Callable[[], Coroutine[Any, Any, Any]] | None = None
    shutdown: bool = False


class MCPClient:
    """Blocking facade over one MCP stdio server process."""

    def __init__(
        self,
        *,
        repo_root: str | Path | None = None,
        python: str | Path | None = None,
        env: dict[str, str] | None = None,
        cwd: str | Path | None = None,
        call_timeout: float = DEFAULT_CALL_TIMEOUT,
        start_timeout: float = DEFAULT_START_TIMEOUT,
        shutdown_timeout: float = DEFAULT_SHUTDOWN_TIMEOUT,
        server_args: list[str] | None = None,
        errlog: Any = None,
    ):
        self.repo_root = Path(repo_root).expanduser().resolve() if repo_root else paths.repo_root()
        self.python = Path(python) if python else paths.default_server_python()
        self.cwd = Path(cwd).expanduser().resolve() if cwd else self.repo_root
        self.call_timeout = float(call_timeout)
        self.start_timeout = float(start_timeout)
        self.shutdown_timeout = float(shutdown_timeout)
        self.server_args = list(server_args or ["-m", "hatari_mcp"])
        self.errlog = errlog if errlog is not None else sys.stderr
        self.env = self._build_env(env)

        self._lock = threading.RLock()
        self._stop_lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._main_task: asyncio.Task[Any] | None = None
        self._queue: asyncio.Queue[_Request] | None = None
        self._ready: concurrent.futures.Future[None] | None = None
        self._running = False
        self._started = False

        self._session: Any = None
        self._stdio_cm: Any = None
        self._session_cm: Any = None
        self._transport_closed = False
        self._server_pid: int | None = None
        self._runtime_dir: Path | None = None
        self._hmcp_before: set[Path] = set()

    # -- setup ---------------------------------------------------------

    def _build_env(self, extra: dict[str, str] | None) -> dict[str, str]:
        env = os.environ.copy()
        env["PYTHONPATH"] = os.pathsep.join(
            part
            for part in [str(self.repo_root / "mcp/project/src"), env.get("PYTHONPATH", "")]
            if part
        )
        env["HATARI_MCP_REPO"] = str(self.repo_root)
        env.setdefault("PYTHONDONTWRITEBYTECODE", "1")
        if extra:
            env.update({key: str(value) for key, value in extra.items()})
        return env

    # -- lifecycle -----------------------------------------------------

    def start(self) -> "MCPClient":
        with self._lock:
            if self._started:
                return self
            if not self.python.is_file():
                raise MCPClientError(f"MCP server Python not found: {self.python}")
            self._transport_closed = False
            self._server_pid = None
            self._runtime_dir = None
            self._hmcp_before = set(Path(tempfile_dir()).glob("hmcp-*"))
            self._loop = asyncio.new_event_loop()
            self._ready = concurrent.futures.Future()
            self._thread = threading.Thread(
                target=self._run_loop, name="hatari-mcp-client", daemon=True
            )
            self._thread.start()
            try:
                self._ready.result(timeout=self.start_timeout)
            except concurrent.futures.TimeoutError as exc:
                self._stop_thread()
                raise MCPTimeout(
                    f"MCP server did not initialize within {self.start_timeout:.0f}s"
                ) from exc
            except BaseException:
                self._stop_thread()
                raise
            self._started = True
            return self

    def _run_loop(self) -> None:
        assert self._loop is not None
        loop = self._loop
        asyncio.set_event_loop(loop)
        self._queue = asyncio.Queue()
        self._main_task = loop.create_task(self._main())
        try:
            loop.run_forever()
        finally:
            pending = [task for task in asyncio.all_tasks(loop) if not task.done()]
            for task in pending:
                task.cancel()
            if pending:
                with contextlib.suppress(BaseException):
                    loop.run_until_complete(asyncio.wait(pending, timeout=3.0))
            with contextlib.suppress(BaseException):
                loop.run_until_complete(loop.shutdown_asyncgens())
            with contextlib.suppress(BaseException):
                loop.close()
            self._running = False

    async def _main(self) -> None:
        assert self._queue is not None
        try:
            await self._connect()
        except BaseException as exc:
            if self._ready is not None and not self._ready.done():
                self._ready.set_exception(exc)
            await self._close_transport()
            return
        if self._ready is not None and not self._ready.done():
            self._ready.set_result(None)
        self._running = True
        try:
            while True:
                request = await self._queue.get()
                if request.shutdown:
                    try:
                        await self._do_shutdown()
                    except BaseException as exc:
                        if not request.future.cancelled():
                            request.future.set_exception(exc)
                    else:
                        if not request.future.cancelled():
                            request.future.set_result(None)
                    break
                if request.future.cancelled():
                    continue
                try:
                    assert request.fn is not None
                    result = await request.fn()
                except BaseException as exc:
                    if not request.future.cancelled():
                        request.future.set_exception(exc)
                else:
                    if not request.future.cancelled():
                        request.future.set_result(result)
        finally:
            self._running = False
            await self._close_transport()

    async def _connect(self) -> None:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        params = StdioServerParameters(
            command=str(self.python),
            args=list(self.server_args),
            env=self.env,
            cwd=str(self.cwd),
        )
        self._stdio_cm = stdio_client(params, errlog=self.errlog)
        try:
            read, write = await self._stdio_cm.__aenter__()
            self._session_cm = ClientSession(read, write)
            self._session = await self._session_cm.__aenter__()
            await self._session.initialize()
            self._server_pid = self._discover_server_pid()
        except BaseException:
            await self._close_transport()
            raise

    def _discover_server_pid(self) -> int | None:
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            for pid in _direct_children():
                line = _cmdline(pid)
                if "hatari_mcp" in line and ("-m" in line or "hatari_mcp" in line):
                    return pid
            time.sleep(0.02)
        for pid in _direct_children():
            if "hatari_mcp" in _cmdline(pid):
                return pid
        return None

    # -- request plumbing ----------------------------------------------

    def _submit(
        self,
        fn: Callable[[], Coroutine[Any, Any, Any]],
        *,
        label: str,
        timeout: float,
    ) -> Any:
        loop = self._loop
        queue = self._queue
        if loop is None or queue is None or not self._started:
            raise MCPClientError("MCP client is not started")
        future: concurrent.futures.Future[Any] = concurrent.futures.Future()
        request = _Request(future=future, label=label, fn=fn)
        try:
            loop.call_soon_threadsafe(queue.put_nowait, request)
        except RuntimeError as exc:
            raise MCPClientError("MCP client event loop is closed") from exc
        try:
            return future.result(timeout=timeout)
        except concurrent.futures.TimeoutError as exc:
            future.cancel()
            raise MCPTimeout(f"{label} timed out after {timeout:.1f}s") from exc

    def _require_started(self) -> None:
        if not self._started:
            raise MCPClientError("MCP client is not started; call start() or use a context manager")

    def call(self, name: str, timeout: float | None = None, **args: Any) -> dict[str, Any]:
        """Call a tool and return a normalized dictionary."""
        self._require_started()
        effective = self.call_timeout if timeout is None else float(timeout)

        async def invoke() -> dict[str, Any]:
            assert self._session is not None
            try:
                result = await asyncio.wait_for(
                    self._session.call_tool(name, dict(args)), timeout=effective
                )
            except asyncio.TimeoutError as exc:
                raise MCPTimeout(f"{name} timed out after {effective:.1f}s") from exc
            return normalize_tool_result(result, tool=name)

        return self._submit(invoke, label=name, timeout=effective + 10.0)

    def call_tool(self, name: str, timeout: float | None = None, **args: Any) -> dict[str, Any]:
        return self.call(name, timeout=timeout, **args)

    def list_tools(self, *, timeout: float | None = None) -> list[dict[str, Any]]:
        self._require_started()
        effective = self.call_timeout if timeout is None else float(timeout)

        async def invoke() -> list[dict[str, Any]]:
            assert self._session is not None
            result = await asyncio.wait_for(self._session.list_tools(), timeout=effective)
            tools: list[dict[str, Any]] = []
            for tool in getattr(result, "tools", []):
                schema = getattr(tool, "inputSchema", None)
                if schema is None:
                    schema = getattr(tool, "input_schema", None)
                tools.append(
                    {
                        "name": getattr(tool, "name", ""),
                        "description": getattr(tool, "description", "") or "",
                        "input_schema": schema or {},
                    }
                )
            return tools

        return self._submit(invoke, label="list_tools", timeout=effective + 10.0)

    def read_resource(self, uri: str, *, timeout: float | None = None) -> Any:
        """Read a resource; JSON text is parsed, plain text returned as ``str``.

        Multiple contents are returned as a list. A single JSON object becomes
        a dictionary so callers do not need to re-parse the resource.
        """
        self._require_started()
        effective = self.call_timeout if timeout is None else float(timeout)

        async def invoke() -> Any:
            assert self._session is not None
            result = await asyncio.wait_for(
                self._session.read_resource(uri), timeout=effective
            )
            parsed: list[Any] = []
            for item in getattr(result, "contents", []) or []:
                text = getattr(item, "text", None)
                mime = (getattr(item, "mime_type", None) or getattr(item, "mimeType", None) or "")
                if text is None:
                    parsed.append(item)
                    continue
                if "json" in str(mime).lower():
                    with contextlib.suppress(json.JSONDecodeError, TypeError):
                        parsed.append(json.loads(text))
                        continue
                with contextlib.suppress(json.JSONDecodeError, TypeError):
                    value = json.loads(text)
                    if isinstance(value, (dict, list)):
                        parsed.append(value)
                        continue
                parsed.append(text)
            if len(parsed) == 1:
                return parsed[0]
            return parsed

        return self._submit(invoke, label=f"read_resource:{uri}", timeout=effective + 10.0)

    # -- shutdown ------------------------------------------------------

    async def _status_before_stop(self) -> None:
        if self._session is None:
            return
        try:
            result = await asyncio.wait_for(
                self._session.call_tool("emu_status", {}), timeout=2.0
            )
            data = normalize_tool_result(result, tool="emu_status")
        except BaseException:
            return
        self._remember_runtime(data)

    def _remember_runtime(self, data: dict[str, Any]) -> None:
        socket = data.get("socket") or data.get("socket_path")
        if not socket:
            return
        parent = Path(str(socket)).expanduser().parent
        if parent.name.startswith("hmcp-"):
            self._runtime_dir = parent

    async def _do_shutdown(self) -> None:
        await self._status_before_stop()
        if self._session is not None:
            with contextlib.suppress(BaseException):
                await asyncio.wait_for(
                    self._session.call_tool("emu_stop", {"force": True}), timeout=6.0
                )
        await self._close_transport()
        self._cleanup_runtime()

    async def _close_transport(self) -> None:
        if self._transport_closed:
            return
        self._transport_closed = True
        if self._session_cm is not None:
            with contextlib.suppress(BaseException):
                await asyncio.wait_for(
                    self._session_cm.__aexit__(None, None, None), timeout=5.0
                )
        if self._stdio_cm is not None:
            with contextlib.suppress(BaseException):
                await asyncio.wait_for(
                    self._stdio_cm.__aexit__(None, None, None), timeout=10.0
                )
        self._session = None
        self._session_cm = None
        self._stdio_cm = None

    def _cleanup_runtime(self) -> None:
        if self._runtime_dir is None:
            return
        path = self._runtime_dir
        self._runtime_dir = None
        if path.name.startswith("hmcp-"):
            shutil.rmtree(path, ignore_errors=True)

    def stop(self, *, timeout: float | None = None) -> None:
        """Close the session and always stop the emulator/server.

        Idempotent. If the graceful path exceeds the deadline, the server and
        its descendants are killed and any runtime directory created by this
        client is removed.
        """
        with self._stop_lock:
            thread = self._thread
            loop = self._loop
            if thread is None or loop is None:
                self._started = False
                return
            deadline = self.shutdown_timeout if timeout is None else float(timeout)
            if self._started and loop.is_running():
                future: concurrent.futures.Future[Any] = concurrent.futures.Future()
                request = _Request(future=future, label="shutdown", shutdown=True)
                try:
                    loop.call_soon_threadsafe(self._queue.put_nowait, request)  # type: ignore[union-attr]
                    future.result(timeout=deadline)
                except BaseException:
                    self._force_kill()
            else:
                self._force_kill()
            self._cleanup_runtime()
            self._stop_thread()
            self._started = False

    def _force_kill(self) -> None:
        pid = self._server_pid
        if pid is None:
            for child in _direct_children():
                if "hatari_mcp" in _cmdline(child):
                    pid = child
                    break
        if pid is not None:
            # Kill descendants first: Hatari is in its own session with a
            # parent-death signal, but killing it explicitly is deterministic.
            descendants = sorted(_descendants(pid), reverse=True)
            for child in descendants:
                _kill_pid(child)
            _kill_pid(pid)
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                if _read_proc_stat(pid) is None:
                    break
                time.sleep(0.05)
        self._cleanup_runtime()
        for path in set(Path(tempfile_dir()).glob("hmcp-*")) - self._hmcp_before:
            shutil.rmtree(path, ignore_errors=True)

    def _stop_thread(self) -> None:
        thread = self._thread
        loop = self._loop
        if loop is not None and loop.is_running():
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(loop.stop)
        if thread is not None and thread.is_alive():
            thread.join(timeout=5.0)
        self._thread = None
        self._loop = None
        self._queue = None
        self._main_task = None
        self._running = False

    # -- context manager ----------------------------------------------

    def __enter__(self) -> "MCPClient":
        return self.start()

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.stop()


def tempfile_dir() -> str:
    import tempfile

    return tempfile.gettempdir()
