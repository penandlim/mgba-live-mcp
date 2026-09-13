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
uv sync --group dev
```

2. With dependencies already installed, default checks are offline with
   respect to ROM downloads and mGBA; dependency installation itself may use
   the network:

```bash
make test
make check
```

For the optional native emulator smoke, provision the checksum-verified
open-source test ROM explicitly. The native smoke workflow/target is tracked
separately in issue #59 and is not provided by this offline-check change:

```bash
make test-rom
make verify-test-rom
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
- Confirmed-dead session directories are moved to
  `~/.mgba-live-mcp/runtime/archived_sessions/` when no command or recovery worker
  still owns them. Missing process metadata alone never authorizes archival.
- Archived sessions are not treated as active and are not returned by `mgba_live_status` calls.
- This is a hard cutover from repo-local `.runtime`; no hybrid fallback is used.
- This is also a hard API cutover at `0.4.0`: single-session tools require
  explicit `session`, same-session overlap is rejected, and screenshots come
  only from explicit visual tools.
- If you have old repo-local sessions, migrate manually by copying `.runtime/*` to
  `~/.mgba-live-mcp/runtime/`.
  Copied PID-only records remain inspectable, but cannot authorize live commands
  or destructive stop; start a new managed session to establish ownership.
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

### Transaction ownership and recovery

- CLI processes and MCP clients share filesystem-backed, per-session ownership.
  A second operation gets `session_busy`; different sessions remain independent.
  Startup-with-Lua and visual composites hold ownership across their entire
  mutation, settling, capture, and cleanup sequence.
- Cancelling an MCP request does not cancel its synchronous worker or emulator
  execution. The worker retains ownership until it finishes. A command timeout
  leaves its request pending: deleting `command.lua` would not cancel Lua already
  executing inside the emulator.
- `transaction.json` records the directory generation, owner, pending request,
  and `ready`/`stopping`/`stopped` state. Abandoned single commands can be reclaimed
  only when execution is resolved; an interrupted, started composite requires
  recovery stop. Do not delete lock files or the journal to bypass `session_busy`.
- Use `mgba_live_stop` (CLI: `stop --session <id>`) to recover a hung operation.
  Stop fences the generation without waiting for the command lock, then verifies
  process identity and terminates the dedicated group. Late workers cannot
  publish into a stopped or replaced generation.
- Recovery that cannot confirm termination leaves the session registered and
  fenced. Inspect `process_state` and `transaction` in status before retrying
  stop. An unresolved in-memory capture stays under the session's `.views/`
  directory until completion or confirmed stop; successful views leave no file.

### Process identity and stop outcomes

- Supported ownership inspection: Linux `/proc` boot identity plus process
  start ticks; macOS native `libproc` birth seconds/microseconds, absolute start
  ticks from `proc_pid_rusage`, and the kernel boot UUID. The recorded PID must
  also be the dedicated process-group and session leader. Absolute birth and exit
  times remain available for zombies; matching birth is required before reaping.
  Unsupported platforms and missing identity data fail closed; this does not
  provide Windows process control.
- Birth and group ownership are checked immediately before TERM and, if needed,
  KILL. Success requires confirmed group disappearance, not merely successful
  signal delivery or leader exit. Remaining or unverifiable group members keep
  the session unresolved.
- Termination uses a monotonic budget of `grace + 1` seconds: TERM gets up to
  `grace`, with at most one additional second for escalation, permission/exit
  races, and final confirmation. Each brief journal-state lock acquisition has
  a separate 0.5-second bound; command and recovery ownership acquisition is
  nonblocking. Filesystem and native syscall latency are outside those wait
  budgets.
- Stop returns `outcome: stopped` or `outcome: already_exited`. Refusals identify
  `identity_mismatch`, `identity_unverified`, `permission_denied`, or
  `termination_unconfirmed`, including the session, generation, and signal stage.
  CLI stop does not archive its target before reporting this outcome.
- Status includes `process_state`, `identity_verified`, and the transaction
  snapshot. The compatibility `alive` field remains conservative (`true`) when
  death cannot be established; it is not proof of ownership. Attach-by-PID and
  ordinary command admission require verified process birth and group ownership.
  Legacy PID-only records cannot authorize signals or commands.
  Out-of-range PID numbers report `alive: false` and `identity_unverified`;
  their records remain inspectable rather than being archived as confirmed exits.
- Until bridge readiness is committed in `session.json`, inspection does not
  reap children or consume startup exit codes. After readiness, status/pruning
  can reap a birth-verified child and archive its confirmed-dead group. Recovery
  stop can also reap verified children; unavailable or mismatched zombie birth
  metadata never authorizes reaping.
  A missing `ready` field means readiness is unknown, not complete. Use recovery
  stop to reap and retire such a session after native ownership is verified;
  do not add readiness metadata or reap a PID merely to force pruning.

## Local CLI (Dev/Debug)

The MCP controller and CLI both call the shared in-process `SessionManager`.
`scripts/mgba_live.py` is a compatibility shim for the packaged module CLI.

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
