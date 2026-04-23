# MCP Tool Reference

This file is auto-generated from the server's `tools/list` metadata.
Do not edit manually. Regenerate with:

```bash
make mcp-docs
```

- Tool count: 15
- Tools: `mgba_live_start`, `mgba_live_start_with_lua`, `mgba_live_attach`, `mgba_live_status`, `mgba_live_stop`, `mgba_live_run_lua`, `mgba_live_input_tap`, `mgba_live_input_set`, `mgba_live_input_clear`, `mgba_live_export_screenshot`, `mgba_live_read_memory`, `mgba_live_read_range`, `mgba_live_dump_pointers`, `mgba_live_dump_oam`, `mgba_live_dump_entities`

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
      "description": "Explicit fpsTarget. Defaults to 120 when omitted.",
      "type": "number"
    },
    "mgba_path": {
      "description": "Optional mgba-qt path.",
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
      "description": "Command timeout in seconds.",
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

Start a live session, run Lua immediately, then return the post-Lua screenshot.

- Required input fields: `rom`

### Input Schema

```json
{
  "properties": {
    "code": {
      "description": "Inline Lua code.",
      "type": "string"
    },
    "fast": {
      "description": "Shortcut for fps_target=600.",
      "type": "boolean"
    },
    "file": {
      "description": "Lua file path.",
      "type": "string"
    },
    "fps_target": {
      "description": "Explicit fpsTarget. Defaults to 120 when omitted.",
      "type": "number"
    },
    "mgba_path": {
      "description": "Optional mgba-qt path.",
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
      "description": "Command timeout in seconds.",
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

### Input Schema

```json
{
  "properties": {
    "pid": {
      "description": "PID of a managed session.",
      "type": "integer"
    },
    "session": {
      "description": "Session id.",
      "type": "string"
    },
    "timeout": {
      "default": 20.0,
      "description": "Command timeout in seconds.",
      "type": "number"
    }
  },
  "type": "object"
}
```

### Output Schema

_Not declared._

## `mgba_live_status`

Show status for one session or all managed sessions.

- Required input fields: _Conditional; see schema._

### Input Schema

```json
{
  "anyOf": [
    {
      "required": [
        "session"
      ]
    },
    {
      "properties": {
        "all": {
          "const": true
        }
      },
      "required": [
        "all"
      ]
    }
  ],
  "properties": {
    "all": {
      "description": "Whether to include all sessions.",
      "type": "boolean"
    },
    "session": {
      "description": "Session id. Required unless all=true.",
      "type": "string"
    },
    "timeout": {
      "default": 20.0,
      "description": "Command timeout in seconds.",
      "type": "number"
    }
  },
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
      "description": "Command timeout in seconds.",
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

Execute Lua in a running live session.

- Required input fields: _Conditional; see schema._

### Input Schema

```json
{
  "oneOf": [
    {
      "required": [
        "file"
      ]
    },
    {
      "required": [
        "code"
      ]
    }
  ],
  "properties": {
    "code": {
      "description": "Inline Lua code.",
      "type": "string"
    },
    "file": {
      "description": "Lua file path.",
      "type": "string"
    },
    "session": {
      "description": "Session id.",
      "type": "string"
    },
    "timeout": {
      "default": 20.0,
      "description": "Command timeout in seconds.",
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

Tap a key for N frames, optionally wait additional frames after release, then return a screenshot.

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
      "description": "Command timeout in seconds.",
      "type": "number"
    },
    "wait_frames": {
      "default": 0,
      "description": "Additional frames to wait after tap release before screenshot capture.",
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

Set currently held keys for live session. Use mgba_live_status after input for visual verification.

- Required input fields: `session`, `keys`

### Input Schema

```json
{
  "properties": {
    "keys": {
      "description": "Keys to hold.",
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
      "description": "Command timeout in seconds.",
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

Clear held keys from live session. Use mgba_live_status after input for visual verification.

- Required input fields: `session`

### Input Schema

```json
{
  "properties": {
    "keys": {
      "description": "Optional keys to clear; omit to clear all.",
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
      "description": "Command timeout in seconds.",
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

Export a screenshot from a live session.

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
      "description": "Command timeout in seconds.",
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

Read memory addresses from live session.

- Required input fields: `session`, `addresses`

### Input Schema

```json
{
  "properties": {
    "addresses": {
      "description": "Addresses to read.",
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
      "description": "Command timeout in seconds.",
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

Read contiguous memory range from live session.

- Required input fields: `session`, `start`, `length`

### Input Schema

```json
{
  "properties": {
    "length": {
      "description": "Byte length.",
      "type": "integer"
    },
    "session": {
      "type": "string"
    },
    "start": {
      "description": "Range start address.",
      "type": "integer"
    },
    "timeout": {
      "default": 20.0,
      "description": "Command timeout in seconds.",
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

Dump pointer table entries from live session.

- Required input fields: `session`, `start`, `count`

### Input Schema

```json
{
  "properties": {
    "count": {
      "description": "Entries to read.",
      "type": "integer"
    },
    "session": {
      "type": "string"
    },
    "start": {
      "description": "Pointer table start address.",
      "type": "integer"
    },
    "timeout": {
      "default": 20.0,
      "description": "Command timeout in seconds.",
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

Dump OAM entries from live session.

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
      "description": "Command timeout in seconds.",
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

Dump structured entity bytes from live session.

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
      "description": "Command timeout in seconds.",
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
