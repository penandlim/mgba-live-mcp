# mgba-live-mcp

MCP server for persistent live control of mGBA using the script bridge workflow.
Commands return structured JSON, and visual snapshots are provided by
`mgba_live_status`/`mgba_live_attach`/`mgba_live_start_with_lua` and `mgba_live_export_screenshot`.

## Requirements

- Python `>=3.11` and `uv`
- A scriptable mGBA Qt build (`mGBA`/`mgba-qt`) with:
  - `BUILD_QT=ON`
  - `ENABLE_SCRIPTING=ON`
  - `USE_LUA=ON`
- Screenshot support (`libpng`/`zlib`, enabled by default in upstream mGBA builds)
- A ROM file (`.gba`/`.gb`/`.gbc`)

This project auto-detects the emulator binary from `PATH` in this order:
`mgba-qt`, `mgba`, `mGBA`.
If your binary is not on `PATH`, pass `mgba_path` to `mgba_live_start` / `mgba_live_start_with_lua`.

## Installation

### 1) Install Python deps for this MCP server

```bash
cd /path/to/mgba-live-mcp
uv sync
```

### 2) Build mGBA with scripting + Qt (macOS example)

Set source/build locations first:

```bash
export MGBA_SRC=/path/to/mgba-source
export MGBA_BUILD="$MGBA_SRC/build-qtllvm"
```

Then build:

```bash
brew install cmake ffmpeg libzip qt@5 sdl2 libedit lua pkg-config

cd "$MGBA_SRC"
cmake -S . -B "$MGBA_BUILD" \
  -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_PREFIX_PATH="$(brew --prefix qt@5)" \
  -DBUILD_QT=ON \
  -DENABLE_SCRIPTING=ON \
  -DUSE_LUA=ON \
  -DBUILD_HEADLESS=ON \
  -DBUILD_SDL=OFF \
  -DBUILD_SHARED=ON \
  -DBUILD_STATIC=OFF
cmake --build "$MGBA_BUILD" -j"$(sysctl -n hw.ncpu)"
```

### 3) Verify the build has required capabilities

```bash
rg -n "^(BUILD_QT|ENABLE_SCRIPTING|USE_LUA):BOOL=ON" \
  "$MGBA_BUILD/CMakeCache.txt"

"$MGBA_BUILD/qt/mGBA.app/Contents/MacOS/mGBA" --help | rg -- "--script"
```

### 4) Make the binary discoverable (optional)

If you want auto-detection to work without passing `mgba_path` every time:

```bash
ln -sf "$MGBA_BUILD/qt/mGBA.app/Contents/MacOS/mGBA" /usr/local/bin/mgba-qt
```

Or set `mgba_path` per tool call, for example:

```json
{
  "rom": "/path/to/game.gba",
  "mgba_path": "/absolute/path/to/mGBA"
}
```

## Features

- Start/attach/stop/manage long-lived `mgba-qt` sessions.
- Frame-accurate input (`A/B/Start/Select/Up/Down/Left/Right/L/R`).
- Execute Lua against a running process.
- Read memory and pointer tables.
- Dump OAM and entity data.
- `mgba_live_input_tap` supports `wait_frames` and returns post-input screenshot/image.
- Use `mgba_live_status` after `mgba_live_input_set` / `mgba_live_input_clear` for post-input view checks.
- PNG screenshot exports with optional custom `out` path.

## Run

```bash
cd /path/to/mgba-live-mcp
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
  "/absolute/path/to/mgba-live-mcp",
  "mgba-live-mcp",
]
```

Notes:
- Start defaults to `120` FPS unless `fps_target` is provided.
- `mgba_live_start` is MCP bootstrap-only and does not accept `script` or return a screenshot.
- `mgba_live_start_with_lua` starts a new session, runs `file` or `code`, then returns the post-Lua screenshot/image.
- CLI `scripts/mgba_live.py start` still supports `--script` startup Lua paths passed directly to mGBA.
- MCP tool text payloads return direct command fields (no `tool`/`command`/`result` wrapper).
- `mgba_live_input_tap` returns screenshot/image and accepts `wait_frames` (capture at `input_frame + tap_duration + wait_frames`).
- `mgba_live_input_set` / `mgba_live_input_clear` return action data; call `mgba_live_status` for visual assessment.
- `mgba_live_export_screenshot` is the screenshot tool name.
- Screenshot responses do not include encoded image text blocks.
- `mgba_live_export_screenshot` always writes and returns a PNG `path` (defaults to session screenshots directory when `out` is omitted).
- For callback-based Lua macros, return a table with `macro_key` (for example `{ status = "started", macro_key = "__my_macro" }`) and set `_G[macro_key].active = false` when done so `mgba_live_run_lua` can wait for completion before returning its snapshot.
