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

For real emulator validation, use the explicit
[native Qt/Lua smoke](#native-qtlua-smoke) below. Its ROM provisioning and emulator
execution are never prerequisites of `make test` or `make check`:

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
- Session IDs are literal, nonempty directory components: no `/`, `\`, NUL,
  `.` or `..`. Whitespace, newlines and Unicode are preserved exactly through
  CLI/MCP arguments and the active marker. There is no additional character or
  length policy; the filesystem must accept the literal component. Only derived
  initialization-lock names are hashed, never the session directory itself.
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
- Every promised image must be a readable, fully validated PNG. Image content uses
  the capture's validated bytes, not a later read of a potentially replaced path.
- Omitting `out` allocates an exclusive, unique filename; implicit exports never
  overwrite earlier captures, including simultaneous cross-process requests.
- An explicit `out` retains `Path(out).resolve()` semantics and permits overwrite.
  Capture is staged on the destination filesystem, then atomically replaces the
  destination only after PNG validation. Failed capture or publication leaves the
  previous file intact. Concurrent writes to the same explicit destination use
  **last successful publication wins**; each response still carries its own image.
- Temporary captures are tracked by the owning session operation. Cancellation
  does not stop a native writer: cleanup waits for actual completion, a later
  request reconciling its correlated response, or verified stop. Cleanup never
  follows an unexpected bridge-returned path or sweeps unrelated image files.
- Screenshot failures include the known session/request, failed stage
  (`capture`, `validation`, `persistence`, or `cleanup`), and retained artifact
  paths when applicable. A cleanup failure after publication also reports
  `published_path`; do not assume that an error rolled back a completed mutation.
  If both staging rollback and its recovery-journal write fail, `journal_error`
  explains why automatic recovery cannot track the reported retained file.
  That file requires manual recovery after the filesystem problem is resolved.
  Reconciliation cleanup failures preserve the recorded request and retained-file
  details even if updating the journal also fails.
- Visual tools fail hard on settle or snapshot failure instead of returning a
  warning alongside a screenshot.

### Machine-readable protocol contracts

- Initialization reports the installed `mgba-live-mcp` package version, not the
  MCP SDK version. Every tool declares a success `outputSchema` and behavior hints.
  The runtime requires `mcp>=1.26.0,<2` because the server uses the SDK's 1.x
  low-level API; MCP 2.x is not supported.
  Initialize/catalog/error smoke is verified with locked SDK 1.26.0 and a fresh
  wheel install resolving SDK 1.30.0, not an exhaustive compatibility matrix.
- Success `structuredContent` is identical to the first compact JSON text block.
  Existing fields are preserved, including `value` for all-session status and
  heterogeneous Lua `data`/`lua` values (`false`, `0`, `""`, arrays, objects, null).
  Command `frame` and `screenshot.frame` are distinct counters.
  Lua `return nil` now yields explicit JSON `null` (`data.result` or startup `lua`)
  instead of the accidental empty array; `return {}` remains an empty array.
- Screenshot bytes occur only in MCP image blocks, never duplicated as base64 in
  the text/structured JSON. Metadata-only tools still return no images.
- **Intentional error-format change:** all tool failures, including unknown names,
  invalid arguments and missing visual content, return `isError=true` with
  matching `{ "error": { "code", "message", "phase", "execution_outcome", ... } }`
  JSON text/structured content. Known session, bridge request and MCP request
  context is retained; error objects do not match the success output schemas.
  CLI failures use the same domain codes/context as JSON on stderr and exit nonzero.
- Tool argument objects reject unknown fields with `invalid_arguments` rather than
  silently ignoring typos. Malformed MCP request envelopes (such as argument arrays)
  are rejected by the SDK with JSON-RPC `-32602` before tool dispatch.
- `execution_outcome` is `not_executed` only when nonexecution is proven,
  `completed` when the requested command's execution is known to have completed,
  or `unknown` when execution or settling cannot be established. These replace
  the former `not_started`/`partial` vocabulary. A completed mutation can still
  have a failed capture or result; do not replay it to recover an image.
  See the generated [error inventory and schemas](docs/mcp-reference.md).
- Annotations are hints, not security controls. Status can archive dead sessions;
  attach updates the active marker; export can overwrite a file; Lua is unrestricted
  and is neither read-only nor safely retryable. Read/idempotence hints exclude
  bridge bookkeeping and do not imply that a running game's state is frozen.

### Lua JSON returns and pointer precision

- Lua results may contain `nil` (JSON null), booleans, finite numbers, valid UTF-8
  strings, consecutive `1..N` array tables, and string-keyed object tables.
  Empty tables remain arrays. Shared acyclic tables are allowed; recursive tables,
  sparse/mixed numeric keys, functions, userdata, threads, and non-finite numbers
  are rejected instead of stringified, rounded, or replaced with null.
- Text controls are JSON-escaped and Unicode is preserved. Arbitrary binary bytes
  must be represented explicitly, for example `return {0, 255}` or
  `return "00ff"`; binary Lua strings are not a JSON text representation.
  Unicode surrogate code points are rejected independently of the native `utf8`
  library's validation behavior.
- The **complete bridge response**, including its envelope, is limited to
  **1,048,576 encoded UTF-8 bytes**, **32 nested tables** (the response root is
  depth 1), and **10,000 visited table entries**. Repeated shared subtrees count
  on every traversal. String escape expansion is checked before allocation.
  These are serialization limits, not limits on arbitrary Lua execution.
- Unsupported results and non-text Lua error objects produce correlated
  `serialization_failed` errors with `phase: serialization`, the request/session
  identifiers, bridge counter, and a bounded `serialization_reason`. If Lua
  returned successfully before serialization failed, the error retains
  `command_completed: true` and `execution_outcome: completed`. The mutation
  already happened; do not replay it merely to recover its result.
  An error during Lua execution keeps an unknown outcome.
- Serialization rejection permits the next request. Response publication failure
  instead emits a bounded JSON diagnostic on emulator stderr and leaves the
  transaction unresolved; use the existing reconciliation/recovery-stop flow.
  The fallback error never serializes the rejected result or invokes its
  `tostring` metamethod.
- Pointer dumps accept only integer widths **1–6 bytes**, defaulting to 4.
  Little-endian values remain JSON integers, exactly represented up to
  **281474976710655 (`2^48 - 1`)**. Widths 7/8, booleans, fractions, and out-of-range
  widths are rejected before memory reads, never clamped. To inspect wider raw
  storage, use a byte-range read and decode it explicitly in the client.
  Bytes are read in ascending address order, preserving the existing MMIO
  read sequence.

### Bounded memory inspections

`read-memory`, `read-range`, `dump-pointers`, and `dump-entities` share fixed
budgets in the CLI and MCP:

- At most **4096 source bytes** per request. Pointer `count * width` and entity
  `count * size` must fit this budget.
- At most **1024 sparse addresses**, **512 pointers**, or **590 one-byte entities**;
  larger entities can require fewer records. Dimensions must be positive integers.
  Duplicate sparse addresses each consume a read; the existing address map keeps
  the last value for each address.
- Addresses and the final byte must fit the active core: **0–65535 on GB/GBC**,
  **0–4294967295 on GBA**. The bridge asks the emulator for its platform; it does
  not infer it from the ROM filename. An unavailable/unknown platform is rejected.
- At most **32 KiB of conservatively estimated native JSON**. This pre-read
  admission budget includes the complete compact bridge success envelope, not
  CLI pretty-printing or MCP protocol wrappers. Records/spans can hit this
  budget before the source-byte/item caps; no data is silently truncated.

All budgets apply together. Both host and bridge reserve the larger of 2048 bytes
or 256 bytes plus the encoded request/session IDs for metadata and shape headers.
They then charge these worst-case payload costs (`N` source bytes, `C` records):

| Selection | Payload charge |
| --- | --- |
| Sparse addresses | 17 bytes per requested address |
| Byte range | `4 * N` |
| Hex range | `2 * N` |
| Delta range | `26 * ceil(N / 2) + 2 * N` |
| Pointers | `60 * C` |
| Entities | `48 * C + 4 * N` |

With normal-sized IDs, 512 pointers, 480 four-byte entities, or a 2048-byte delta
exactly fill their estimated budget; one more record/byte is rejected before
reads. Delta admission assumes worst-case fragmentation even if the baseline
later proves unchanged. Byte-array and hex ranges still accept 4096 source bytes.
The shared Lua serializer's independent **1 MiB**, **10,000-entry**, and
**32-level** safety limits remain unchanged for arbitrary Lua and correlated
error envelopes, so an oversized request's identifiers can still be reported.

Invalid selections and budgets fail before any memory read. `invalid_arguments`
reports invalid dimensions, addresses, encodings, or baselines; `inspection_limit`
includes `limit_name`, `limit`, `max_read_bytes`, `max_items`, and
`max_response_bytes`, with a smaller-chunk suggestion. `inspection_unsupported`
reports an unavailable platform. A failed/non-byte native read returns
`inspection_read_failed`, not a fabricated zero or partial success.

`read-range` keeps the default byte array and offers two opt-in encodings:

```sh
uv run python scripts/mgba_live.py read-range --session game --start 0xC000 --length 4
uv run python scripts/mgba_live.py read-range --session game --start 0xC000 --length 4 --encoding hex
uv run python scripts/mgba_live.py read-range --session game --start 0xC000 --length 4 --encoding delta \
  --baseline '{"start":49152,"data":"00010203"}'
```

The MCP equivalent is `mgba_live_read_range` with
`{"session":"game","start":49152,"length":4,"encoding":"delta","baseline":{"start":49152,"data":"00010203"}}`.
Baselines are caller-owned and stateless: provide exactly `start` and `data`,
matching the requested start and byte length. Hex input is case-insensitive,
without whitespace or a prefix. A baseline is required only for `delta`.

All responses preserve the root `session_id` and `frame`. The `range` payload is:

- Bytes: `{start, length, data: [0, 1, 255, 3]}`.
- Hex: `{start, length, encoding: "hex", data: "0001ff03"}`.
- Delta: `{start, length, encoding: "delta", spans: [{offset: 2, data: "ff"}]}`
  for that example relative to `00010203`. Spans are maximal contiguous changes,
  ordered by zero-based byte offset; apply them to a copy of the baseline.
  Unchanged data yields `spans: []`; all-changed data yields one complete span.

Split larger reads explicitly. `frame` is a **bridge callback counter**, not a
native emulator frame number; separate chunks need not observe the same frame
or game state. These bounds do not sandbox arbitrary Lua or change OAM behavior.
Restart existing sessions after upgrading to load the current bridge checks.

### Transactional startup

- ROM, executable, bridge, startup Lua, initial savestate and numeric options
  are checked before reserving a session or changing the active marker. An
  existing explicit session ID is rejected rather than reused.
- Startup inputs and logs are staged through the reserved directory's open
  handles and already-held parents. A pre-launch failure rolls back only that
  creator's directory generation; replaced namespaces and symlink targets are
  never coordination-file or cleanup targets.
- Once mGBA starts, its native identity and logs remain discoverable if readiness
  or startup Lua fails. Status exposes `startup.state: failed` and the error.
  Failed startup records are retained until explicit recovery stop. If
  registration cannot be committed, startup instead attempts identity-verified
  cleanup and reports its outcome.
- Successful startup activates the new session only after readiness; a startup
  composite waits until its Lua, settling and requested visual result complete.
- Final activation holds a short singleton marker lock through publication,
  final ownership checks and transaction retirement. Attach and automatic
  active-session refresh use the same lock. No emulator/readiness wait runs
  under it.
- If publication or finalization fails, the exact previous marker (or its
  absence) is restored under that lock, before a later activation can proceed.
  If restoration or its durability cannot be confirmed, the error reports
  `active_marker_restore.confirmed: false` and the observed `active_session`
  (or an `active_session_error`). Do not assume that a failed start restored
  the marker when restoration is unconfirmed.

### Operation deadlines

- Every `timeout` is a finite positive number of seconds. Booleans, nonnumbers,
  zero, negative values, NaN, infinity, and unrepresentable numeric values fail
  with `invalid_arguments` before allocation or dispatch.
- One monotonic deadline begins at the public CLI/MCP/controller boundary.
  Validation, worker queueing, acquisition, startup, native execution, settling,
  capture, and result assembly spend the same budget. Nested timeout caps may
  shorten that deadline but never renew it. Wall-clock changes do not affect it.
  CLI `start --ready-timeout` retains its spelling but covers complete startup,
  not just the readiness ping. CLI attach/status also accept `--timeout`.
- Timeout errors retain the failed `phase`, session, and allocated request IDs.
  When a primary Lua/input command completed before a later failure,
  `command_completed: true` and `command_request_id` retain that evidence;
  `request_id` can identify a different, failed capture/poll request.
  `cause_code`, `cause_phase`, and `cause_execution_outcome` describe the failed
  composite stage. Readiness-ping completion never implies startup Lua ran.
  Known bridge failures remain the primary error even if completion bookkeeping
  crosses the deadline; `completion_error` retains the bookkeeping failure.
- Deadlines are cooperative, not hard cancellation of native code or filesystem
  syscalls. No new command/capture starts after expiry. Ownership-preserving
  reconciliation, journaling, rollback, and late-child registration may finish
  beyond it; each safety-state lock wait is bounded to 0.5 seconds, while native
  and filesystem syscall latency cannot be preempted. This never grants Lua or
  a later capture a fresh execution budget.
- Stop/recovery deliberately has no operation `timeout`. Its nonnegative `grace`
  and independent termination/confirmation bounds below remain available even
  when the requesting operation's budget has expired.

### Transaction ownership and recovery

- CLI processes and MCP clients share filesystem-backed, per-session ownership.
  A second operation gets `session_busy`; different sessions remain independent.
  Startup-with-Lua and visual composites hold ownership across their entire
  mutation, settling, capture, and cleanup sequence.
- Cancelling an MCP request does not cancel its synchronous worker or emulator
  execution. The worker retains ownership until it finishes under its original
  deadline. Timeout is not proof that a mutation did not happen.
- The packaged bridge atomically claims `command.lua` as `command.lua.running`.
  On expiry the host withdraws only a request it can atomically prove is still
  pending and owned. It never overwrites a raced-in foreign command.
  Claimed/running work, interrupted composites, and older/custom bridges without
  the `rename-v1` claim handshake remain conservatively fenced. Neither timeout
  nor recovery automatically replays a command.
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
- After native birth and metadata registration, the parent automatically reaps
  each child on exit, even if its startup response read is blocked or an MCP
  server is idle. External CLI stop can confirm exit without waiting for that
  worker or server shutdown. Dedicated waiters hold no transaction lease, send
  no signals, and share the original `Popen` exit-status reader with permitted
  inspection.

## Native Qt/Lua Smoke

`make native-smoke` is an opt-in integration check, not a unit test or gameplay
benchmark. It requires a real Qt frontend built from upstream mGBA commit
[`543a197582c30364584d773a974d7f991892fa43`](https://github.com/mgba-emu/mgba/tree/543a197582c30364584d773a974d7f991892fa43)
(reports `0.11.0`). A program named `mgba`, or a version string alone, is not proof
of Lua support: the smoke must actually load the packaged bridge and execute Lua.
The stock macOS Qt 0.10.5 app was tested and rejected: it has no `--script` option.
PNG validation uses Pillow at runtime; the optional `native` group pins its version
for reproducible smoke checks. The `native/` scripts are type-checked when selecting
`make native-smoke`, separately from the default offline type-check paths.

Bridge JSON is serialized before opening a same-directory temporary file, then
published by rename only after successful write and close. Failed publication
preserves the prior complete snapshot. The native smoke includes a deterministic
Lua I/O gate: while the writer is held after open, the previous heartbeat must
remain readable; after release, a newer complete JSON snapshot must appear.
This does not replace or retry the normal liveness/heartbeat assertion.
Pre-cleanup diagnostics copy published files, not the bridge's transient
`heartbeat.json.tmp` and `response.json.tmp` scratch files.

### Clean Ubuntu 22.04 machine

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), clone this
repository, and run the following from its root:

```bash
sudo apt-get update
sudo apt-get install --no-install-recommends -y build-essential cmake git pkg-config \
  qtbase5-dev qtmultimedia5-dev qttools5-dev qttools5-dev-tools \
  liblua5.4-dev libpng-dev zlib1g-dev libsqlite3-dev libepoxy-dev xvfb xauth
uv python install 3.11
uv sync --python 3.11 --frozen --group dev --group native
make native-build
make test-rom
QT_QPA_PLATFORM=xcb LIBGL_ALWAYS_SOFTWARE=1 QT_X11_NO_MITSHM=1 \
  xvfb-run --auto-servernum --error-file=.native/xvfb.log \
  --server-args='-screen 0 1024x768x24 -nolisten tcp' \
  make native-smoke
```

The build helper fetches the exact commit into a **new** `.native/mgba/` directory
and builds the `mgba-qt` target there; it does not install over another emulator.
It also fetches the pinned 0.10.5 commit solely for Linux AppStream release metadata.
Required options include `BUILD_QT=ON`, `FORCE_QT_VERSION=5`,
`ENABLE_SCRIPTING=ON`, `USE_LUA=5.4`, `USE_PNG=ON`, and `USE_ZLIB=ON`.
The full options are in `native/provision.py` and retained `commands.json`
and `CMakeCache.txt`. Keep the native Qt SQLite library feature enabled: this pin's
Qt initialization crashes with it disabled. This is mGBA's existing local library,
not a new server database or service.

The helper checks compiled Lua/scripting/PNG flags and the Qt `--script` option.
The smoke then proves actual API availability, callback delivery and rendering.
Build provenance retains the exact source commit, executable SHA-256, compiler
configuration, linked libraries, OS package versions (Linux), and version output.
`--build-provenance` verifies that the smoke uses that executable. The ROM cache
is keyed by the pinned checksum and verified even on a cache hit; corrupt cached
ROMs fail rather than becoming successful skips.

Builds refuse to reuse an existing output directory. For another independent
build use `uv run python native/provision.py --root /new/build/path` and
set `MGBA_PATH=/new/build/path/build/qt/mgba-qt` and
`NATIVE_PROVENANCE=/new/build/path/provenance.json` on `make native-smoke`.
`NATIVE_ARTIFACTS=/new/results/path` chooses a new diagnostic directory; the default
is timestamped under `.native/`. Never point it at an existing user runtime.

### What it checks and retains

Each of two runs gets a private HOME, runtime, ROM/save copy and explicitly named
owned session. The CLI starts it, reads live heartbeat/status, loads metadata-only
Lua and taps A. A real Lua callback observes both press and automatic release.
The real MCP stdio client uses the **same session ID** to read Lua metadata, run a
small B-key callback macro, observe its press/release, and check that the callback
is removed with no further input across at least 30 subsequent callback deliveries.
The visual tool returns an image that Pillow fully decodes to 160×144 RGB pixels;
a PNG signature/base64 string alone or a blank image fails.

The normal run stops through MCP and verifies the recorded process identity is
dead. The second deliberately raises an error after decoding the image, before
normal stop; `finally` must observe the still-live owned process, stop that exact
session, and confirm death. Any unexpected failure or failed cleanup fails the
whole invocation. No executable-name kill, global runtime sweep, fake emulator,
or green missing-prerequisite skip is used.

Both runs retain command results (CLI and MCP), native stdout/stderr, heartbeat
snapshots, bridge files/journals, a pre-cleanup runtime snapshot, decoded PNGs,
pixel hashes, provenance and final outcomes. Nothing is deleted from the
diagnostic directory. The separate `native-qt-lua` CI job always uploads these
diagnostics for **14 days**, including failed runs; ROMs and saves are excluded.
Source fetch/configure phases have 120-second limits, compilation 15 minutes,
readiness 15 seconds, macro/input observation phases 10 seconds, each MCP request
15 seconds, and the complete two-scenario smoke 120 seconds plus bounded cleanup.
The CI job has a 30-minute outer deadline and uses Xvfb's explicit X11 backend.

This proves callback progress and observed key transitions, **not exact
pause/step/frame semantics**. The logged native `currentFrame()` and bridge
callback counters are not assumed equivalent. Capability metadata also records
the loaded platform and state-method/flag availability; it does not prove
save/load-state or OAM semantics.

### Native CI build cache

The native job caches the complete `.native/mgba/` tree, including the Qt
executable, shared libraries, build/runtime assets, source checkout and original
provenance/logs. It uses an exact key: Ubuntu 22.04 and architecture, upstream
commit, build recipe/Makefile/workflow hashes, workspace, compiler/build inputs
and a sorted installed-package version inventory. This intentionally favors
conservative invalidation over reusing a potentially incompatible native build.
There are no broad restore keys; apt progress logs do not enter the fingerprint.

Only `make native-build` is skipped on an exact hit. Every job still runs the
unchanged native smoke, checking the executable version's source pin, recorded
build commit and executable SHA-256 before both real CLI/MCP scenarios, PNG
decoding, the atomic-publication regression and confirmed process cleanup.
The uv dependency cache is enabled using `uv.lock`; the separate checksum-keyed
ROM cache remains unchanged. `cache-inputs.txt` and `cache-status.log` are retained
with the native diagnostics.

Observed on the same `f52926b` head in
[run 34775213953](https://github.com/penandlim/mgba-live-mcp/actions/runs/34775213953):
attempt 1 built and saved the cache in **176 seconds** (128 seconds building);
attempt 2 restored the exact key in **1 second**, skipped building, and completed
in **55 seconds**. Both full native scenarios passed on both attempts. This
observed pair saved 121 seconds (about 69%); hosted-runner timings vary.

### Tested native matrix

| OS / architecture | Native build | Display | Evidence / status |
| --- | --- | --- | --- |
| Ubuntu 22.04 x86-64 | Same pin; Qt 5.15.3, Lua 5.4.4, GCC 11.4.0, CMake 3.22.1 | Xvfb + xcb + Mesa software GL | Verified: both real sequences and retained artifacts; [native workflow runs](https://github.com/penandlim/mgba-live-mcp/actions/workflows/native-smoke.yml) |
| macOS 26.6.2 arm64 | Same pin; Qt 5.15.18, Lua 5.4.8, LLVM 22.1.4, CMake 4.3.2 | Cocoa | Verified: both real sequences, decoded pixels, confirmed cleanup |
| macOS stock Qt 0.10.5 | Official app at `26b7884…` | Cocoa | Rejected: `--script` unavailable |
| Windows; other OS/native versions; Lua 5.5 | Not exercised by this smoke | — | Unverified; no support claim (Windows managed process ownership is unsupported) |

The verified macOS build used `CC`/`CXX` from Homebrew LLVM, an explicit
`SDKROOT="$(xcrun --show-sdk-path)"`, and `CMAKE_PREFIX_PATH` containing Qt5 and
Lua5.4 prefixes (colon-separated). Run with `QT_QPA_PLATFORM=cocoa`; set
`MGBA_PATH` to `<build>/qt/mGBA.app/Contents/MacOS/mGBA`. Do not substitute a
Homebrew `lua@5.4` symlink without checking its headers/version: a local installation
was actually Lua5.5. The exercised Lua5.4.8 source archive is
[`lua-5.4.8.tar.gz`](https://www.lua.org/ftp/lua-5.4.8.tar.gz), SHA-256
`4f18ddae154e793e46eeab727c59ef1c0c0c2b744e7b94219710d76f530629ae`
([upstream checksums](https://www.lua.org/ftp/)); it was built with
`make macosx install INSTALL_TOP=<private-prefix>`. Ubuntu instructions above
remain the automated clean-machine reference.

### Public ROM attribution and redistribution

The existing helper downloads **µCity 1.3**, by **Antonio Niño Díaz
(AntonioND/SkyLyrac)**, directly from the
[upstream release](https://github.com/AntonioND/ucity/releases/tag/v1.3).
The pinned `ucity.gbc` SHA-256 is
`9422ee2ca7b7ea1d46b58b2a429fff3f354dfd3e732dee1e7ae6220f148ce6e0`.
[The release's own notices](https://github.com/AntonioND/ucity/blob/v1.3/readme.rst)
license the game under **GPL-3.0-or-later**, graphics/music under **CC-BY-SA-4.0**,
and identify separately licensed components such as **BSD-2-Clause GBT Player**.
The [GPL text](https://github.com/AntonioND/ucity/blob/v1.3/gpl-3.0.txt) and
[corresponding source and component notices](https://github.com/AntonioND/ucity/tree/v1.3)
are available at the pinned tag.

Do not redistribute the ROM as if it were MIT-licensed repository code: retain
the copyright/license notices and provide Corresponding Source as required by
GPLv3, including applicable component notices. Redistributed screenshots of the
game's graphics must retain attribution and the
[CC-BY-SA-4.0 terms](https://creativecommons.org/licenses/by-sa/4.0/).
Diagnostic provenance carries this attribution/source link alongside the images.
Commercial ROMs are never used; diagnostic uploads do not redistribute the ROM,
save data or mGBA binaries.

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
- The packaged bridge contract tests use a system Lua interpreter (`lua`, `lua5.4`
  or `luajit`). CI installs Lua 5.4; without one, only these interpreter tests skip.

## Release Checklist

1. Set the release version in `pyproject.toml`; `__version__` and MCP initialization
   read the installed distribution metadata rather than a second release string.
2. Add release notes in `CHANGELOG.md`.
3. Run local checks:
`uv sync --group dev && make check && uv build`
4. Trigger TestPyPI publish workflow (`publish-testpypi`) and verify install from TestPyPI.
5. Push tag `v0.5.0` to trigger the PyPI release workflow.
6. Smoke test:
`uvx mgba-live-mcp` and `uvx --from git+https://github.com/penandlim/mgba-live-mcp mgba-live-mcp`.
