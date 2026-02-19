# mgba-live-mcp

MCP server for persistent live control of mGBA using the script bridge workflow.
Commands return structured JSON, and visual snapshots are provided by
`mgba_live_status`/`mgba_live_attach`/`mgba_live_start_with_lua` and `mgba_live_export_screenshot`.

## Features

- Start/attach/stop/manage long-lived `mgba-qt` sessions.
- Frame-accurate input (`A/B/Start/Select/Up/Down/Left/Right/L/R`).
- Execute Lua against a running process.
- Read memory and pointer tables.
- Dump OAM and entity data.
- Use `mgba_live_status` after input commands for reliable post-input view checks.
- Optional on-disk PNG persistence via `png`/`out` options for `mgba_live_export_screenshot`.

## Run

```bash
cd ~/Documents/mgba-live-mcp
uv run mgba-live-mcp
```

## Claude Code MCP Config

Set in `~/.codex/config.toml`:

```toml
[mcp_servers.mgba]
command = "uv"
args = [
  "run",
  "--directory",
  "/Users/jongseunglim/Documents/mgba-live-mcp",
  "mgba-live-mcp",
]
```

Notes:
- Start defaults to `120` FPS unless `fps_target` is provided.
- `mgba_live_start` is MCP bootstrap-only and does not accept `script` or return a screenshot.
- `mgba_live_start_with_lua` starts a new session, runs `file` or `code`, then returns the post-Lua screenshot/image.
- CLI `scripts/mgba_live.py start` still supports `--script` startup Lua paths passed directly to mGBA.
- Input commands (`mgba_live_input_*`) return action data and a next-step hint; call `mgba_live_status` for post-input visual assessment.
- `mgba_live_export_screenshot` is the preferred screenshot tool name. `mgba_live_screenshot` remains a backward-compatible alias.
- `mgba_live_export_screenshot` defaults to `text_format = "hex"`.
- A PNG file is only written when `png` is true (or `out` is provided).
- For callback-based Lua macros, return a table with `macro_key` (for example `{ status = "started", macro_key = "__my_macro" }`) and set `_G[macro_key].active = false` when done so `mgba_live_run_lua` can wait for completion before returning its snapshot.
