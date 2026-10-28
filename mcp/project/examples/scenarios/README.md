# Scenario examples (v1)

Scenarios are JSON (YAML is accepted only when the optional PyYAML package is
installed). They are executable configuration, not trusted code: unknown keys
are errors, `${VAR}` expands only from declared `requires.env`, `vars`, `env`
and matrix axes, and paths must stay inside the scenario directory, the
external corpus roots or `/tmp`.

```json
{
  "version": 1,
  "id": "boot-capture-stop",
  "name": "Boot, capture, stop",
  "requires": {"env": ["HATARI_TOS"], "rom": "emutos", "skip_reason": "..."},
  "determinism": "tolerant",
  "timeouts": {"scenario_ms": 45000, "step_ms": 30000},
  "config": {"profile": "ste", "tos": "${HATARI_TOS}"},
  "matrix": {"machine": ["st", "ste"]},
  "steps": [
    {"id": "start", "tool": "emu_start", "args": {}, "timeout_ms": 30000, "expect": {"ok": true}},
    {"id": "capture", "tool": "screen_frame_capture", "args": {},
     "capture": {"name": "desktop", "kind": "screenshot", "format": "bmp", "hash": "rgba"}},
    {"id": "stop", "tool": "emu_stop", "args": {}}
  ],
  "teardown": [{"id": "teardown-stop", "tool": "emu_stop", "args": {"force": true}}]
}
```

## Top-level keys

| Key | Meaning |
| --- | --- |
| `version` | Must be `1`. |
| `id` / `name` | `id` is the stable scenario id; `name` is required or defaults to `id`. |
| `description` | Free text. |
| `requires` | `env`, `files`, `dirs`, `rom` (`emutos`/`tos`/`either`), `tools`, `skip_reason`. Missing prerequisites produce `skipped`, never a false pass. |
| `determinism` | `strict`, `tolerant` or `off` (or `{mode, seed}`). Strict stock-Hatari options are injected; fidelity is documented as `approximate-frame`, never VBL-exact. |
| `timeouts` | `scenario_ms` and default `step_ms`. A step may lower, not raise, the scenario deadline. |
| `config` | Defaults passed to `emu_start`: `profile`, `tos`, `workspace`, `autostart`, GEMDOS options, `args`. `args` is checked against an unsafe-option denylist and path confinement. |
| `matrix` | Scalar axes; `max_cases` and `fail_fast` are reserved metadata. Case ids are deterministic (`id_axis_value` with non-alphanumerics sanitized). |
| `overrides` | `[{match: {axis: value}, timeouts/config/determinism/env/vars/skip_reason}]`. |
| `stage` | `[{from, to, optional}]`, read-only copies from an external tree into the per-case workspace. |
| `record` | Record input events and decoded-RGBA checkpoints into `replay/`. Fidelity is always `approximate` without P7.4/P7.5. |
| `seed` | Recorded in the manifest; P6 does not claim it seeds guest RNG. |
| `attempts`, `retry_on` | Opt-in retries; timeouts are not retried unless named explicitly. |

## Step keys

`id`, `tool`, `args`, `timeout_ms`, `expect`, `capture`, `assert`/`asserts`.
`tool` is a real MCP tool name (for example `emu_start`, `input_text`,
`dbg_registers_get`). `expect` is subset equality. `assert` supports
`eq`/`ne`/`contains`/`regex`/`gte`/`lte`/`exists`/`type` on a dotted result path.

`capture.kind` is `result` (write the tool result), `file` (copy the result
`path` into the bundle), or `screenshot`. Screenshot steps use the engine's
unquoted debugger screenshot path and record a decoded-RGBA SHA-256 for BMP.
Decoded-RGBA hashing requires **uncompressed 24- or 32-bit BI_RGB BMP**
(BITMAPINFOHEADER or larger); compressed, palette, RLE and 1/4/8/16-bit BMPs are
not decoded. Capture PNG only with `hash: sha256` (PNG bytes); `format: png`
with `hash: rgba` is a schema error.

Scenario steps may only call a conservative allowlist of tools (lifecycle,
screen/capture, input, log/console reads, safe debugger reads, and path-checked
media/run_prg). `dbg_command`, `ahcc_*`, `scenario_*`, `emu_set_option`,
`emu_set_path`, memory/state writers and trace/profile writers are denied.
`capture.path`, `stage.to` and artifact paths must be relative and stay inside
the case bundle/workspace; corpus and repository trees are never writable.

## CLI

```sh
python -m hatari_mcp.scenario_cli validate examples/scenarios/boot-capture-stop.json
python -m hatari_mcp.scenario_cli run examples/scenarios/boot-capture-stop.json \
  --artifacts /tmp/hatari-artifacts --max-cases 4 --fail-fast --json --junit /tmp/junit.xml
python -m hatari_mcp.scenario_cli list-corpus --json
python -m hatari_mcp.scenario_cli repeat examples/scenarios/strict-boot.json --repeats 3
python -m hatari_mcp.scenario_cli replay /tmp/hatari-artifacts/<run>/<case>/attempt-1
```

Exit codes: `0` pass, `1` failure, `2` usage/schema, `3` infrastructure,
`4` watchdog, `5` internal, `130` interrupted.
