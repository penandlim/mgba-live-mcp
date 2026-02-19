# AGENTS.md

## Project scope

This repository contains `mgba-live-mcp`, an MCP server that manages persistent
`mgba-qt` sessions and exposes live-control tools (start/attach/status/stop,
input, Lua execution, memory reads, OAM/entity dumps, and screenshots).

## Source of truth

- Product behavior and run instructions: `README.md`
- MCP tool definitions and argument mapping: `src/mgba_live_mcp/server.py`
- Subprocess bridge to CLI: `src/mgba_live_mcp/live_controller.py`
- Live session/bridge implementation: `scripts/mgba_live.py`
- In-emulator bridge script: `scripts/mgba_live_bridge.lua`

## Runtime model

- Runtime state is stored under `.runtime/sessions/<session_id>/`.
- The CLI starts `mgba-qt` with a Lua bridge and communicates through
  `command.lua` and `response.json`.
- Session startup currently configures:
  - `-C fpsTarget=<value>`
  - `-s 0`
  - `--script <bridge.lua>`
  - `-l <log_level>`
  - `<rom path>`

## Development workflow

- Use `uv` for local execution in this repo.
- Start server locally:
  - `uv run mgba-live-mcp`
- Run CLI directly during development:
  - `uv run python scripts/mgba_live.py --help`
  - `uv run python scripts/mgba_live.py start --rom <path-to-rom>`

## Change guidelines for this repo

- Keep MCP input schemas and CLI argument mapping in sync whenever adding or
  changing tool parameters.
- Preserve JSON output compatibility for CLI and MCP tool responses.
- Avoid breaking the session directory contract (`session.json`, heartbeat,
  logs, command/response files) used by attach/status flows.
- Prefer unit tests for command construction/parsing over tests that require
  launching a real emulator process.

## Test expectations

- Place tests under `tests/` and run with `uv run pytest`.
- Mock subprocess/process-liveness interactions for deterministic tests.
- For ROM-specific tests, use files under `roms/` but do not hardcode
  machine-specific absolute paths.

