# MCP Tool Reference

This file is auto-generated from the server's `tools/list` metadata.
Do not edit manually. Regenerate with:

```bash
make mcp-docs
```

- Tool count: 19
- Tools: `mgba_live_start`, `mgba_live_start_with_lua`, `mgba_live_start_with_lua_and_view`, `mgba_live_attach`, `mgba_live_status`, `mgba_live_get_view`, `mgba_live_stop`, `mgba_live_run_lua`, `mgba_live_run_lua_and_view`, `mgba_live_input_tap`, `mgba_live_input_tap_and_view`, `mgba_live_input_set`, `mgba_live_input_clear`, `mgba_live_export_screenshot`, `mgba_live_read_memory`, `mgba_live_read_range`, `mgba_live_dump_pointers`, `mgba_live_dump_oam`, `mgba_live_dump_entities`

## Result and error contracts

Success `structuredContent` matches the tool's output schema; the first text
block is the same object encoded as compact JSON. Images appear only in
image blocks, never in that JSON. Existing heterogeneous fields are retained:
`status(all=true)` uses `value`, Lua uses `data`/`lua`, and command `frame`
is distinct from `screenshot.frame`. A nullable frame reports absence of a
bridge counter, not an inferred or substituted screenshot frame.
Lua `return nil` is explicitly encoded as `data.result: null` (or startup
`lua: null`); `return {}` retains the bridge's existing empty-array encoding.

Failures have `isError=true` and matching JSON text/structured content:
`{error: {code, message, phase, execution_outcome, ...context}}`.
Context includes `tool` (MCP) or `command` (CLI), `session_id`, `pid`,
bridge `request_id`, and `mcp_request_id` when known. A blocking prior
request is identified separately by `pending_request_id`, not confused
with a refused request's execution outcome. CLI failures print this
envelope to stderr and exit nonzero. Error envelopes are not successes
and are not validated against the success output schema.
Tool argument objects reject unknown fields with `invalid_arguments`.
Malformed MCP request envelopes, such as argument arrays, are rejected
by the SDK with JSON-RPC `-32602` before tool dispatch.

`execution_outcome` is domain-owned: `not_executed` requires proof of
nonexecution, `completed` preserves known command completion even when
capture/result handling fails, and `unknown` means execution or settling
cannot be established. `command_completed` and `command_request_id` retain
primary mutation evidence separately from a failed capture/poll `request_id`.
Snapshot/settle failures retain `cause_code`, `cause_phase`, and
`cause_execution_outcome`. Known bridge failures remain primary if their
completion crosses the deadline; `completion_error` records that failure.
Never replay a mutation just to recover its result. Phases identify the
actual failing stage, including acquisition, startup, dispatch, command,
settle, snapshot, result, or native inspection/TERM/KILL.

A timeout must be a finite positive number, never a boolean. One monotonic
budget covers validation, worker queueing, acquisition, startup, execution,
settling, capture, and result assembly. Nested caps never renew it;
wall-clock jumps do not affect it. CLI `start --ready-timeout` covers all
startup phases; CLI attach/status also accept `--timeout`.

Timeout is not cancellation. Only an atomically proven pending owned
request can be withdrawn using the bridge's `rename-v1` claim protocol.
Running/ambiguous work, legacy bridges, and interrupted composites stay
fenced. Cancelling an MCP caller does not release a running worker's lease.
There is no automatic replay. Use independently bounded stop/recovery.

Deadlines are cooperative: safety cleanup can finish after expiry, with
each journal-state lock wait bounded to 0.5 seconds; filesystem/native
syscalls cannot be preempted. Stop has no operation `timeout`: its separate
nonnegative `grace` allows TERM, plus at most one second for escalation
and exit confirmation. Ownership lock acquisition is nonblocking.

Annotations are hints, not security enforcement. Read-only/idempotent
hints describe requested domain effects, excluding bridge bookkeeping;
live emulation continues, so repeated reads need not return identical data.
Status performs maintenance, attach changes the active marker, Lua is
unrestricted, and screenshot export may overwrite files.

### Stable error codes

| Code | Meaning |
| --- | --- |
| `unknown_tool` | The requested tool is not in the catalog; nothing was invoked. |
| `invalid_arguments` | Arguments are malformed or violate an operation's input rules. |
| `session_required` | An explicit session (or supported PID/all selector) is required. |
| `session_not_found` | The requested managed session does not exist. |
| `session_dead` | The managed process/group has exited. |
| `session_exists` | The requested session directory is already reserved. |
| `session_busy` | Another operation or unresolved request owns the session. |
| `session_stopping` | Recovery has fenced the session while termination is unresolved. |
| `session_stopped` | Recovery has retired this session generation. |
| `session_generation_changed` | The session directory or ownership generation changed. |
| `session_state_corrupt` | The transaction journal cannot establish safe ownership. |
| `identity_unverified` | Native process ownership could not be established. |
| `identity_mismatch` | The process identity is not the managed process/group. |
| `permission_denied` | OS permissions prevent the requested operation or inspection. |
| `termination_unconfirmed` | Process/group exit could not be confirmed. |
| `bridge_error` | The bridge reported a command error; side effects may have occurred. |
| `serialization_failed` | Lua response could not be serialized as JSON; inspect command_completed before retrying. |
| `command_timeout` | The operation budget expired; inspect execution_outcome before retrying. |
| `settle_failed` | The command ran, but settling could not be confirmed. |
| `snapshot_failed` | Required visual content is unavailable after the requested operation. |
| `startup_failed` | Startup/readiness failed; inspect the retained session before retrying. |
| `resource_not_found` | A requested ROM, Lua file, bridge script or executable is unavailable. |
| `io_error` | A filesystem or OS operation failed. |
| `invalid_result` | An operation returned content inconsistent with its success contract. |
| `internal_error` | An unexpected implementation failure occurred; outcome is not established. |

## `mgba_live_start`

Validate local inputs and transactionally start a session.

- Required input fields: `rom`

### Input Schema

```json
{
  "additionalProperties": false,
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
      "description": "Finite positive budget in seconds for the entire operation, including validation, worker queueing, execution, settling, capture, and result assembly as applicable. Expiry does not prove nonexecution; inspect execution_outcome before recovery.",
      "exclusiveMinimum": 0,
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

```json
{
  "additionalProperties": false,
  "properties": {
    "fps_target": {
      "title": "Fps Target",
      "type": "number"
    },
    "pid": {
      "title": "Pid",
      "type": "integer"
    },
    "session_dir": {
      "title": "Session Dir",
      "type": "string"
    },
    "session_id": {
      "title": "Session Id",
      "type": "string"
    },
    "status": {
      "const": "started",
      "title": "Status",
      "type": "string"
    }
  },
  "required": [
    "session_id",
    "status",
    "pid",
    "fps_target",
    "session_dir"
  ],
  "title": "Started",
  "type": "object"
}
```

### Behavior Annotations

```json
{
  "destructiveHint": true,
  "idempotentHint": false,
  "openWorldHint": true,
  "readOnlyHint": false
}
```

## `mgba_live_start_with_lua`

Start and run unrestricted Lua. Metadata only; not safely retryable.

- Required input fields: `rom`
- Runtime argument rule: Provide exactly one of `file` or `code`.

### Input Schema

```json
{
  "additionalProperties": false,
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
      "description": "Finite positive budget in seconds for the entire operation, including validation, worker queueing, execution, settling, capture, and result assembly as applicable. Expiry does not prove nonexecution; inspect execution_outcome before recovery.",
      "exclusiveMinimum": 0,
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

```json
{
  "$defs": {
    "JsonValue": {}
  },
  "additionalProperties": false,
  "properties": {
    "lua": {
      "$ref": "#/$defs/JsonValue"
    },
    "pid": {
      "anyOf": [
        {
          "type": "integer"
        },
        {
          "type": "null"
        }
      ],
      "title": "Pid"
    },
    "session_id": {
      "title": "Session Id",
      "type": "string"
    }
  },
  "required": [
    "session_id",
    "pid",
    "lua"
  ],
  "title": "StartupLua",
  "type": "object"
}
```

### Behavior Annotations

```json
{
  "destructiveHint": true,
  "idempotentHint": false,
  "openWorldHint": true,
  "readOnlyHint": false
}
```

## `mgba_live_start_with_lua_and_view`

Start, run unrestricted Lua, settle, capture. Not safely retryable.

- Required input fields: `rom`
- Runtime argument rule: Provide exactly one of `file` or `code`.

### Input Schema

```json
{
  "additionalProperties": false,
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
      "description": "Finite positive budget in seconds for the entire operation, including validation, worker queueing, execution, settling, capture, and result assembly as applicable. Expiry does not prove nonexecution; inspect execution_outcome before recovery.",
      "exclusiveMinimum": 0,
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

```json
{
  "$defs": {
    "Frame": {
      "additionalProperties": false,
      "properties": {
        "frame": {
          "anyOf": [
            {
              "type": "integer"
            },
            {
              "type": "null"
            }
          ],
          "title": "Frame"
        }
      },
      "required": [
        "frame"
      ],
      "title": "Frame",
      "type": "object"
    },
    "JsonValue": {}
  },
  "additionalProperties": false,
  "properties": {
    "lua": {
      "$ref": "#/$defs/JsonValue"
    },
    "pid": {
      "anyOf": [
        {
          "type": "integer"
        },
        {
          "type": "null"
        }
      ],
      "title": "Pid"
    },
    "screenshot": {
      "$ref": "#/$defs/Frame"
    },
    "session_id": {
      "title": "Session Id",
      "type": "string"
    }
  },
  "required": [
    "session_id",
    "pid",
    "lua",
    "screenshot"
  ],
  "title": "StartupView",
  "type": "object"
}
```

### Behavior Annotations

```json
{
  "destructiveHint": true,
  "idempotentHint": false,
  "openWorldHint": true,
  "readOnlyHint": false
}
```

## `mgba_live_attach`

Attach to a managed session; updates the CLI active-session marker.

- Required input fields: _None._
- Runtime argument rule: Provide `session` or `pid`.

### Input Schema

```json
{
  "additionalProperties": false,
  "properties": {
    "pid": {
      "description": "PID of a managed session. Provide session or pid.",
      "type": "integer"
    },
    "session": {
      "description": "Session id. Provide session or pid.",
      "type": "string"
    },
    "timeout": {
      "default": 20.0,
      "description": "Finite positive budget in seconds for the entire operation, including validation, worker queueing, execution, settling, capture, and result assembly as applicable. Expiry does not prove nonexecution; inspect execution_outcome before recovery.",
      "exclusiveMinimum": 0,
      "type": "number"
    }
  },
  "type": "object"
}
```

### Output Schema

```json
{
  "additionalProperties": false,
  "properties": {
    "fps_target": {
      "title": "Fps Target",
      "type": "number"
    },
    "mgba_path": {
      "anyOf": [
        {
          "type": "string"
        },
        {
          "type": "null"
        }
      ],
      "title": "Mgba Path"
    },
    "pid": {
      "title": "Pid",
      "type": "integer"
    },
    "rom": {
      "title": "Rom",
      "type": "string"
    },
    "session_id": {
      "title": "Session Id",
      "type": "string"
    },
    "status": {
      "const": "attached",
      "title": "Status",
      "type": "string"
    }
  },
  "required": [
    "session_id",
    "status",
    "pid",
    "rom",
    "fps_target",
    "mgba_path"
  ],
  "title": "Attached",
  "type": "object"
}
```

### Behavior Annotations

```json
{
  "destructiveHint": false,
  "idempotentHint": true,
  "openWorldHint": false,
  "readOnlyHint": false
}
```

## `mgba_live_status`

Show metadata; archives dead sessions and refreshes the active marker.

- Required input fields: _None._
- Runtime argument rule: Provide `session`, or set `all=true`.

### Input Schema

```json
{
  "additionalProperties": false,
  "properties": {
    "all": {
      "description": "If true, list all sessions. Otherwise pass session.",
      "type": "boolean"
    },
    "session": {
      "description": "Session id for one session. Provide session, or set all=true.",
      "type": "string"
    },
    "timeout": {
      "default": 20.0,
      "description": "Finite positive budget in seconds for the entire operation, including validation, worker queueing, execution, settling, capture, and result assembly as applicable. Expiry does not prove nonexecution; inspect execution_outcome before recovery.",
      "exclusiveMinimum": 0,
      "type": "number"
    }
  },
  "type": "object"
}
```

### Output Schema

```json
{
  "$defs": {
    "JsonValue": {},
    "Status": {
      "additionalProperties": false,
      "properties": {
        "alive": {
          "title": "Alive",
          "type": "boolean"
        },
        "fps_target": {
          "title": "Fps Target",
          "type": "number"
        },
        "heartbeat": {
          "$ref": "#/$defs/JsonValue"
        },
        "identity_verified": {
          "title": "Identity Verified",
          "type": "boolean"
        },
        "is_active": {
          "title": "Is Active",
          "type": "boolean"
        },
        "mgba_path": {
          "anyOf": [
            {
              "type": "string"
            },
            {
              "type": "null"
            }
          ],
          "title": "Mgba Path"
        },
        "pid": {
          "title": "Pid",
          "type": "integer"
        },
        "process_state": {
          "title": "Process State",
          "type": "string"
        },
        "rom": {
          "title": "Rom",
          "type": "string"
        },
        "session_dir": {
          "title": "Session Dir",
          "type": "string"
        },
        "session_id": {
          "title": "Session Id",
          "type": "string"
        },
        "startup": {
          "anyOf": [
            {
              "additionalProperties": {
                "$ref": "#/$defs/JsonValue"
              },
              "type": "object"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Startup"
        },
        "transaction": {
          "anyOf": [
            {
              "additionalProperties": {
                "$ref": "#/$defs/JsonValue"
              },
              "type": "object"
            },
            {
              "type": "null"
            }
          ],
          "title": "Transaction"
        }
      },
      "required": [
        "session_id",
        "pid",
        "alive",
        "process_state",
        "identity_verified",
        "transaction",
        "rom",
        "fps_target",
        "mgba_path",
        "heartbeat",
        "is_active",
        "session_dir"
      ],
      "title": "Status",
      "type": "object"
    },
    "StatusList": {
      "additionalProperties": false,
      "properties": {
        "value": {
          "items": {
            "$ref": "#/$defs/Status"
          },
          "title": "Value",
          "type": "array"
        }
      },
      "required": [
        "value"
      ],
      "title": "StatusList",
      "type": "object"
    }
  },
  "anyOf": [
    {
      "$ref": "#/$defs/Status"
    },
    {
      "$ref": "#/$defs/StatusList"
    }
  ],
  "type": "object"
}
```

### Behavior Annotations

```json
{
  "destructiveHint": true,
  "idempotentHint": true,
  "openWorldHint": false,
  "readOnlyHint": false
}
```

## `mgba_live_get_view`

Capture a screenshot using a temporary file; no emulator mutation.

- Required input fields: `session`

### Input Schema

```json
{
  "additionalProperties": false,
  "properties": {
    "session": {
      "description": "Session id.",
      "type": "string"
    },
    "timeout": {
      "default": 20.0,
      "description": "Finite positive budget in seconds for the entire operation, including validation, worker queueing, execution, settling, capture, and result assembly as applicable. Expiry does not prove nonexecution; inspect execution_outcome before recovery.",
      "exclusiveMinimum": 0,
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

```json
{
  "$defs": {
    "Frame": {
      "additionalProperties": false,
      "properties": {
        "frame": {
          "anyOf": [
            {
              "type": "integer"
            },
            {
              "type": "null"
            }
          ],
          "title": "Frame"
        }
      },
      "required": [
        "frame"
      ],
      "title": "Frame",
      "type": "object"
    }
  },
  "additionalProperties": false,
  "properties": {
    "screenshot": {
      "$ref": "#/$defs/Frame"
    },
    "session_id": {
      "title": "Session Id",
      "type": "string"
    }
  },
  "required": [
    "session_id",
    "screenshot"
  ],
  "title": "View",
  "type": "object"
}
```

### Behavior Annotations

```json
{
  "destructiveHint": false,
  "idempotentHint": true,
  "openWorldHint": false,
  "readOnlyHint": true
}
```

## `mgba_live_stop`

Stop a managed group independently of operation deadlines; retire its generation. Repeated stop confirms exit.

- Required input fields: `session`

### Input Schema

```json
{
  "additionalProperties": false,
  "properties": {
    "grace": {
      "description": "Independent SIGTERM grace period in seconds before SIGKILL; not a total operation timeout.",
      "minimum": 0,
      "type": "number"
    },
    "session": {
      "description": "Session id to stop.",
      "type": "string"
    }
  },
  "required": [
    "session"
  ],
  "type": "object"
}
```

### Output Schema

```json
{
  "additionalProperties": false,
  "properties": {
    "alive_after": {
      "const": false,
      "title": "Alive After",
      "type": "boolean"
    },
    "alive_before": {
      "title": "Alive Before",
      "type": "boolean"
    },
    "cleanup_errors": {
      "items": {
        "type": "string"
      },
      "title": "Cleanup Errors",
      "type": "array"
    },
    "outcome": {
      "enum": [
        "stopped",
        "already_exited"
      ],
      "title": "Outcome",
      "type": "string"
    },
    "pid": {
      "title": "Pid",
      "type": "integer"
    },
    "session_id": {
      "title": "Session Id",
      "type": "string"
    },
    "stopped": {
      "title": "Stopped",
      "type": "boolean"
    }
  },
  "required": [
    "session_id",
    "pid",
    "alive_before",
    "alive_after",
    "stopped",
    "outcome"
  ],
  "title": "Stopped",
  "type": "object"
}
```

### Behavior Annotations

```json
{
  "destructiveHint": true,
  "idempotentHint": true,
  "openWorldHint": false,
  "readOnlyHint": false
}
```

## `mgba_live_run_lua`

Unrestricted Lua; may change emulator/files/processes. No safe retry.

- Required input fields: `session`
- Runtime argument rule: Provide exactly one of `file` or `code`.

### Input Schema

```json
{
  "additionalProperties": false,
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
      "description": "Finite positive budget in seconds for the entire operation, including validation, worker queueing, execution, settling, capture, and result assembly as applicable. Expiry does not prove nonexecution; inspect execution_outcome before recovery.",
      "exclusiveMinimum": 0,
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

```json
{
  "$defs": {
    "JsonValue": {}
  },
  "additionalProperties": false,
  "properties": {
    "data": {
      "$ref": "#/$defs/JsonValue"
    },
    "frame": {
      "anyOf": [
        {
          "type": "integer"
        },
        {
          "type": "null"
        }
      ],
      "title": "Frame"
    },
    "session_id": {
      "title": "Session Id",
      "type": "string"
    }
  },
  "required": [
    "frame",
    "session_id",
    "data"
  ],
  "title": "Command[JsonValue]",
  "type": "object"
}
```

### Behavior Annotations

```json
{
  "destructiveHint": true,
  "idempotentHint": false,
  "openWorldHint": true,
  "readOnlyHint": false
}
```

## `mgba_live_run_lua_and_view`

Unrestricted Lua, settle, capture; changes may persist. No safe retry.

- Required input fields: `session`
- Runtime argument rule: Provide exactly one of `file` or `code`.

### Input Schema

```json
{
  "additionalProperties": false,
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
      "description": "Finite positive budget in seconds for the entire operation, including validation, worker queueing, execution, settling, capture, and result assembly as applicable. Expiry does not prove nonexecution; inspect execution_outcome before recovery.",
      "exclusiveMinimum": 0,
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

```json
{
  "$defs": {
    "Frame": {
      "additionalProperties": false,
      "properties": {
        "frame": {
          "anyOf": [
            {
              "type": "integer"
            },
            {
              "type": "null"
            }
          ],
          "title": "Frame"
        }
      },
      "required": [
        "frame"
      ],
      "title": "Frame",
      "type": "object"
    },
    "JsonValue": {}
  },
  "additionalProperties": false,
  "properties": {
    "data": {
      "$ref": "#/$defs/JsonValue"
    },
    "frame": {
      "anyOf": [
        {
          "type": "integer"
        },
        {
          "type": "null"
        }
      ],
      "title": "Frame"
    },
    "screenshot": {
      "$ref": "#/$defs/Frame"
    },
    "session_id": {
      "title": "Session Id",
      "type": "string"
    }
  },
  "required": [
    "frame",
    "session_id",
    "data",
    "screenshot"
  ],
  "title": "CommandView[JsonValue]",
  "type": "object"
}
```

### Behavior Annotations

```json
{
  "destructiveHint": true,
  "idempotentHint": false,
  "openWorldHint": true,
  "readOnlyHint": false
}
```

## `mgba_live_input_tap`

Tap a key for N frames. Metadata only.

- Required input fields: `session`, `key`

### Input Schema

```json
{
  "additionalProperties": false,
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
      "description": "Finite positive budget in seconds for the entire operation, including validation, worker queueing, execution, settling, capture, and result assembly as applicable. Expiry does not prove nonexecution; inspect execution_outcome before recovery.",
      "exclusiveMinimum": 0,
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

```json
{
  "$defs": {
    "Tap": {
      "additionalProperties": false,
      "properties": {
        "duration": {
          "title": "Duration",
          "type": "integer"
        },
        "key": {
          "title": "Key",
          "type": "integer"
        }
      },
      "required": [
        "key",
        "duration"
      ],
      "title": "Tap",
      "type": "object"
    }
  },
  "additionalProperties": false,
  "properties": {
    "data": {
      "$ref": "#/$defs/Tap"
    },
    "frame": {
      "anyOf": [
        {
          "type": "integer"
        },
        {
          "type": "null"
        }
      ],
      "title": "Frame"
    },
    "session_id": {
      "title": "Session Id",
      "type": "string"
    }
  },
  "required": [
    "frame",
    "session_id",
    "data"
  ],
  "title": "Command[Tap]",
  "type": "object"
}
```

### Behavior Annotations

```json
{
  "destructiveHint": true,
  "idempotentHint": false,
  "openWorldHint": false,
  "readOnlyHint": false
}
```

## `mgba_live_input_tap_and_view`

Tap a key, optionally wait additional frames, then return one screenshot.

- Required input fields: `session`, `key`

### Input Schema

```json
{
  "additionalProperties": false,
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
      "description": "Finite positive budget in seconds for the entire operation, including validation, worker queueing, execution, settling, capture, and result assembly as applicable. Expiry does not prove nonexecution; inspect execution_outcome before recovery.",
      "exclusiveMinimum": 0,
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

```json
{
  "$defs": {
    "Frame": {
      "additionalProperties": false,
      "properties": {
        "frame": {
          "anyOf": [
            {
              "type": "integer"
            },
            {
              "type": "null"
            }
          ],
          "title": "Frame"
        }
      },
      "required": [
        "frame"
      ],
      "title": "Frame",
      "type": "object"
    },
    "Tap": {
      "additionalProperties": false,
      "properties": {
        "duration": {
          "title": "Duration",
          "type": "integer"
        },
        "key": {
          "title": "Key",
          "type": "integer"
        }
      },
      "required": [
        "key",
        "duration"
      ],
      "title": "Tap",
      "type": "object"
    }
  },
  "additionalProperties": false,
  "properties": {
    "data": {
      "$ref": "#/$defs/Tap"
    },
    "frame": {
      "anyOf": [
        {
          "type": "integer"
        },
        {
          "type": "null"
        }
      ],
      "title": "Frame"
    },
    "screenshot": {
      "$ref": "#/$defs/Frame"
    },
    "session_id": {
      "title": "Session Id",
      "type": "string"
    }
  },
  "required": [
    "frame",
    "session_id",
    "data",
    "screenshot"
  ],
  "title": "CommandView[Tap]",
  "type": "object"
}
```

### Behavior Annotations

```json
{
  "destructiveHint": true,
  "idempotentHint": false,
  "openWorldHint": false,
  "readOnlyHint": false
}
```

## `mgba_live_input_set`

Replace held keys and cancel scheduled releases; the game keeps running.

- Required input fields: `session`, `keys`

### Input Schema

```json
{
  "additionalProperties": false,
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
      "description": "Finite positive budget in seconds for the entire operation, including validation, worker queueing, execution, settling, capture, and result assembly as applicable. Expiry does not prove nonexecution; inspect execution_outcome before recovery.",
      "exclusiveMinimum": 0,
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

```json
{
  "$defs": {
    "Keys": {
      "additionalProperties": false,
      "properties": {
        "keys": {
          "items": {
            "type": "integer"
          },
          "title": "Keys",
          "type": "array"
        }
      },
      "required": [
        "keys"
      ],
      "title": "Keys",
      "type": "object"
    }
  },
  "additionalProperties": false,
  "properties": {
    "data": {
      "$ref": "#/$defs/Keys"
    },
    "frame": {
      "anyOf": [
        {
          "type": "integer"
        },
        {
          "type": "null"
        }
      ],
      "title": "Frame"
    },
    "session_id": {
      "title": "Session Id",
      "type": "string"
    }
  },
  "required": [
    "frame",
    "session_id",
    "data"
  ],
  "title": "Command[Keys]",
  "type": "object"
}
```

### Behavior Annotations

```json
{
  "destructiveHint": true,
  "idempotentHint": true,
  "openWorldHint": false,
  "readOnlyHint": false
}
```

## `mgba_live_input_clear`

Clear held keys from a live session.

- Required input fields: `session`

### Input Schema

```json
{
  "additionalProperties": false,
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
      "description": "Finite positive budget in seconds for the entire operation, including validation, worker queueing, execution, settling, capture, and result assembly as applicable. Expiry does not prove nonexecution; inspect execution_outcome before recovery.",
      "exclusiveMinimum": 0,
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

```json
{
  "$defs": {
    "Cleared": {
      "additionalProperties": false,
      "properties": {
        "cleared": {
          "const": "all",
          "title": "Cleared",
          "type": "string"
        }
      },
      "required": [
        "cleared"
      ],
      "title": "Cleared",
      "type": "object"
    },
    "Keys": {
      "additionalProperties": false,
      "properties": {
        "keys": {
          "items": {
            "type": "integer"
          },
          "title": "Keys",
          "type": "array"
        }
      },
      "required": [
        "keys"
      ],
      "title": "Keys",
      "type": "object"
    }
  },
  "additionalProperties": false,
  "properties": {
    "data": {
      "anyOf": [
        {
          "$ref": "#/$defs/Keys"
        },
        {
          "$ref": "#/$defs/Cleared"
        }
      ],
      "title": "Data"
    },
    "frame": {
      "anyOf": [
        {
          "type": "integer"
        },
        {
          "type": "null"
        }
      ],
      "title": "Frame"
    },
    "session_id": {
      "title": "Session Id",
      "type": "string"
    }
  },
  "required": [
    "frame",
    "session_id",
    "data"
  ],
  "title": "Command[Union[Keys, Cleared]]",
  "type": "object"
}
```

### Behavior Annotations

```json
{
  "destructiveHint": true,
  "idempotentHint": true,
  "openWorldHint": false,
  "readOnlyHint": false
}
```

## `mgba_live_export_screenshot`

Save a screenshot; may overwrite the requested file.

- Required input fields: `session`

### Input Schema

```json
{
  "additionalProperties": false,
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
      "description": "Finite positive budget in seconds for the entire operation, including validation, worker queueing, execution, settling, capture, and result assembly as applicable. Expiry does not prove nonexecution; inspect execution_outcome before recovery.",
      "exclusiveMinimum": 0,
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

```json
{
  "additionalProperties": false,
  "properties": {
    "frame": {
      "anyOf": [
        {
          "type": "integer"
        },
        {
          "type": "null"
        }
      ],
      "title": "Frame"
    },
    "path": {
      "title": "Path",
      "type": "string"
    },
    "session_id": {
      "title": "Session Id",
      "type": "string"
    }
  },
  "required": [
    "frame",
    "session_id",
    "path"
  ],
  "title": "Exported",
  "type": "object"
}
```

### Behavior Annotations

```json
{
  "destructiveHint": true,
  "idempotentHint": false,
  "openWorldHint": true,
  "readOnlyHint": false
}
```

## `mgba_live_read_memory`

Read memory addresses from a live session.

- Required input fields: `session`, `addresses`

### Input Schema

```json
{
  "additionalProperties": false,
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
      "description": "Finite positive budget in seconds for the entire operation, including validation, worker queueing, execution, settling, capture, and result assembly as applicable. Expiry does not prove nonexecution; inspect execution_outcome before recovery.",
      "exclusiveMinimum": 0,
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

```json
{
  "$defs": {
    "JsonValue": {}
  },
  "additionalProperties": false,
  "properties": {
    "frame": {
      "anyOf": [
        {
          "type": "integer"
        },
        {
          "type": "null"
        }
      ],
      "title": "Frame"
    },
    "memory": {
      "anyOf": [
        {
          "additionalProperties": {
            "type": "integer"
          },
          "type": "object"
        },
        {
          "items": {
            "$ref": "#/$defs/JsonValue"
          },
          "maxItems": 0,
          "type": "array"
        }
      ],
      "title": "Memory"
    },
    "session_id": {
      "title": "Session Id",
      "type": "string"
    }
  },
  "required": [
    "frame",
    "session_id",
    "memory"
  ],
  "title": "Memory",
  "type": "object"
}
```

### Behavior Annotations

```json
{
  "destructiveHint": false,
  "idempotentHint": true,
  "openWorldHint": false,
  "readOnlyHint": true
}
```

## `mgba_live_read_range`

Read a contiguous memory range from a live session.

- Required input fields: `session`, `start`, `length`

### Input Schema

```json
{
  "additionalProperties": false,
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
      "description": "Finite positive budget in seconds for the entire operation, including validation, worker queueing, execution, settling, capture, and result assembly as applicable. Expiry does not prove nonexecution; inspect execution_outcome before recovery.",
      "exclusiveMinimum": 0,
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

```json
{
  "$defs": {
    "RangeData": {
      "additionalProperties": false,
      "properties": {
        "data": {
          "items": {
            "type": "integer"
          },
          "title": "Data",
          "type": "array"
        },
        "length": {
          "title": "Length",
          "type": "integer"
        },
        "start": {
          "title": "Start",
          "type": "integer"
        }
      },
      "required": [
        "start",
        "length",
        "data"
      ],
      "title": "RangeData",
      "type": "object"
    }
  },
  "additionalProperties": false,
  "properties": {
    "frame": {
      "anyOf": [
        {
          "type": "integer"
        },
        {
          "type": "null"
        }
      ],
      "title": "Frame"
    },
    "range": {
      "$ref": "#/$defs/RangeData"
    },
    "session_id": {
      "title": "Session Id",
      "type": "string"
    }
  },
  "required": [
    "frame",
    "session_id",
    "range"
  ],
  "title": "MemoryRange",
  "type": "object"
}
```

### Behavior Annotations

```json
{
  "destructiveHint": false,
  "idempotentHint": true,
  "openWorldHint": false,
  "readOnlyHint": true
}
```

## `mgba_live_dump_pointers`

Dump pointer table entries from a live session.

- Required input fields: `session`, `start`, `count`

### Input Schema

```json
{
  "additionalProperties": false,
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
      "description": "Finite positive budget in seconds for the entire operation, including validation, worker queueing, execution, settling, capture, and result assembly as applicable. Expiry does not prove nonexecution; inspect execution_outcome before recovery.",
      "exclusiveMinimum": 0,
      "type": "number"
    },
    "width": {
      "default": 4,
      "maximum": 6,
      "minimum": 1,
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

```json
{
  "$defs": {
    "Pointer": {
      "additionalProperties": false,
      "properties": {
        "address": {
          "title": "Address",
          "type": "integer"
        },
        "index": {
          "title": "Index",
          "type": "integer"
        },
        "value": {
          "maximum": 281474976710655,
          "minimum": 0,
          "title": "Value",
          "type": "integer"
        }
      },
      "required": [
        "index",
        "address",
        "value"
      ],
      "title": "Pointer",
      "type": "object"
    },
    "PointerData": {
      "additionalProperties": false,
      "properties": {
        "count": {
          "title": "Count",
          "type": "integer"
        },
        "pointers": {
          "items": {
            "$ref": "#/$defs/Pointer"
          },
          "title": "Pointers",
          "type": "array"
        },
        "start": {
          "title": "Start",
          "type": "integer"
        },
        "width": {
          "maximum": 6,
          "minimum": 1,
          "title": "Width",
          "type": "integer"
        }
      },
      "required": [
        "start",
        "count",
        "width",
        "pointers"
      ],
      "title": "PointerData",
      "type": "object"
    }
  },
  "additionalProperties": false,
  "properties": {
    "frame": {
      "anyOf": [
        {
          "type": "integer"
        },
        {
          "type": "null"
        }
      ],
      "title": "Frame"
    },
    "pointers": {
      "$ref": "#/$defs/PointerData"
    },
    "session_id": {
      "title": "Session Id",
      "type": "string"
    }
  },
  "required": [
    "frame",
    "session_id",
    "pointers"
  ],
  "title": "Pointers",
  "type": "object"
}
```

### Behavior Annotations

```json
{
  "destructiveHint": false,
  "idempotentHint": true,
  "openWorldHint": false,
  "readOnlyHint": true
}
```

## `mgba_live_dump_oam`

Dump OAM entries from a live session.

- Required input fields: `session`

### Input Schema

```json
{
  "additionalProperties": false,
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
      "description": "Finite positive budget in seconds for the entire operation, including validation, worker queueing, execution, settling, capture, and result assembly as applicable. Expiry does not prove nonexecution; inspect execution_outcome before recovery.",
      "exclusiveMinimum": 0,
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

```json
{
  "$defs": {
    "OamData": {
      "additionalProperties": false,
      "properties": {
        "base": {
          "title": "Base",
          "type": "integer"
        },
        "count": {
          "title": "Count",
          "type": "integer"
        },
        "sprites": {
          "items": {
            "$ref": "#/$defs/Sprite"
          },
          "title": "Sprites",
          "type": "array"
        }
      },
      "required": [
        "base",
        "count",
        "sprites"
      ],
      "title": "OamData",
      "type": "object"
    },
    "Sprite": {
      "additionalProperties": false,
      "properties": {
        "address": {
          "title": "Address",
          "type": "integer"
        },
        "attr0": {
          "title": "Attr0",
          "type": "integer"
        },
        "attr1": {
          "title": "Attr1",
          "type": "integer"
        },
        "attr2": {
          "title": "Attr2",
          "type": "integer"
        },
        "index": {
          "title": "Index",
          "type": "integer"
        }
      },
      "required": [
        "index",
        "address",
        "attr0",
        "attr1",
        "attr2"
      ],
      "title": "Sprite",
      "type": "object"
    }
  },
  "additionalProperties": false,
  "properties": {
    "frame": {
      "anyOf": [
        {
          "type": "integer"
        },
        {
          "type": "null"
        }
      ],
      "title": "Frame"
    },
    "oam": {
      "$ref": "#/$defs/OamData"
    },
    "session_id": {
      "title": "Session Id",
      "type": "string"
    }
  },
  "required": [
    "frame",
    "session_id",
    "oam"
  ],
  "title": "Oam",
  "type": "object"
}
```

### Behavior Annotations

```json
{
  "destructiveHint": false,
  "idempotentHint": true,
  "openWorldHint": false,
  "readOnlyHint": true
}
```

## `mgba_live_dump_entities`

Dump structured entity bytes from a live session.

- Required input fields: `session`

### Input Schema

```json
{
  "additionalProperties": false,
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
      "description": "Finite positive budget in seconds for the entire operation, including validation, worker queueing, execution, settling, capture, and result assembly as applicable. Expiry does not prove nonexecution; inspect execution_outcome before recovery.",
      "exclusiveMinimum": 0,
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

```json
{
  "$defs": {
    "Entity": {
      "additionalProperties": false,
      "properties": {
        "address": {
          "title": "Address",
          "type": "integer"
        },
        "bytes": {
          "items": {
            "type": "integer"
          },
          "title": "Bytes",
          "type": "array"
        },
        "index": {
          "title": "Index",
          "type": "integer"
        }
      },
      "required": [
        "index",
        "address",
        "bytes"
      ],
      "title": "Entity",
      "type": "object"
    },
    "EntityData": {
      "additionalProperties": false,
      "properties": {
        "base": {
          "title": "Base",
          "type": "integer"
        },
        "count": {
          "title": "Count",
          "type": "integer"
        },
        "entities": {
          "items": {
            "$ref": "#/$defs/Entity"
          },
          "title": "Entities",
          "type": "array"
        },
        "size": {
          "title": "Size",
          "type": "integer"
        }
      },
      "required": [
        "base",
        "size",
        "count",
        "entities"
      ],
      "title": "EntityData",
      "type": "object"
    }
  },
  "additionalProperties": false,
  "properties": {
    "entities": {
      "$ref": "#/$defs/EntityData"
    },
    "frame": {
      "anyOf": [
        {
          "type": "integer"
        },
        {
          "type": "null"
        }
      ],
      "title": "Frame"
    },
    "session_id": {
      "title": "Session Id",
      "type": "string"
    }
  },
  "required": [
    "frame",
    "session_id",
    "entities"
  ],
  "title": "Entities",
  "type": "object"
}
```

### Behavior Annotations

```json
{
  "destructiveHint": false,
  "idempotentHint": true,
  "openWorldHint": false,
  "readOnlyHint": true
}
```
