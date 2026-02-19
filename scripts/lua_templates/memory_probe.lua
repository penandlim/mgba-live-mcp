-- Memory probe template.
-- Reads a few sample addresses and returns a compact report.

local addresses = {
  0x02000000,
  0x02000001,
  0x03000000,
  0x04000000,
}

local bytes = {}
for _, addr in ipairs(addresses) do
  bytes[string.format("0x%08X", addr)] = emu:read8(addr)
end

local function read_u16(addr)
  local lo = emu:read8(addr)
  local hi = emu:read8(addr + 1)
  return lo + hi * 256
end

return {
  status = "ok",
  sample_bytes = bytes,
  sample_u16 = {
    [string.format("0x%08X", 0x02000000)] = read_u16(0x02000000),
    [string.format("0x%08X", 0x03000000)] = read_u16(0x03000000),
  },
}
