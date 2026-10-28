# P8 lab package (non-shipping)

This directory contains disposable research harnesses for the P8 stretch
portfolio. It is **not** production code and must not be treated as a feature
commitment.

- Nothing here is imported by `src/hatari_mcp`.
- Nothing here registers an MCP tool.
- The directory is outside `src/`, so `pyproject.toml` does not package it; a
  wheel build is checked in `tests/labs/test_lab_guard.py`.
- Every submodule requires `HATARI_MCP_LABS=1`; `import labs` alone is inert.
- Raw measurements and generated files belong under `/tmp/hatari-mcp-p8-*`.
- A GO from a spike authorizes a new ADR and plan, not implementation.

Run from `mcp/project` (the virtualenv lives one directory up):

```sh
HATARI_MCP_LABS=1 ../.venv/bin/python -m labs bench
HATARI_MCP_LABS=1 ../.venv/bin/python -m labs time-travel
HATARI_MCP_LABS=1 ../.venv/bin/python -m labs dwarf
HATARI_MCP_LABS=1 ../.venv/bin/python -m labs coverage
```

The coverage spike can audit retained JSON without starting Hatari or writing a
1 GiB trace again:

```sh
HATARI_MCP_LABS=1 ../.venv/bin/python -m labs coverage \
  --from-json /tmp/hatari-mcp-p8-final/coverage/coverage/coverage.json \
  --out /tmp/hatari-mcp-p8-audit/coverage
```

Coverage output is written to `<out>/coverage.json`; the retained pre-fix tree
keeps its historical `coverage/coverage/coverage.json` path for the `--from-json`
input.

Tests live in `../tests/labs`. Tests that need the gitignored AHCC
`HELLO_ST.*` corpus carry an explicit skip guard, so a clean checkout still
passes; the tests do not download or fabricate the external corpus. The wheel
test builds a wheel with a Python that has `setuptools` and `wheel`, then
asserts that no `labs/` module is packaged and that importing the lab package
does not change the MCP server tool set.

Each command prints the JSON result path. Investigation notes live in
`../doc/investigations/P8-*.md`.

Delete or promote these harnesses after the investigations. Do not copy them
into `src/hatari_mcp` without a promoted plan.
