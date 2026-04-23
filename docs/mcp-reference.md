# MCP Tool Reference

This file is auto-generated from the server's `tools/list` metadata.
Do not edit manually. Regenerate with:

```bash
make mcp-docs
```

- Tool count: 19
- Tools: `mgba_live_start`, `mgba_live_start_with_lua`, `mgba_live_start_with_lua_and_view`, `mgba_live_attach`, `mgba_live_status`, `mgba_live_get_view`, `mgba_live_stop`, `mgba_live_run_lua`, `mgba_live_run_lua_and_view`, `mgba_live_input_tap`, `mgba_live_input_tap_and_view`, `mgba_live_input_set`, `mgba_live_input_clear`, `mgba_live_export_screenshot`, `mgba_live_read_memory`, `mgba_live_read_range`, `mgba_live_dump_pointers`, `mgba_live_dump_oam`, `mgba_live_dump_entities`

## `mgba_live_start`

Start a persistent live mGBA session.

- Required input fields: `rom`

### Input Schema

```json
{
  "properties": {
    "fast": {
      "description": "Shortcut for fps_target=600.",
      "type": "boolean"
    },
    "fps_target": {
      "description": "Explicit fpsTarget.",
      "type": "number"
    },
    "mgba_path": {
      "description": "Optional mGBA binary path.",
      "type": "string"
    },
    "rom": {
      "description": "Path to ROM (.gba/.gb/.gbc).",
      "type": "string"
    },
    "savestate": {
      "description": "Optional savestate path.",
      "type": "string"
    },
    "session_id": {
      "description": "Optional explicit session id.",
      "type": "string"
    },
    "timeout": {
      "default": 20.0,
      "type": "number"
    }
  },
  "required": [
    "rom"
  ],
  "type": "object"
}
```

### Output Schema

_Not declared._

## `mgba_live_start_with_lua`

Start a live session and run Lua immediately. Metadata only.

- Required input fields: `rom`
- Runtime argument rule: Provide exactly one of `file` or `code`.

### Input Schema

```json
{
  "properties": {
    "code": {
      "description": "Inline Lua code. Provide exactly one of file or code.",
      "type": "string"
    },
    "fast": {
      "description": "Shortcut for fps_target=600.",
      "type": "boolean"
    },
    "file": {
      "description": "Lua file path. Provide exactly one of file or code.",
      "type": "string"
    },
    "fps_target": {
      "description": "Explicit fpsTarget.",
      "type": "number"
    },
    "mgba_path": {
      "description": "Optional mGBA binary path.",
      "type": "string"
    },
    "rom": {
      "description": "Path to ROM (.gba/.gb/.gbc).",
      "type": "string"
    },
    "savestate": {
      "description": "Optional savestate path.",
      "type": "string"
    },
    "session_id": {
      "description": "Optional explicit session id.",
      "type": "string"
    },
    "timeout": {
      "default": 20.0,
      "type": "number"
    }
  },
  "required": [
    "rom"
  ],
  "type": "object"
}
```

### Output Schema

_Not declared._

## `mgba_live_start_with_lua_and_view`

Start a live session, run Lua, settle, and return one screenshot.

- Required input fields: `rom`
- Runtime argument rule: Provide exactly one of `file` or `code`.

### Input Schema

```json
{
  "properties": {
    "code": {
      "description": "Inline Lua code. Provide exactly one of file or code.",
      "type": "string"
    },
    "fast": {
      "description": "Shortcut for fps_target=600.",
      "type": "boolean"
    },
    "file": {
      "description": "Lua file path. Provide exactly one of file or code.",
      "type": "string"
    },
    "fps_target": {
      "description": "Explicit fpsTarget.",
      "type": "number"
    },
    "mgba_path": {
      "description": "Optional mGBA binary path.",
      "type": "string"
    },
    "rom": {
      "description": "Path to ROM (.gba/.gb/.gbc).",
      "type": "string"
    },
    "savestate": {
      "description": "Optional savestate path.",
      "type": "string"
    },
    "session_id": {
      "description": "Optional explicit session id.",
      "type": "string"
    },
    "timeout": {
      "default": 20.0,
      "type": "number"
    }
  },
  "required": [
    "rom"
  ],
  "type": "object"
}
```

### Output Schema

_Not declared._

## `mgba_live_attach`

Attach to an existing managed live session.

- Required input fields: _None._
- Runtime argument rule: Provide `session` or `pid`.

### Input Schema

```json
{
  "properties": {
    "pid": {
      "description": "PID of a managed session. Provide session or pid (at least one).",
      "type": "integer"
    },
    "session": {
      "description": "Session id. Provide session or pid (at least one required).",
      "type": "string"
    },
    "timeout": {
      "default": 20.0,
      "type": "number"
    }
  },
  "type": "object"
}
```

### Output Schema

_Not declared._

## `mgba_live_status`

Show metadata for one session or all managed sessions.

- Required input fields: _None._
- Runtime argument rule: Provide `session`, or set `all=true`.

### Input Schema

```json
{
  "properties": {
    "all": {
      "description": "If true, list all sessions. Otherwise pass session.",
      "type": "boolean"
    },
    "session": {
      "description": "Session id for one session. Or set all=true for every session.",
      "type": "string"
    },
    "timeout": {
      "default": 20.0,
      "type": "number"
    }
  },
  "type": "object"
}
```

### Output Schema

_Not declared._

## `mgba_live_get_view`

Capture one in-memory screenshot from a live session.

- Required input fields: `session`

### Input Schema

```json
{
  "properties": {
    "session": {
      "description": "Session id.",
      "type": "string"
    },
    "timeout": {
      "default": 20.0,
      "type": "number"
    }
  },
  "required": [
    "session"
  ],
  "type": "object"
}
```

### Output Schema

_Not declared._

## `mgba_live_stop`

Stop one managed session.

- Required input fields: `session`

### Input Schema

```json
{
  "properties": {
    "grace": {
      "description": "Kill grace period in seconds.",
      "type": "number"
    },
    "session": {
      "description": "Session id to stop.",
      "type": "string"
    },
    "timeout": {
      "default": 20.0,
      "type": "number"
    }
  },
  "required": [
    "session"
  ],
  "type": "object"
}
```

### Output Schema

_Not declared._

## `mgba_live_run_lua`

Execute Lua in a running live session. Metadata only.

- Required input fields: `session`
- Runtime argument rule: Provide exactly one of `file` or `code`.

### Input Schema

```json
{
  "properties": {
    "code": {
      "description": "Inline Lua code. Provide exactly one of file or code.",
      "type": "string"
    },
    "file": {
      "description": "Lua file path. Provide exactly one of file or code.",
      "type": "string"
    },
    "session": {
      "description": "Session id.",
      "type": "string"
    },
    "timeout": {
      "default": 20.0,
      "type": "number"
    }
  },
  "required": [
    "session"
  ],
  "type": "object"
}
```

### Output Schema

_Not declared._

## `mgba_live_run_lua_and_view`

Execute Lua, settle, and return one screenshot.

- Required input fields: `session`
- Runtime argument rule: Provide exactly one of `file` or `code`.

### Input Schema

```json
{
  "properties": {
    "code": {
      "description": "Inline Lua code. Provide exactly one of file or code.",
      "type": "string"
    },
    "file": {
      "description": "Lua file path. Provide exactly one of file or code.",
      "type": "string"
    },
    "session": {
      "description": "Session id.",
      "type": "string"
    },
    "timeout": {
      "default": 20.0,
      "type": "number"
    }
  },
  "required": [
    "session"
  ],
  "type": "object"
}
```

### Output Schema

_Not declared._

## `mgba_live_input_tap`

Tap a key for N frames. Metadata only.

- Required input fields: `session`, `key`

### Input Schema

```json
{
  "properties": {
    "frames": {
      "default": 1,
      "type": "integer"
    },
    "key": {
      "description": "A/B/START/SELECT/UP/DOWN/LEFT/RIGHT/L/R.",
      "type": "string"
    },
    "session": {
      "type": "string"
    },
    "timeout": {
      "default": 20.0,
      "type": "number"
    }
  },
  "required": [
    "session",
    "key"
  ],
  "type": "object"
}
```

### Output Schema

_Not declared._

## `mgba_live_input_tap_and_view`

Tap a key, optionally wait additional frames, then return one screenshot.

- Required input fields: `session`, `key`

### Input Schema

```json
{
  "properties": {
    "frames": {
      "default": 1,
      "type": "integer"
    },
    "key": {
      "description": "A/B/START/SELECT/UP/DOWN/LEFT/RIGHT/L/R.",
      "type": "string"
    },
    "session": {
      "type": "string"
    },
    "timeout": {
      "default": 20.0,
      "type": "number"
    },
    "wait_frames": {
      "default": 0,
      "minimum": 0,
      "type": "integer"
    }
  },
  "required": [
    "session",
    "key"
  ],
  "type": "object"
}
```

### Output Schema

_Not declared._

## `mgba_live_input_set`

Set currently held keys for a live session.

- Required input fields: `session`, `keys`

### Input Schema

```json
{
  "properties": {
    "keys": {
      "items": {
        "type": "string"
      },
      "type": "array"
    },
    "session": {
      "type": "string"
    },
    "timeout": {
      "default": 20.0,
      "type": "number"
    }
  },
  "required": [
    "session",
    "keys"
  ],
  "type": "object"
}
```

### Output Schema

_Not declared._

## `mgba_live_input_clear`

Clear held keys from a live session.

- Required input fields: `session`

### Input Schema

```json
{
  "properties": {
    "keys": {
      "items": {
        "type": "string"
      },
      "type": "array"
    },
    "session": {
      "type": "string"
    },
    "timeout": {
      "default": 20.0,
      "type": "number"
    }
  },
  "required": [
    "session"
  ],
  "type": "object"
}
```

### Output Schema

_Not declared._

## `mgba_live_export_screenshot`

Persist and return a screenshot from a live session.

- Required input fields: `session`

### Input Schema

```json
{
  "properties": {
    "out": {
      "description": "Optional persisted PNG output path.",
      "type": "string"
    },
    "session": {
      "description": "Session id.",
      "type": "string"
    },
    "timeout": {
      "default": 20.0,
      "type": "number"
    }
  },
  "required": [
    "session"
  ],
  "type": "object"
}
```

### Output Schema

_Not declared._

## `mgba_live_read_memory`

Read memory addresses from a live session.

- Required input fields: `session`, `addresses`

### Input Schema

```json
{
  "properties": {
    "addresses": {
      "items": {
        "type": "integer"
      },
      "type": "array"
    },
    "session": {
      "type": "string"
    },
    "timeout": {
      "default": 20.0,
      "type": "number"
    }
  },
  "required": [
    "session",
    "addresses"
  ],
  "type": "object"
}
```

### Output Schema

_Not declared._

## `mgba_live_read_range`

Read a contiguous memory range from a live session.

- Required input fields: `session`, `start`, `length`

### Input Schema

```json
{
  "properties": {
    "length": {
      "type": "integer"
    },
    "session": {
      "type": "string"
    },
    "start": {
      "type": "integer"
    },
    "timeout": {
      "default": 20.0,
      "type": "number"
    }
  },
  "required": [
    "session",
    "start",
    "length"
  ],
  "type": "object"
}
```

### Output Schema

_Not declared._

## `mgba_live_dump_pointers`

Dump pointer table entries from a live session.

- Required input fields: `session`, `start`, `count`

### Input Schema

```json
{
  "properties": {
    "count": {
      "type": "integer"
    },
    "session": {
      "type": "string"
    },
    "start": {
      "type": "integer"
    },
    "timeout": {
      "default": 20.0,
      "type": "number"
    },
    "width": {
      "default": 4,
      "type": "integer"
    }
  },
  "required": [
    "session",
    "start",
    "count"
  ],
  "type": "object"
}
```

### Output Schema

_Not declared._

## `mgba_live_dump_oam`

Dump OAM entries from a live session.

- Required input fields: `session`

### Input Schema

```json
{
  "properties": {
    "count": {
      "default": 40,
      "type": "integer"
    },
    "session": {
      "type": "string"
    },
    "timeout": {
      "default": 20.0,
      "type": "number"
    }
  },
  "required": [
    "session"
  ],
  "type": "object"
}
```

### Output Schema

_Not declared._

## `mgba_live_dump_entities`

Dump structured entity bytes from a live session.

- Required input fields: `session`

### Input Schema

```json
{
  "properties": {
    "base": {
      "default": 49664,
      "type": "integer"
    },
    "count": {
      "default": 10,
      "type": "integer"
    },
    "session": {
      "type": "string"
    },
    "size": {
      "default": 24,
      "type": "integer"
    },
    "timeout": {
      "default": 20.0,
      "type": "number"
    }
  },
  "required": [
    "session"
  ],
  "type": "object"
}
```

### Output Schema

_Not declared._
