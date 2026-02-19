-- One-shot deterministic input template.
-- Presses A once using callbacks and returns macro_key so run-lua waits correctly.

local macro_key = "__input_one_shot_a"

if _G[macro_key] and _G[macro_key].active then
  return { status = "already-running", macro_key = macro_key }
end

local state = {
  active = true,
  frame = 0,
  frame_cb = nil,
  keys_cb = nil,
  active_key = nil,
  release_at = nil,
  done_at = nil,
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

  if state.frame == 1 then
    state.active_key = C.GBA_KEY.A
    state.release_at = 3
    return
  end

  if state.active_key and state.frame >= state.release_at then
    state.active_key = nil
    state.done_at = state.frame + 5
    return
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
  key = "A",
  hold_frames = 2,
}
