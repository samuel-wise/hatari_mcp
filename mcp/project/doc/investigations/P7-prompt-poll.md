# P7 prompt polling investigation (readline/stdin vs control socket)

Status: Investigation complete; no Hatari patch applied in this task.
Date: 2026-10-27.
Scope: stock Hatari base at this checkout (`src/hatari` reports
`Hatari v2.6.1-devel (Oct 27 2026)`; repo commit `368942f1` for the MCP
scaffold). No C source was modified.

## Question

While the debugger is at its prompt, can the control socket deliver commands
without stdin, and what is the smallest safe upstream patch if not?

## Exact observations from this codebase

### The debugger blocks in a stdin read loop

* `src/debug/debugui.c:1305` `DebugUI()` runs a `do { ... } while` loop and
  calls `DebugUI_GetCommand()` at `src/debug/debugui.c:1363`.
* With readline, `DebugUI_GetCommand()` (`src/debug/debugui.c:962`) returns
  `Str_Trim(readline("> "))` (`:974`). Readline owns its own input buffer and
  writes its prompt through its output stream.
* Without readline, the other `DebugUI_GetCommand()` (`src/debug/debugui.c:1017`)
  prints `"> "` and blocks in
  `fgets(input, MAX_DEBUG_CMD_LEN, stdin)` (`:1027`).
* `MAX_DEBUG_CMD_LEN` is 4096 (`src/debug/debugui.h:39`).
* Neither path polls anything except stdin. `DebugUI()` knows nothing about
  the control socket.

### The control socket is only serviced from the GUI event loop

* `Control_CheckUpdates()` is implemented at `src/control.c:455`.
* Its only caller is `GuiEvent_EventHandler()` at `src/sdl/gui_event.c:134`;
  there is no caller from `debugui.c`.
* `Control_CheckUpdates()` reads at most `MAX_DEBUG_CMD_LEN - 1` bytes in one
  `read()` (`src/control.c:458`, `:521`) and calls `Control_ProcessBuffer()`
  (`src/control.c:534`, `:476` for the FIFO path). It does not retain a partial
  line between calls; the comment says "assume whole command can be read in one
  go".
* `Control_ProcessBuffer()` handles `hatari-debug` by calling
  `DebugUI_ParseLine(arg)` (`src/control.c:397`). `DebugUI_ParseLine()` is
  `src/debug/debugui.c:1531`; it initializes debugger state and executes one
  command, but it is not the interactive prompt loop.
* Therefore, once `DebugUI()` is inside `DebugUI_GetCommand()`, a socket
  command is read only if the GUI event loop runs again. At the prompt that
  loop is not running; the process is blocked in stdin. This matches the
  architecture's §2.3 warning and the P7 plan's P7.1b split.

### What the current server does

* `src/hatari_mcp/supervisor.py` selects stdin while
  `state == State.AT_PROMPT` (`WRITABLE_STDIN` in `state.py`) and the socket
  while running/remotely paused (`WRITABLE_SOCKET`).
* `Supervisor.debug()` sends the command, then an
  `echo __HMCP_<uuid>__` sentinel on the same channel, and waits for the
  sentinel on stderr (`supervisor.py`, `_clean_lines`/`_drain_stderr`).
* Breakpoint entry is inferred from stderr; after this P7 change the
  `"You have entered debug mode"` welcome line also transitions to
  `AT_PROMPT`, which prevents the heartbeat from misclassifying a stopped
  debugger as unresponsive.
* Live check during this task (stock `src/hatari`, EmuTOS):
  1. read `cpureg` over the socket while running;
  2. set `breakpoint pc = <next-pc> :once` over the socket;
  3. the breakpoint hit, `AT_PROMPT` was observed, `cpureg` over stdin worked,
     `step` stayed at the prompt, and `cont` returned to `RUNNING`.
  This confirms the dual-channel flow is usable but also confirms that the
  prompt itself is still stdin-only.

### Readline details that make a naive poll unsafe

* Readline buffers input internally. Mixing `select()`/`poll()` on file
  descriptor 0 with `readline()` can report "input ready" while readline has
  already consumed those bytes, or can consume bytes that readline expects.
  A typed partial line can be lost or reordered.
* On a non-TTY stdin (the MCP case), the server passes a pipe. The readline
  build still takes the readline path; the `!isatty()` decision is not in
  `DebugUI_GetCommand()` today. The no-readline build takes the `fgets` path
  and is the simpler target.
* Simply polling the socket in `Control_CheckUpdates()` is not enough: calling
  `DebugUI_ParseLine()` from inside the already-active `DebugUI()` is
  re-entrant. The debugger is not re-entrant; the command must be fed into the
  existing loop.

## Recommended minimal patch (not implemented here)

Split into two independent changes so the running-state framed path can land
without prompt support.

### 0002a — non-TTY prompt polling (primary, testable)

1. Add a public helper in `control.c`, e.g.
   `int Control_PollCommand(char *out, size_t out_size, int timeout_ms)`.
   It should:
   * use `poll()` (or `select()`) on `{STDIN_FILENO, ControlSocket,
     ControlFifo}` with a bounded timeout;
   * keep a static/dynamic receive buffer per control connection so a partial
     line survives across calls (the current one-read assumption is not enough
     for prompt polling);
   * split on `\n`, process `hatari-*` control commands through
     `Control_ProcessBuffer()`/the existing dispatch, and return only a
     debugger command line to the caller;
   * never call `DebugUI_ParseLine()` for a debugger command from this helper
     while the interactive loop is active.
2. In the `!HAVE_LIBREADLINE` `DebugUI_GetCommand()` (`debugui.c:1017`), when
   `!isatty(STDIN_FILENO)`:
   * call `Control_PollCommand()`; if it returns a debugger line, hand that
     line to the existing loop;
   * otherwise fall back to `fgets()` when stdin is readable (stdin remains
     supported for compatibility and scripts);
   * on EOF from stdin, still service the socket (do not break the loop just
     because stdin closed).
3. In a readline build, force the same non-readline path when
   `!isatty(STDIN_FILENO)`. This is the P0.3a `!isatty()` fix and avoids
   readline's buffer on the MCP pipe. It can be selected at
   `DebugUI_GetCommand()` without changing interactive TTY behaviour.

This is the primary patch because the MCP server always uses pipes, and it
can be tested without a terminal.

### 0002b — TTY/readline callback (optional, higher risk)

For an interactive TTY:

* replace the blocking `readline()` call with
  `rl_callback_handler_install("> ", callback)` and drive
  `rl_callback_read_char()` from a `poll()` loop that also drains the control
  socket; or
* keep readline out of the poll loop and document that framed prompt commands
  are disabled on a TTY while typed input continues to work on stdin.

Do not mix `select()` on fd 0 with a blocking `readline()` as a shortcut.

### Prompt-loop integration rules

* Execute exactly one command at a time. The helper must not let the socket
  callback recurse into `DebugUI()`/`DebugUI_ParseLine()`.
* Preserve `cont`, `quit` and `reset` semantics: these leave the loop, and a
  stale sentinel/reply must not be queued after them.
* Keep the legacy `hatari-debug <cmd>` socket path for running state; the new
  prompt path is additive.
* When the framed patch is present, parse complete HMCP `REQUEST` frames in
  the same helper and reply with `OK`/`ERR` carrying the request id. The legacy
  newline path must stay available on the same connection only when no frame
  has been negotiated; a connection is framed or legacy, never mixed.

### Server-side state today

The server does not assume prompt polling. It uses stdin at the prompt,
reports `framed_prompt` as unsupported on stock Hatari, and the heartbeat
probes only `RUNNING`/`REMOTE_PAUSED`; it skips while at the prompt. This
keeps stock behaviour stable and makes the patch purely additive.

## Test plan for the patch

* Build with and without readline.
* Spawn Hatari with a control socket and a pipe for stdin (no TTY), set a
  breakpoint, then send `registers`, `memory`, `step`, `next`, `cont` over the
  socket and assert replies/state; repeat with typed stdin lines.
* Send commands split across `read()` boundaries and multiple commands in one
  write; assert no lost or reordered input.
* Close the client socket at the prompt; assert the debugger remains usable on
  stdin and does not busy-loop.
* For the callback path, type a partial line while injecting a socket command
  and assert both are handled without losing the partial line.
* Run the existing Hatari tests plus the P0–P6 MCP smoke suite against the
  patched build.

## Conclusion

The readline/stdin block is real and cannot be fixed by the Python server.
The non-TTY `poll()` path is the smallest, most testable patch; the readline
callback path should remain optional and explicitly documented if upstream
rejects it. No C patch is included in this P7 server-side task.
