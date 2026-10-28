# P8 — Disposition of items not executed as full spikes

Status: portfolio disposition only; no production work started
Date: 2026-10-27
Related: `../plans/P8-stretch.md`, `P8-benchmark.md`, `P8-time-travel.md`,
`P8-dwarf.md`, `P8-coverage.md`

No concrete user demand was supplied for these items in this phase. They were
therefore kept unstarted. A GO from the completed spikes would still require an
ADR, a full plan, tests, a licensing review where relevant, and a security
review.

| Item | Classification | Demand in this phase | Feasibility / blocker | Disposition |
| --- | --- | --- | --- | --- |
| P8.0 Lab guard | Done | Phase requirement | `HATARI_MCP_LABS=1`, outside `src/`, no MCP registration, packaging excludes it | Done; tests in `tests/labs/` |
| P8.4 SCC link | Research, non-IP | None | Needs a guest SCC driver and a PTY/FIFO relay; SCC LAN is a byte link, not Ethernet/IP | Not started; stop at missing guest driver |
| P8.5 Remote multi-user | Out of scope for v1 | None (explicit non-goal) | Isolation must be enforced by containers/quotas; application-level path checks are insufficient | Not started; unchanged out-of-scope decision |
| P8.6 Framebuffer streaming | Conditional spike | None | Depends on P7.2/P7.3; stock screenshot latency is 17–19 ms and PNG is not a binary frame path | Not started; only a patched/binary path is credible |
| P8.7 Attach/pair mode | Conditional spike | None | Stock Hatari accepts one outbound control connection; a broker plus P7.1 framing and an input lease are required | Not started; no second-connection shortcut |
| P8.8 Host input capture | Privacy-gated spike | None | Needs P7.4 direct injection and foreground SDL capture; CI cannot supply a gamepad | Not started; opt-in/foreground only if revisited |
| P8.9 LLM test tooling | Orchestration spike | None | Needs P6 artifacts and a failing scenario; value is validation/minimization/clustering, not model training | Not started |
| P8.11 Real hardware | Out of scope | None | Needs physical hardware, capture rig, synchronization, and licensing | Not started; do not reserve CI |
| P8.12 Analog devices | Research, needs patch | None | Needs a P7.4 extension and a guest test program that reads paddle/lightpen registers | Not started; NO-GO without a test program |

## Brief rationale

### P8.4 — SCC link

The plan already limits this to a byte-exact serial link. The repository has no
guest-side SCC driver and no demand was recorded. Building only the host relay
would prove nothing about guest use, so the spike was not started.

### P8.5 — Remote multi-user service

This remains an explicit v1 non-goal. A safe answer requires container-level
isolation, per-tenant workspaces/sockets, quotas, and attack tests. Auth alone
is not isolation. Starting it would be a product decision, not a P8 spike.

### P8.6 — Framebuffer streaming

The measured stock screenshot median is 17–19 ms with observed maxima near 33 ms, and the
path returns a PNG file rather than a binary framebuffer. A 50 Hz stream would
need P7.2/P7.3 and a bounded queue with explicit drops. No demand was recorded.

### P8.7 — Attach/pair mode

The stock control socket is a single outbound connection from Hatari. Sharing
it requires a broker that owns that connection, framed commands, and an input
lease. The plan's dependency on P7.1 is still correct; no shortcut was found or
attempted.

### P8.8 — Host input capture

Host input capture is privacy-sensitive and needs P7.4 plus a foreground SDL
capture path. Without a gamepad and explicit opt-in, any implementation would
be unverifiable. Not started.

### P8.9 — LLM-native test tooling

The useful part is deterministic orchestration over P6 artifacts: schema
validation, execution, delta-debugging, and artifact-grounded explanation. No
failing P6 artifact or demand was available in this phase, so no model calls or
harness were added. Autonomous code changes and unbounded token spend remain
out of scope.

### P8.11 — Real-hardware bridge

No hardware, capture rig, synchronization design, or licensing basis exists.
Out of scope; no resources reserved.

### P8.12 — Analog devices

A patch without a guest test program cannot be validated. The plan's NO-GO
condition (no guest test program) applies. Not started.

## Promotion gate

Any item above that later receives demand must return with:

1. a one-paragraph user need and a measurable question;
2. a pinned environment and a bounded experiment;
3. an ADR and a new plan;
4. unit/integration/corpus tests and a CI budget;
5. security review for remote, pair, host-input, or analog work;
6. licensing review for third-party firmware, ROMs, or drivers.
