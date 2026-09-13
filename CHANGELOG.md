# Changelog

## Unreleased

- Make startup transactional: preflight inputs before reservation, stage through
  owned directory handles, roll back only pre-launch allocations, and retain
  native identity/logs and failed-startup diagnostics after launch (#61).
- Preserve the previous active session until the entire startup succeeds, and
  preserve literal filesystem-valid session IDs across CLI/MCP, active markers,
  initialization locks and archival without trimming or adding a size policy.
- Reap registered children from dedicated parent-owned waiters so external CLI
  stop can confirm MCP-owned process exit while the server stays alive. Share
  the original `Popen` reader to preserve exit codes across registration/status
  races without weakening native birth or process-group checks (#61).
- Bind child admission and prelaunch rollback to the already-held parent
  directory so namespace replacement cannot redirect coordination-file writes.
  Keep literal session IDs in initialization-lock refusal diagnostics (#61).
- Serialize final activation through marker publication, final guards and journal
  retirement. Restore the exact prior marker on failure without overwriting later
  activations; report unconfirmed restoration instead of claiming rollback (#61).
- Add explicit pinned Qt5/Lua5.4 native provisioning and a separate native CI
  smoke (#59), exercising the real CLI/MCP bridge, observed input/callback
  completion, fully decoded PNG pixels and identity-confirmed process exit.
- Prove scoped cleanup with a controlled live-session failure; retain native
  provenance, logs, heartbeat/journal snapshots and images for 14 days in CI.
- Atomically publish native bridge JSON after checked write/close, preserving
  the previous snapshot on failure. Add a real-Lua publication-boundary
  regression for the zero-byte heartbeat race found by native CI (#59).
- Cache the complete pinned native build using exact recipe/toolchain inputs
  and enable uv caching; retain all real native checks on hits. Verified
  same-head cold/warm jobs took 176/55 seconds, with compilation skipped only
  after an exact cache hit (#59).
- Document clean-machine setup, verified/unverified native combinations and
  µCity ROM/media attribution and redistribution requirements.
- Apply native birth/group identity consistently to attach and command admission,
  expose verified ownership in status, and remove PID-only liveness helpers (#51).
- Preserve startup return codes while allowing normal status/pruning to reap
  birth-verified children only after bridge readiness (#51).
- Keep identity, permission, and termination refusal codes consistent across
  admission and recovery; retain `session_dead` for confirmed exit (#51).
- Report the installed product version during MCP initialization and expose
  per-tool Pydantic success schemas, matching structured/compact text content,
  image-only screenshot bytes, and audited behavior annotations (#47).
- Standardize MCP/CLI failures with domain-owned codes, phases, execution
  outcomes and known session/request context; unknown tools and missing visuals
  now return errors instead of apparent successes. Unknown tool argument fields are
  rejected instead of ignored. CLI errors are compact JSON on stderr.
- Bound the runtime MCP dependency to `>=1.26.0,<2`; an unconstrained fresh
  installation selected SDK 2.2.0, whose API cannot start this 1.x-based server.
- Preserve heterogeneous and falsy Lua values without collapsing them, and keep
  command/capture counters separate; generate the schema/annotation/error reference
  from authoritative definitions.
- Encode Lua `nil` results explicitly as JSON null rather than silently dropping
  the `result` key and producing an empty array. Empty tables retain their prior
  array representation; false/zero/empty-string and shared-table results are preserved.
- Preserve cross-process session ownership across MCP cancellation, command
  timeouts, startup-with-Lua, and settled visual composites (#46).
- Journal pending execution and fence recovery by directory generation; prevent
  late workers and archival from reopening unresolved or replaced sessions.
- Verify native Linux/macOS process birth and dedicated-group ownership before
  TERM/KILL, then confirm bounded group termination independently of hung command
  ownership, as required for verified transaction recovery. Refuse destructive
  control of unverified PID-only records.
- Expose process/transaction state and distinct stop outcomes; retain unresolved
  sessions for inspection and allow CLI stop to report already-exited targets.
- Verify macOS zombie birth with native rusage start/exit times and boot identity
  before reaping, refusing PID replacements and unavailable metadata.
- Remove interrupted temporary captures only after completion or confirmed stop.
- Preserve falsy Lua results in startup composites, including false, zero, empty
  strings, and explicit null values (Copilot review of the ownership changes).
- Keep owned response waits active through indeterminate process inspection;
  only confirmed death or a mismatched birth aborts before the command deadline.
- Refuse known-dead or replaced processes before command publication, leaving
  no pending request that would require unnecessary recovery.
- Make default `make test` and `make check` offline unit checks; retain
  explicit checksum-verified ROM provisioning for native validation.
- Remove redundant ROM cache/provisioning from the default CI job and document
  the separation between offline checks and the optional native smoke.

## 0.5.0

- Remove top-level JSON Schema combinators from MCP tool input schemas so
  strict function-calling clients can load the tool list.
- Preserve runtime validation for conditional arguments, including
  `session`/`pid` attach targets and Lua `file`/`code` source selection.
- Update generated MCP reference docs with runtime argument notes sourced from
  the server metadata.

## 0.4.0

- Hard-cut the MCP contract to explicit session scoping for single-session
  tools, with `session_id` returned on successful single-session responses.
- Replace subprocess-per-command controller behavior with an in-process runtime
  built around `SessionManager` plus async controller wrappers.
- Keep the local CLI as a thin adapter over the shared runtime instead of
  duplicating session/process logic.
- Split metadata-only tools from explicit visual tools, adding
  `mgba_live_get_view`, `mgba_live_run_lua_and_view`,
  `mgba_live_input_tap_and_view`, and `mgba_live_start_with_lua_and_view`.
- Reject same-session overlap with `session_busy` and hard-fail visual settle
  and snapshot errors with `settle_failed` / `snapshot_failed`.
- Update README, generated MCP reference docs, and direct contract/runtime tests
  for the `0.4.0` cutover.

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
