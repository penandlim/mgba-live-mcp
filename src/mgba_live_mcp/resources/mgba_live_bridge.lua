-- Persistent bridge script for live mGBA process control.
-- Loaded once via: mgba-qt --script mgba_live_bridge.lua <rom>

local frame = 0

local session_dir = os.getenv("MGBA_LIVE_SESSION_DIR") or "."
local command_path = os.getenv("MGBA_LIVE_COMMAND") or (session_dir .. "/command.lua")
local response_path = os.getenv("MGBA_LIVE_RESPONSE") or (session_dir .. "/response.json")
local heartbeat_path = os.getenv("MGBA_LIVE_HEARTBEAT") or (session_dir .. "/heartbeat.json")
local heartbeat_interval = tonumber(os.getenv("MGBA_LIVE_HEARTBEAT_INTERVAL") or "30")

local key_map = {
  A = C.GBA_KEY.A,
  B = C.GBA_KEY.B,
  SELECT = C.GBA_KEY.SELECT,
  START = C.GBA_KEY.START,
  RIGHT = C.GBA_KEY.RIGHT,
  LEFT = C.GBA_KEY.LEFT,
  UP = C.GBA_KEY.UP,
  DOWN = C.GBA_KEY.DOWN,
  R = C.GBA_KEY.R,
  L = C.GBA_KEY.L,
}

local release_at = {}

local function json_escape(str)
  local repl = {
    ['"'] = '\\"',
    ['\\'] = '\\\\',
    ['\b'] = '\\b',
    ['\f'] = '\\f',
    ['\n'] = '\\n',
    ['\r'] = '\\r',
    ['\t'] = '\\t',
  }
  return str:gsub('[%z\1-\31\\"]', function(c)
    if repl[c] then
      return repl[c]
    end
    return string.format("\\u%04x", c:byte())
  end)
end

local function is_array(tbl)
  local n = 0
  local max = 0
  for k, _ in pairs(tbl) do
    if type(k) ~= "number" or k < 1 or math.floor(k) ~= k then
      return false
    end
    n = n + 1
    if k > max then
      max = k
    end
  end
  return n == max
end

local function json_encode(value)
  local t = type(value)
  if t == "nil" then
    return "null"
  end
  if t == "boolean" then
    return value and "true" or "false"
  end
  if t == "number" then
    if value ~= value or value == math.huge or value == -math.huge then
      return "null"
    end
    return tostring(value)
  end
  if t == "string" then
    return '"' .. json_escape(value) .. '"'
  end
  if t == "table" then
    if is_array(value) then
      local parts = {}
      for i = 1, #value do
        parts[#parts + 1] = json_encode(value[i])
      end
      return "[" .. table.concat(parts, ",") .. "]"
    end
    local keys = {}
    for k, _ in pairs(value) do
      keys[#keys + 1] = tostring(k)
    end
    table.sort(keys)
    local parts = {}
    for _, sk in ipairs(keys) do
      local v = value[sk]
      if v == nil then
        v = value[tonumber(sk)]
      end
      parts[#parts + 1] = '"' .. json_escape(sk) .. '":' .. json_encode(v)
    end
    return "{" .. table.concat(parts, ",") .. "}"
  end
  return '"' .. json_escape(tostring(value)) .. '"'
end

local function write_text(path, text)
  local f, err = io.open(path, "w")
  if not f then
    return false, err
  end
  f:write(text)
  f:close()
  return true, nil
end

local function write_json(path, value)
  return write_text(path, json_encode(value))
end

local function resolve_key(k)
  if type(k) == "number" then
    return k
  end
  if type(k) ~= "string" then
    return nil
  end
  local up = string.upper(k)
  return key_map[up]
end

local function to_key_list(keys)
  local out = {}
  if type(keys) ~= "table" then
    return out
  end
  for _, k in ipairs(keys) do
    local idx = resolve_key(k)
    if idx ~= nil then
      out[#out + 1] = idx
    end
  end
  return out
end

local function apply_key_releases()
  for key, until_frame in pairs(release_at) do
    if frame >= until_frame then
      emu:clearKey(key)
      release_at[key] = nil
    end
  end
end

local function parse_command_file()
  local f = io.open(command_path, "r")
  if not f then
    return nil
  end
  f:close()

  local loader, lerr = loadfile(command_path)
  os.remove(command_path)
  if not loader then
    return { id = "unknown", kind = "__invalid__", _error = tostring(lerr) }
  end

  local ok, command = pcall(loader)
  if not ok then
    return { id = "unknown", kind = "__invalid__", _error = tostring(command) }
  end
  if type(command) ~= "table" then
    return { id = "unknown", kind = "__invalid__", _error = "command.lua must return a table" }
  end
  return command
end

local function resolve_output_path(path, fallback_name)
  if type(path) == "string" and #path > 0 then
    if string.sub(path, 1, 1) == "/" then
      return path
    end
    return session_dir .. "/" .. path
  end
  return session_dir .. "/" .. fallback_name
end

local function read_memory(addresses)
  local data = {}
  for _, addr in ipairs(addresses or {}) do
    data[string.format("0x%08X", addr)] = emu:read8(addr)
  end
  return data
end

local function read_range(start_addr, length)
  local data = {}
  for i = 0, length - 1 do
    data[#data + 1] = emu:read8(start_addr + i)
  end
  return {
    start = start_addr,
    length = length,
    data = data,
  }
end

local function read_pointer(addr, width)
  local val = 0
  for i = 0, width - 1 do
    val = val + emu:read8(addr + i) * (256 ^ i)
  end
  return val
end

local function dump_pointers(start_addr, count, width)
  local pointers = {}
  for i = 0, count - 1 do
    local addr = start_addr + i * width
    pointers[#pointers + 1] = {
      index = i,
      address = addr,
      value = read_pointer(addr, width),
    }
  end
  return {
    start = start_addr,
    count = count,
    width = width,
    pointers = pointers,
  }
end

local function dump_oam(count)
  local base = 0x07000000
  local max = tonumber(count or 40) or 40
  if max < 1 then
    max = 1
  end
  if max > 128 then
    max = 128
  end
  local sprites = {}
  for i = 0, max - 1 do
    local addr = base + i * 8
    local attr0 = emu:read8(addr) + emu:read8(addr + 1) * 256
    local attr1 = emu:read8(addr + 2) + emu:read8(addr + 3) * 256
    local attr2 = emu:read8(addr + 4) + emu:read8(addr + 5) * 256
    sprites[#sprites + 1] = {
      index = i,
      address = addr,
      attr0 = attr0,
      attr1 = attr1,
      attr2 = attr2,
    }
  end
  return {
    base = base,
    count = max,
    sprites = sprites,
  }
end

local function dump_entities(base, size, count)
  local entity_base = tonumber(base or 0xC200) or 0xC200
  local entity_size = tonumber(size or 24) or 24
  local entity_count = tonumber(count or 10) or 10
  if entity_size < 1 then
    entity_size = 1
  end
  if entity_count < 1 then
    entity_count = 1
  end
  local entities = {}
  for i = 0, entity_count - 1 do
    local addr = entity_base + i * entity_size
    local bytes = {}
    for j = 0, entity_size - 1 do
      bytes[#bytes + 1] = emu:read8(addr + j)
    end
    entities[#entities + 1] = {
      index = i,
      address = addr,
      bytes = bytes,
    }
  end
  return {
    base = entity_base,
    size = entity_size,
    count = entity_count,
    entities = entities,
  }
end

local function run_lua_file(path)
  if type(path) ~= "string" or #path == 0 then
    return nil, "missing script path"
  end
  local resolved = path
  if string.sub(path, 1, 1) ~= "/" then
    resolved = session_dir .. "/" .. path
  end
  local loader, err = loadfile(resolved)
  if not loader then
    return nil, err
  end
  local ok, result = pcall(loader)
  if not ok then
    return nil, result
  end
  return result, nil
end

local function run_lua_inline(code)
  if type(code) ~= "string" or #code == 0 then
    return nil, "missing inline code"
  end
  local load_fn = loadstring or load
  local loader, err = load_fn(code, "mgba_live_inline")
  if not loader then
    return nil, err
  end
  local ok, result = pcall(loader)
  if not ok then
    return nil, result
  end
  return result, nil
end

local function handle_command(cmd)
  local kind = cmd.kind
  if kind == "ping" then
    return { frame = frame, keys = emu:getKeys() }
  end

  if kind == "screenshot" then
    local out_path = resolve_output_path(cmd.path, string.format("screenshots/frame_%08d.png", frame))
    emu:screenshot(out_path)
    return { path = out_path }
  end

  if kind == "tap_key" then
    local key = resolve_key(cmd.key)
    if key == nil then
      error("invalid key")
    end
    local duration = tonumber(cmd.duration or 1) or 1
    if duration < 1 then
      duration = 1
    end
    emu:addKey(key)
    release_at[key] = frame + duration
    return { key = key, duration = duration }
  end

  if kind == "set_keys" then
    local key_list = to_key_list(cmd.keys or {})
    emu:setKeys(util.makeBitmask(key_list))
    release_at = {}
    return { keys = key_list }
  end

  if kind == "clear_keys" then
    if type(cmd.keys) ~= "table" then
      emu:setKeys(0)
      release_at = {}
      return { cleared = "all" }
    end
    local key_list = to_key_list(cmd.keys)
    for _, key in ipairs(key_list) do
      emu:clearKey(key)
      release_at[key] = nil
    end
    return { keys = key_list }
  end

  if kind == "read_memory" then
    return read_memory(cmd.addresses or {})
  end

  if kind == "read_range" then
    local start_addr = tonumber(cmd.start)
    local length = tonumber(cmd.length)
    if not start_addr or not length then
      error("read_range requires start and length")
    end
    if length < 1 then
      error("length must be > 0")
    end
    return read_range(start_addr, length)
  end

  if kind == "dump_pointers" then
    local start_addr = tonumber(cmd.start)
    local count = tonumber(cmd.count)
    local width = tonumber(cmd.width or 4) or 4
    if not start_addr or not count then
      error("dump_pointers requires start and count")
    end
    if width < 1 then
      width = 1
    end
    if width > 8 then
      width = 8
    end
    if count < 1 then
      count = 1
    end
    return dump_pointers(start_addr, count, width)
  end

  if kind == "dump_oam" then
    return dump_oam(cmd.count)
  end

  if kind == "dump_entities" then
    return dump_entities(cmd.base, cmd.size, cmd.count)
  end

  if kind == "run_lua_file" then
    local result, err = run_lua_file(cmd.path)
    if err then
      error(err)
    end
    return { result = result }
  end

  if kind == "run_lua_inline" then
    local result, err = run_lua_inline(cmd.code)
    if err then
      error(err)
    end
    return { result = result }
  end

  error("unknown command: " .. tostring(kind))
end

local function process_command(cmd)
  local command_id = tostring(cmd.id or "unknown")
  if cmd.kind == "__invalid__" then
    write_json(response_path, {
      id = command_id,
      ok = false,
      frame = frame,
      error = cmd._error or "invalid command",
    })
    return
  end

  local ok, data = pcall(handle_command, cmd)
  if ok then
    write_json(response_path, {
      id = command_id,
      ok = true,
      frame = frame,
      data = data,
    })
    return
  end

  write_json(response_path, {
    id = command_id,
    ok = false,
    frame = frame,
    error = tostring(data),
  })
end

local function write_heartbeat()
  write_json(heartbeat_path, {
    frame = frame,
    keys = emu:getKeys(),
    unix_time = os.time(),
  })
end

callbacks:add("frame", function()
  frame = frame + 1
  apply_key_releases()

  if frame == 1 or (heartbeat_interval > 0 and frame % heartbeat_interval == 0) then
    write_heartbeat()
  end

  local command = parse_command_file()
  if command then
    process_command(command)
  end
end)
