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

### Machine-readable protocol contracts

- Initialization reports the installed `mgba-live-mcp` package version, not the
  MCP SDK version. Every tool declares a success `outputSchema` and behavior hints.
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
- `not_started`, `partial` and `unknown` execution outcomes are not interchangeable:
  do not blindly retry a mutation after a partial composite or ambiguous timeout.
  See the generated [error inventory and schemas](docs/mcp-reference.md).
- Annotations are hints, not security controls. Status can archive dead sessions;
  attach updates the active marker; export can overwrite a file; Lua is unrestricted
  and is neither read-only nor safely retryable. Read/idempotence hints exclude
  bridge bookkeeping and do not imply that a running game's state is frozen.

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

## Native Qt/Lua Smoke

`make native-smoke` is an opt-in integration check, not a unit test or gameplay
benchmark. It requires a real Qt frontend built from upstream mGBA commit
[`543a197582c30364584d773a974d7f991892fa43`](https://github.com/mgba-emu/mgba/tree/543a197582c30364584d773a974d7f991892fa43)
(reports `0.11.0`). A program named `mgba`, or a version string alone, is not proof
of Lua support: the smoke must actually load the packaged bridge and execute Lua.
The stock macOS Qt 0.10.5 app was tested and rejected: it has no `--script` option.
Native-only Python dependencies stay in the optional `native` group. The
`native/` scripts are type-checked when selecting `make native-smoke`, separately
from the default offline type-check paths.

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
