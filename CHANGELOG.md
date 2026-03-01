# Changelog

## 0.3.2

- Fix packaging so published wheels include runtime Python modules
  (`mgba_live_mcp.server`, `mgba_live_mcp.live_cli`), not only resources.
- Keep bridge resource packaging intact.
- Strengthen release workflow artifact validation to assert runtime modules are
  present in the wheel.

## 0.3.1

- Archive dead/crashed session directories to
  `~/.mgba-live-mcp/runtime/archived_sessions` instead of deleting them.
- Keep dead sessions out of active session resolution and status listings.
- Improve stalled-session errors with explicit diagnostics and likely causes
  (including bad ROM build/patch and Lua deadloops).
- Add scoped stale `command.lua` cleanup when a command response times out.
- Expand test coverage for stall diagnostics, timeout handling, and archive
  behavior.
- Remove user-specific absolute path from Lua template README examples.

## 0.2.0

- Switched live-controller subprocess execution to packaged module invocation
  (`python -m mgba_live_mcp.live_cli`) for install-safe `uvx` usage.
- Moved runtime CLI implementation into `src/mgba_live_mcp/live_cli.py` and kept
  `scripts/mgba_live.py` as a compatibility shim.
- Hard cutover of runtime root to `~/.mgba-live-mcp/runtime`.
- Packaged Lua bridge resource under `src/mgba_live_mcp/resources/` and now stage
  a session-local copy before launching mGBA.
- Added `mgba-live-cli` entrypoint in `pyproject.toml`.
- Added CI and release checks to validate wheel artifacts contain
  `mgba_live_mcp/resources/mgba_live_bridge.lua`.
- Added manual TestPyPI publishing workflow.
- Updated README for `uvx`-first usage, migration notes, and release checklist.
