"""Bounded, sequence-addressed stream buffers."""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass


@dataclass(frozen=True)
class Entry:
    seq: int
    ts: float
    kind: str
    text: str

    def as_dict(self) -> dict:
        return {"seq": self.seq, "ts": self.ts, "kind": self.kind, "text": self.text}


class RingBuffer:
    def __init__(self, max_bytes: int = 4 * 1024 * 1024):
        self.max_bytes = max_bytes
        self._entries: deque[Entry] = deque()
        self._bytes = 0
        self._next = 1

    def add(self, kind: str, text: str) -> Entry:
        entry = Entry(self._next, time.time(), kind, text)
        self._next += 1
        self._entries.append(entry)
        self._bytes += len(text) + 64
        while self._bytes > self.max_bytes and self._entries:
            old = self._entries.popleft()
            self._bytes -= len(old.text) + 64
        return entry

    def read(
        self,
        *,
        since: int | None = None,
        limit: int = 200,
        kinds: set[str] | None = None,
    ) -> tuple[list[Entry], int | None]:
        limit = max(1, min(int(limit), 1000))
        out: list[Entry] = []
        for entry in self._entries:
            if since is not None and entry.seq <= since:
                continue
            if kinds is not None and entry.kind not in kinds:
                continue
            out.append(entry)
            if len(out) >= limit:
                break
        cursor = out[-1].seq if out else since
        return out, cursor

    def tail(self, limit: int = 200) -> list[Entry]:
        entries = list(self._entries)[-max(1, min(limit, 1000)) :]
        return entries

    def clear(self) -> None:
        self._entries.clear()
        self._bytes = 0
