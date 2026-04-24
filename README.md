# mgba-live-mcp
[![codecov](https://codecov.io/gh/penandlim/mgba-live-mcp/graph/badge.svg?branch=master)](https://codecov.io/gh/penandlim/mgba-live-mcp)

MCP server for persistent, live mGBA control. It is designed for agent workflows
that need to keep one emulator process running across multiple tool calls
(input, Lua, memory reads, OAM/entity dumps, screenshots).

If you need one-shot/headless runs instead of persistent sessions, see
[struktured-labs/mgba-mcp](https://github.com/struktured-labs/mgba-mcp).

## What You Get (MCP Context)

- Long-lived session lifecycle: `mgba_live_start`, `mgba_live_attach`,
  `mgba_live_status`, `mgba_live_stop`
- Metadata-only control: `mgba_live_input_tap`, `mgba_live_input_set`,
  `mgba_live_input_clear`, `mgba_live_run_lua`, `mgba_live_start_with_lua`
- Visual tools: `mgba_live_get_view`, `mgba_live_input_tap_and_view`,
  `mgba_live_run_lua_and_view`, `mgba_live_start_with_lua_and_view`,
  `mgba_live_export_screenshot`
- Inspection: `mgba_live_read_memory`, `mgba_live_read_range`,
  `mgba_live_dump_pointers`, `mgba_live_dump_oam`, `mgba_live_dump_entities`
- Explicit session scoping for all single-session tools after `mgba_live_start`
- `session_id` returned in every successful single-session response

MCP reference: [docs/mcp-reference.md](docs/mcp-reference.md)

## Quick Start (uvx)

1. Run directly from PyPI with `uvx`:

```bash
uvx mgba-live-mcp
```

2. If you want to run from git (for unreleased changes), use:

```bash
uvx --from git+https://github.com/penandlim/mgba-live-mcp mgba-live-mcp
```

3. Register in an MCP client (Codex example):

```toml
[mcp_servers.mgba]
command = "uvx"
args = ["mgba-live-mcp"]
```

Git fallback (if package is not yet published):

```toml
[mcp_servers.mgba]
command = "uvx"
args = ["--from", "git+https://github.com/penandlim/mgba-live-mcp", "mgba-live-mcp"]
```

## Local Development

1. Install dependencies for this repo:

```bash
uv sync
```

2. Provision the checksum-verified open-source test ROM used by emulator-backed tests:

```bash
make test-rom
```

3. Run the MCP server:

```bash
uv run mgba-live-mcp
```

4. Register it in your MCP client (Codex example):

```toml
[mcp_servers.mgba]
command = "uv"
args = [
  "run",
  "--directory",
  "/absolute/path/to/mgba-live-mcp",
  "mgba-live-mcp",
]
```

5. Install and run pre-commit hooks (lint/checks via `uv run`):

```bash
make precommit-install
make precommit-run
```

## Requirements And Setup Links

- [mGBA](https://github.com/mgba-emu/mgba):
  Build/install a Qt + Lua-capable binary (`mgba-qt`/`mGBA`) with these required
  CMake flags:
  `-DBUILD_QT=ON -DENABLE_SCRIPTING=ON -DUSE_LUA=ON`
- [uv](https://docs.astral.sh/uv/):
  Python package/runtime manager used by this repo (`uv sync`, `uv run ...`)
- [Model Context Protocol](https://modelcontextprotocol.io):
  Protocol used by the server; configure this process as an MCP server in your client

Important runtime notes:

- A ROM path is required to start (`.gba`, `.gb`, `.gbc`).
- Binary auto-discovery order: `mgba-qt`, `mgba`, `mGBA`.
- If auto-discovery fails, pass `mgba_path` in `mgba_live_start` or
  `mgba_live_start_with_lua`.
- Runtime state is stored at `~/.mgba-live-mcp/runtime` (sessions, logs, command/response files).
- Dead or crashed session directories are moved to
  `~/.mgba-live-mcp/runtime/archived_sessions/` instead of being deleted.
- Archived sessions are not treated as active and are not returned by `mgba_live_status` calls.
- This is a hard cutover from repo-local `.runtime`; no hybrid fallback is used.
- This is also a hard API cutover at `0.4.0`: single-session tools require
  explicit `session`, same-session overlap is rejected, and screenshots come
  only from explicit visual tools.
- If you have old repo-local sessions, migrate manually by copying `.runtime/*` to
  `~/.mgba-live-mcp/runtime/`.
- `mgba_live_status` with `all=true` lists sessions from this shared user-level runtime root.
- `scripts/mgba_live_bridge.lua` is transitional for local workflows; packaged
  `src/mgba_live_mcp/resources/mgba_live_bridge.lua` is the runtime source of truth.

## Common MCP Flows

### 1) Start a session

```json
{
  "rom": "/absolute/path/to/game.gba",
  "fast": true
}
```

Notes:

- `fast: true` maps to `fps_target=600`
- default when omitted is `fps_target=120`

### 2) Start + run Lua immediately

Use `mgba_live_start_with_lua` when you need first-frame setup and metadata only.

```json
{
  "rom": "/absolute/path/to/game.gba",
  "code": "return emu:currentFrame()"
}
```

### 3) Start + run Lua + capture a view

```json
{
  "rom": "/absolute/path/to/game.gba",
  "code": "return emu:currentFrame()"
}
```

Use `mgba_live_start_with_lua_and_view` when you want the same setup flow plus
one post-settle screenshot.

### 4) Tap input and capture after settle

```json
{
  "session": "20260220-120000",
  "key": "A",
  "frames": 2,
  "wait_frames": 6
}
```

Use `mgba_live_input_tap_and_view` for this flow. `wait_frames` is applied
after release before the screenshot is captured.

### 5) Read memory

```json
{
  "session": "20260220-120000",
  "start": 49664,
  "length": 64
}
```

### 6) Get a current view without persisting

```json
{
  "session": "20260220-120000"
}
```

Use `mgba_live_get_view` for a one-off in-memory screenshot.

### 7) Save screenshot to a known path

```json
{
  "session": "20260220-120000",
  "out": "/tmp/mgba-shot.png"
}
```

## Important Behavior

- `mgba_live_start` is bootstrap-only (no Lua arg, no screenshot return).
- `mgba_live_start_with_lua` requires exactly one of `file` or `code`.
- `mgba_live_status(session)` is metadata-only, and `mgba_live_status(all=true)`
  never returns screenshots.
- `mgba_live_run_lua`, `mgba_live_input_tap`, `mgba_live_input_set`,
  `mgba_live_input_clear`, and `mgba_live_start_with_lua` are metadata-only.
- `mgba_live_run_lua_and_view`, `mgba_live_input_tap_and_view`, and
  `mgba_live_start_with_lua_and_view` are the settled visual composite tools.
- `mgba_live_get_view` and `mgba_live_export_screenshot` are explicit screenshot tools.
- `mgba_live_export_screenshot` persists a file and returns that path plus image
  content. `mgba_live_get_view` returns only image content plus frame metadata.
- Visual tools fail hard on settle or snapshot failure instead of returning a
  warning alongside a screenshot.

## Local CLI (Dev/Debug)

The MCP server wraps `scripts/mgba_live.py`. This script is a compatibility shim
that delegates to the packaged module CLI.

```bash
uv run python scripts/mgba_live.py --help
uv run python scripts/mgba_live.py start --help
make test
```

Quality commands:

```bash
make lint
make typecheck
make test
make check
```

## Release Checklist

1. Confirm version is `0.5.0` in `pyproject.toml` and `src/mgba_live_mcp/__init__.py`.
2. Add release notes in `CHANGELOG.md`.
3. Run local checks:
`uv sync --group dev && make check && uv build`
4. Trigger TestPyPI publish workflow (`publish-testpypi`) and verify install from TestPyPI.
5. Push tag `v0.5.0` to trigger the PyPI release workflow.
6. Smoke test:
`uvx mgba-live-mcp` and `uvx --from git+https://github.com/penandlim/mgba-live-mcp mgba-live-mcp`.
