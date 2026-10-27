# Agent session examples

Narrative transcripts of how the MCP tools are meant to be used. They are
documentation for both humans and models: each example shows a goal, the tool
calls, and how to interpret the results.

These are illustrative, not executable tests. Tool output is abbreviated and
may drift from the implementation; the contract is in
`doc/tools-reference.md`.

Examples:

* `boot-inspect-debug.md` — boot headless, wait for the desktop, set a
  breakpoint, inspect registers and memory, step, continue.
* `media-screenshot.md` — mount a floppy, type a command, capture the screen,
  save and restore state.

When the server lands, add a real, runnable version under `tests/e2e/` and keep
these files focused on intent.
