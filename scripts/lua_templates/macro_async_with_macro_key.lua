-- Generic async macro template for mgba_live_run_lua.
--
-- Contract:
-- - Return macro_key in the initial response.
-- - Set _G[macro_key].active = false at completion.

local macro_key = "__example_async_macro"

if _G[macro_key] and _G[macro_key].active then
  return { status = "already-running", macro_key = macro_key }
end

local cfg = {
  wait_frames = 120,
  hold_frames = 2,
  final_settle_frames = 20,
}

local state = {
  active = true,
  frame = 0,
  active_key = nil,
  release_at = nil,
  done_at = nil,
  pressed = false,
  frame_cb = nil,
  keys_cb = nil,
}

_G[macro_key] = state

local function stop_macro()
  state.active = false
  if _G[macro_key] then
    _G[macro_key].active = false
  end
  if state.frame_cb then
    callbacks:remove(state.frame_cb)
  end
  if state.keys_cb then
    callbacks:remove(state.keys_cb)
  end
  emu:setKeys(0)
end

local function on_keys_read()
  if not state.active then
    return
  end
  emu:setKeys(0)
  if state.active_key then
    emu:addKey(state.active_key)
  end
end

local function on_frame()
  if not state.active then
    return
  end

  state.frame = state.frame + 1

  if state.active_key and state.frame >= state.release_at then
    state.active_key = nil
    state.release_at = nil
    state.done_at = state.frame + cfg.final_settle_frames
  end

  if not state.pressed and state.frame >= cfg.wait_frames then
    state.active_key = C.GBA_KEY.A
    state.release_at = state.frame + cfg.hold_frames
    state.pressed = true
  end

  if state.done_at and state.frame >= state.done_at then
    stop_macro()
  end
end

state.frame_cb = callbacks:add("frame", on_frame)
state.keys_cb = callbacks:add("keysRead", on_keys_read)

return {
  status = "started",
  macro_key = macro_key,
  wait = cfg.wait_frames,
  hold = cfg.hold_frames,
  final_settle = cfg.final_settle_frames,
}
