# Agent session examples

Narrative transcripts of how the MCP tools are meant to be used. They are
documentation for both humans and models: each example shows a goal, the tool
calls, and how to interpret the results.

These are illustrative, not executable tests. Tool output is abbreviated and
may drift from the implementation; the contract is in
`doc/tools-reference.md`. For runnable, schema-validated automation, see
`scenarios/` and `doc/tools-reference.md`'s scenario tools.

Examples:

* `boot-inspect-debug.md` — boot headless, wait for the desktop, set a
  breakpoint, inspect registers and memory, step, continue.
* `media-screenshot.md` — mount a floppy, type a command, capture the screen,
  save and restore state.

`programs/` contains four runnable host-side Tkinter applications that use the
MCP server: a game launcher, an interactive debugger cockpit, a time-travel
debugger, and a crash detective. See `programs/README.md` for run commands,
features and limitations.

Runnable end-to-end coverage lives under `tests/e2e/`; keep these files focused
on intent and current tool names.
