# mgba-live-mcp

MCP server for persistent live control of mGBA using the script bridge workflow.
Commands return structured JSON, and visual snapshots are provided by
`mgba_live_status`/`mgba_live_start`/`mgba_live_attach` and screenshot tools.

## Features

- Start/attach/stop/manage long-lived `mgba-qt` sessions.
- Frame-accurate input (`A/B/Start/Select/Up/Down/Left/Right/L/R`).
- Execute Lua against a running process.
- Read memory and pointer tables.
- Dump OAM and entity data.
- Use `mgba_live_status` after input commands for reliable post-input view checks.
- Optional on-disk PNG persistence via `png`/`out` options for screenshot commands.

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
- Start accepts optional startup Lua script path(s) via `--script`; these are passed through to mGBA in addition to the live bridge script.
- Input commands (`mgba_live_input_*`) return action data and a next-step hint; call `mgba_live_status` for post-input visual assessment.
- Screenshot commands default to `text_format = "hex"`.
- A PNG file is only written when `png` is true (or `out` is provided).
