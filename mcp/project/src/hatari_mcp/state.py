"""Explicit emulator lifecycle state."""

from __future__ import annotations

from enum import Enum


class State(str, Enum):
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    REMOTE_PAUSED = "remote_paused"
    AT_PROMPT = "at_prompt"
    UNRESPONSIVE = "unresponsive"
    STOPPING = "stopping"
    EXITED = "exited"
    CRASHED = "crashed"
    FAILED = "failed"


ALIVE = {State.STARTING, State.RUNNING, State.REMOTE_PAUSED, State.AT_PROMPT, State.UNRESPONSIVE}
# Normal-cadence probing only while running. REMOTE_PAUSED is a SIGSTOP'd
# process that cannot service the socket, and AT_PROMPT blocks in stdin. The
# unresponsive state is probed on a slower recovery cadence by the supervisor.
HEARTBEAT_ACTIVE = {State.RUNNING}
WRITABLE_SOCKET = {State.RUNNING}
WRITABLE_STDIN = {State.AT_PROMPT}
