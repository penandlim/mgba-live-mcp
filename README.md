# mgba-live-mcp

MCP server for persistent, live mGBA control. It is designed for agent workflows
that need to keep one emulator process running across multiple tool calls
(input, Lua, memory reads, OAM/entity dumps, screenshots).

If you need one-shot/headless runs instead of persistent sessions, see
[struktured-labs/mgba-mcp](https://github.com/struktured-labs/mgba-mcp).

## What You Get (MCP Context)

- Long-lived session lifecycle: `mgba_live_start`, `mgba_live_attach`,
  `mgba_live_status`, `mgba_live_stop`
- Live control: `mgba_live_input_tap`, `mgba_live_input_set`,
  `mgba_live_input_clear`, `mgba_live_run_lua`
- Inspection: `mgba_live_read_memory`, `mgba_live_read_range`,
  `mgba_live_dump_pointers`, `mgba_live_dump_oam`, `mgba_live_dump_entities`
- Snapshot output: `mgba_live_export_screenshot` plus image snapshots returned
  by `status`/`attach`/`start_with_lua`/most live commands

## Quick Start

1. Install dependencies for this repo:

```bash
uv sync
```

2. Run the MCP server:

```bash
uv run mgba-live-mcp
```

3. Register it in your MCP client (Codex example):

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

Use `mgba_live_start_with_lua` when you need first-frame setup and a post-Lua
snapshot in one call.

```json
{
  "rom": "/absolute/path/to/game.gba",
  "code": "return emu:currentFrame()"
}
```

### 3) Tap input and capture after settle

```json
{
  "session": "20260220-120000",
  "key": "A",
  "frames": 2,
  "wait_frames": 6
}
```

`wait_frames` is applied after release before the screenshot is captured.

### 4) Read memory

```json
{
  "session": "20260220-120000",
  "start": 49664,
  "length": 64
}
```

### 5) Save screenshot to a known path

```json
{
  "session": "20260220-120000",
  "out": "/tmp/mgba-shot.png"
}
```

## Important Behavior

- `mgba_live_start` is bootstrap-only (no Lua arg, no screenshot return).
- `mgba_live_start_with_lua` requires exactly one of `file` or `code`.
- `mgba_live_run_lua` supports callback-style macros by returning
  `{ macro_key = "..." }` and setting `_G[macro_key].active = false` when done;
  the tool waits for completion before returning its snapshot.
- `mgba_live_input_set` and `mgba_live_input_clear` update held keys but do not
  include a snapshot; call `mgba_live_status` to verify visually.

## Local CLI (Dev/Debug)

The MCP server wraps `scripts/mgba_live.py`.

```bash
uv run python scripts/mgba_live.py --help
uv run python scripts/mgba_live.py start --help
uv run pytest
```

Quality commands:

```bash
make lint
make typecheck
make test
make check
```
