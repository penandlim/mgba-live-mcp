-- Persistent bridge script for live mGBA process control.
-- Loaded once via: mgba-qt --script mgba_live_bridge.lua <rom>

local frame = 0
-- Lua drops nil-valued table fields; retain an explicit JSON null for Lua results.
local json_null = {}

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

-- Limits include the complete response envelope, not just the user's result.
local json_max_depth = 32
local json_max_entries = 10000
local json_max_bytes = 1048576
local serialization_messages = {
  cycle = "Lua response contains a recursive table.",
  depth_limit = "Lua response exceeds the JSON nesting limit.",
  entry_limit = "Lua response exceeds the JSON entry limit.",
  byte_limit = "Lua response exceeds the encoded JSON byte limit.",
  invalid_utf8 = "Lua response contains invalid UTF-8; return bytes as integers or hex text.",
  unsupported_type = "Lua response contains an unsupported value type.",
  unsupported_key = "Lua objects require string keys; arrays require consecutive integer keys.",
  non_finite = "Lua response contains a non-finite number.",
  unsupported_error = "Lua raised a non-text error object.",
}
local json_escapes = {
  ['"'] = '\\"', ['\\'] = '\\\\', ['\b'] = '\\b', ['\f'] = '\\f',
  ['\n'] = '\\n', ['\r'] = '\\r', ['\t'] = '\\t',
}

local function valid_utf8(str)
  -- Lua 5.3's utf8.len accepts surrogates; require scalar values on every runtime.
  local i = 1
  while true do
    i = str:find("[\128-\255]", i)
    if not i then return true end
    local a, b, c, d = str:byte(i, i + 3)
    if not b or b < 128 or b > 191 then return false end
    if a >= 194 and a <= 223 then
      i = i + 2
    elseif a >= 224 and a <= 239 and c and c >= 128 and c <= 191
        and (a ~= 224 or b >= 160) and (a ~= 237 or b < 160) then
      i = i + 3
    elseif a >= 240 and a <= 244 and c and c >= 128 and c <= 191
        and d and d >= 128 and d <= 191
        and (a ~= 240 or b >= 144) and (a ~= 244 or b < 144) then
      i = i + 4
    else
      return false
    end
  end
end

local function json_encode(value)
  local parts, active = {}, {}
  local bytes, entries = 0, 0
  local function append(text)
    if #text > json_max_bytes - bytes then
      error("byte_limit", 0)
    end
    bytes = bytes + #text
    parts[#parts + 1] = text
  end
  local function encode_string(str)
    local size = #str + 2
    if size > json_max_bytes - bytes then
      error("byte_limit", 0)
    end
    if not valid_utf8(str) then
      error("invalid_utf8", 0)
    end
    -- Count expansion before gsub allocates an escaped copy.
    for c in str:gmatch('[%z\1-\31\\"]') do
      size = size + (json_escapes[c] and #json_escapes[c] or 6) - 1
      if size > json_max_bytes - bytes then
        error("byte_limit", 0)
      end
    end
    append('"')
    append(str:gsub('[%z\1-\31\\"]', function(c)
      return json_escapes[c] or string.format("\\u%04x", c:byte())
    end))
    append('"')
  end
  local encode
  encode = function(item, depth)
    local kind = type(item)
    if kind == "nil" or rawequal(item, json_null) then
      append("null")
    elseif kind == "boolean" then
      append(item and "true" or "false")
    elseif kind == "number" then
      if item ~= item or item == math.huge or item == -math.huge then
        error("non_finite", 0)
      end
      if math.type and math.type(item) == "integer" then
        append(tostring(item))
      else
        append((string.format("%.17g", item):gsub(",", ".")))
      end
    elseif kind == "string" then
      encode_string(item)
    elseif kind == "table" then
      if active[item] then
        error("cycle", 0)
      end
      if depth > json_max_depth then
        error("depth_limit", 0)
      end
      active[item] = true
      local keys, maximum, numeric = {}, 0, 0
      -- Raw traversal avoids executing __pairs, __index or __len callbacks.
      local key_bytes = 0
      for key in next, item do
        entries = entries + 1
        if entries > json_max_entries then
          error("entry_limit", 0)
        end
        if type(key) == "number" and key >= 1 and key < math.huge
            and math.floor(key) == key then
          numeric = numeric + 1
          maximum = math.max(maximum, key)
        elseif type(key) ~= "string" then
          error("unsupported_key", 0)
        else
          key_bytes = key_bytes + #key + 3
          if key_bytes > json_max_bytes - bytes then
            error("byte_limit", 0)
          end
        end
        keys[#keys + 1] = key
      end
      local array = numeric == #keys and maximum == #keys
      if not array and numeric > 0 then
        error("unsupported_key", 0)
      end
      append(array and "[" or "{")
      if not array then
        table.sort(keys)
      end
      for i = 1, #keys do
        if i > 1 then append(",") end
        local key = array and i or keys[i]
        if not array then
          encode_string(key)
          append(":")
        end
        encode(rawget(item, key), depth + 1)
      end
      append(array and "]" or "}")
      active[item] = nil
    else
      error("unsupported_type", 0)
    end
  end
  encode(value, 1)
  return table.concat(parts)
end

local function write_text(path, text)
  -- The bridge is the sole writer of its heartbeat and response files.
  local temporary = path .. ".tmp"
  local f, err = io.open(temporary, "w")
  if not f then
    return false, err
  end
  local written, write_err = f:write(text)
  local closed, close_err = f:close()
  if not written or not closed then
    os.remove(temporary)
    return false, write_err or close_err
  end
  local renamed, rename_err = os.rename(temporary, path)
  if not renamed then
    os.remove(temporary)
    return false, rename_err
  end
  return true, nil
end

local function write_json(path, value)
  local ok, text = pcall(json_encode, value)
  if not ok then
    return false, text
  end
  return write_text(path, text)
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
  local claimed_path = command_path .. ".running"
  -- Rename is the claim: exactly one bridge invocation can win this command.
  local claimed = os.rename(command_path, claimed_path)
  if not claimed then
    return nil
  end
  local loader, lerr = loadfile(claimed_path)
  if not loader then
    os.remove(claimed_path)
    return { id = "unknown", kind = "__invalid__", _error = lerr }
  end
  local ok, command = pcall(loader)
  os.remove(claimed_path)
  if not ok then
    return { id = "unknown", kind = "__invalid__", _error = command }
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
  local val, scale = 0, 1
  for i = 0, width - 1 do
    val = val + emu:read8(addr + i) * scale
    scale = scale * 256
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
    error("missing script path", 0)
  end
  local resolved = path
  if string.sub(path, 1, 1) ~= "/" then
    resolved = session_dir .. "/" .. path
  end
  local loader, err = loadfile(resolved)
  if not loader then
    error(err, 0)
  end
  return loader()
end

local function run_lua_inline(code)
  if type(code) ~= "string" or #code == 0 then
    error("missing inline code", 0)
  end
  local load_fn = loadstring or load
  local loader, err = load_fn(code, "mgba_live_inline")
  if not loader then
    error(err, 0)
  end
  return loader()
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
    local width = cmd.width == nil and 4 or cmd.width
    if not start_addr or not count then
      error("dump_pointers requires start and count")
    end
    if type(width) ~= "number" or width ~= math.floor(width) or width < 1 or width > 6 then
      error("pointer width must be an integer from 1 to 6 bytes", 0)
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
    local result = run_lua_file(cmd.path)
    return { result = result == nil and json_null or result }
  end

  if kind == "run_lua_inline" then
    local result = run_lua_inline(cmd.code)
    return { result = result == nil and json_null or result }
  end

  error("unknown command: " .. tostring(kind))
end

local function process_command(cmd)
  local invalid = cmd.kind == "__invalid__"
  local ok, data
  if invalid then
    ok, data = false, cmd._error
  else
    ok, data = pcall(handle_command, cmd)
  end
  local response = {
    id = cmd.id or "unknown",
    session_id = cmd.session_id or session_dir:match("([^/\\]+)[/\\]*$"),
    ok = ok,
    frame = frame,
  }
  if ok then
    response.data = data
  else
    response.error = (data == nil or data == false)
        and "Lua command raised a non-text error." or data
  end
  local encoded, text
  if not ok and data ~= nil and data ~= false and type(data) ~= "string" then
    encoded, text = false, "unsupported_error"
  else
    encoded, text = pcall(json_encode, response)
  end
  if not encoded then
    local reason = type(text) == "string" and serialization_messages[text] and text or "unsupported_type"
    -- Only trusted request metadata and fixed text reach this independent fallback.
    -- Never include or tostring the offending Lua result/error object.
    encoded, text = pcall(json_encode, {
      id = response.id,
      session_id = response.session_id,
      ok = false,
      frame = frame,
      code = "serialization_failed",
      phase = "serialization",
      error = serialization_messages[reason],
      serialization_reason = reason,
      execution_outcome = ok and "completed" or (invalid and "not_executed" or "unknown"),
      command_completed = ok,
    })
  end
  if not encoded then
    io.stderr:write('{"code":"response_encoding_failed","phase":"serialization","execution_outcome":"unknown"}\n')
    return
  end
  local written = write_text(response_path, text)
  if not written then
    io.stderr:write('{"code":"response_write_failed","phase":"publish","execution_outcome":"unknown"}\n')
  end
end

local function write_heartbeat()
  write_json(heartbeat_path, {
    frame = frame,
    keys = emu:getKeys(),
    unix_time = os.time(),
    command_claim = "rename-v1",
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
