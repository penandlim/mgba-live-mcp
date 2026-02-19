-- Kamaitachi no Yoru Advance
-- Start state: freshly started ROM session (mgba-live)
-- End state: HELP > INFO (first page)
--
-- Contract for mgba_live_run_lua post-macro screenshot settle:
-- - Return macro_key in the initial result.
-- - Set _G[macro_key].active = false when complete.

local macro_key = "__kamaitachi_fresh_start_to_help_info_page1"

if _G[macro_key] and _G[macro_key].active then
  console:log("[help-info] macro already running")
  return { status = "already-running", macro_key = macro_key }
end

local cfg = {
  initial_wait_frames = 650,
  hold_frames = 2,
  settle_after_step_frames = {60, 620, 40, 0},
  final_settle_frames = 120,
}

-- Fresh boot path to INFO page 1:
-- 1) RIGHT: move from GAME START to HELP
-- 2) A: open HELP list
-- 3) DOWN: move to INFO
-- 4) A: open INFO page 1
local sequence = {
  C.GBA_KEY.RIGHT,
  C.GBA_KEY.A,
  C.GBA_KEY.DOWN,
  C.GBA_KEY.A,
}

local state = {
  active = true,
  frame = 0,
  step_index = 1,
  active_key = nil,
  release_at = nil,
  next_press_at = cfg.initial_wait_frames,
  final_done_at = nil,
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
  console:log("[help-info] reached INFO page sequence end")
end

local function on_keys_read()
  if not state.active then
    return
  end

  -- Inject only this macro's key for deterministic input.
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

    local completed_step = state.step_index - 1
    local settle = cfg.settle_after_step_frames[completed_step] or 0
    state.next_press_at = state.frame + settle

    if state.step_index > #sequence and not state.final_done_at then
      state.final_done_at = state.frame + cfg.final_settle_frames
    end
  end

  if (not state.active_key)
      and state.step_index <= #sequence
      and state.frame >= state.next_press_at then
    state.active_key = sequence[state.step_index]
    state.release_at = state.frame + cfg.hold_frames
    state.step_index = state.step_index + 1
  end

  if state.final_done_at and state.frame >= state.final_done_at then
    stop_macro()
  end
end

state.frame_cb = callbacks:add("frame", on_frame)
state.keys_cb = callbacks:add("keysRead", on_keys_read)

console:log(string.format(
  "[help-info] macro started (wait=%d hold=%d settle=[%d,%d,%d,%d] final=%d)",
  cfg.initial_wait_frames,
  cfg.hold_frames,
  cfg.settle_after_step_frames[1],
  cfg.settle_after_step_frames[2],
  cfg.settle_after_step_frames[3],
  cfg.settle_after_step_frames[4],
  cfg.final_settle_frames
))

return {
  status = "started",
  macro_key = macro_key,
  wait = cfg.initial_wait_frames,
  hold = cfg.hold_frames,
  settle_after_step = cfg.settle_after_step_frames,
  final_settle = cfg.final_settle_frames,
}
