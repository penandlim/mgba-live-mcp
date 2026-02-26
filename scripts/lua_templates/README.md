# Lua Templates

Reusable Lua templates for `mgba_live_run_lua`.

## Run a template

Use the `file` argument with an absolute path (recommended):

```json
{"session":"<session-id>","file":"/absolute/path/to/mgba-live-mcp/scripts/lua_templates/kamaitachi_fresh_start_to_help_info_page1.lua"}
```

## Templates

- `kamaitachi_fresh_start_to_help_info_page1.lua`
  - Fresh boot macro for Kamaitachi (RIGHT -> A -> DOWN -> A).
  - Returns `macro_key` and marks macro inactive on completion.
- `macro_async_with_macro_key.lua`
  - Minimal async callback macro pattern with `macro_key` contract.
- `input_one_shot.lua`
  - One-shot deterministic key tap template (`A`) using callbacks.
- `memory_probe.lua`
  - Read-only memory probe returning sample bytes/16-bit values.

## Contract for callback-based macros

For correct `mgba_live_run_lua` post-run screenshots:

- Return `macro_key` in the initial Lua result.
- Keep `_G[macro_key].active = true` while running.
- Set `_G[macro_key].active = false` when complete.
