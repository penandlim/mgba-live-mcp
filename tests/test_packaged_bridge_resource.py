from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
from importlib.resources import as_file, files
from pathlib import Path
from types import SimpleNamespace

import pytest
from mcp import types

from mgba_live_mcp import deadlines, inspections, server
from mgba_live_mcp.errors import DomainError
from mgba_live_mcp.inspections import MAX_ITEMS, MAX_READ_BYTES
from mgba_live_mcp.live_controller import LiveControllerClient
from mgba_live_mcp.session_manager import SessionManager

# Only the emulator host API is substituted. Commands and responses pass through
# the unmodified packaged bridge and the real Lua interpreter/serializer.
HOST = """
C = {
  GBA_KEY = { A=0, B=1, SELECT=2, START=3, RIGHT=4, LEFT=5, UP=6, DOWN=7, R=8, L=9 },
  PLATFORM = { GB=1, GBA=2 },
}
emu = {
  keys = 0, memory = {}, reads = 0, platform_id = 2,
  getKeys = function(self) return self.keys end,
  platform = function(self) return self.platform_id end,
  setKeys = function(self, keys) self.keys = keys end,
  read8 = function(self, address)
    self.reads = self.reads + 1
    return self.memory[address] or 0
  end,
}
callbacks = { add = function(self, event, fn) frame_callback = fn end }
dofile(arg[1])
for index = 1, tonumber(arg[2]) do
  assert(os.rename("command-" .. index .. ".lua", "command.lua"))
  frame_callback()
  os.rename("response.json", "response-" .. index .. ".json")
end
"""

MAX_RESPONSE_BYTES = 1_048_576


@pytest.fixture
def packaged_bridge(tmp_path):
    lua = shutil.which("lua5.4") or shutil.which("lua") or shutil.which("luajit")
    if lua is None:
        pytest.skip(
            "Packaged bridge regressions require a real Lua interpreter. "
            "Install lua5.4 (CI installs it), or install Lua with `brew install lua`."
        )
    manager = SessionManager(runtime_root=tmp_path)
    manager.ensure_runtime_dirs()
    host = tmp_path / "host.lua"
    host.write_text(HOST)
    original_write = manager.write_command
    bridge = files("mgba_live_mcp").joinpath("resources/mgba_live_bridge.lua")

    with as_file(bridge) as bridge_path:

        def execute(*commands, session="running", env=None, **kwargs):
            directory = manager.session_dir(session)
            directory.mkdir(parents=True, exist_ok=True)
            command_path = directory / "command.lua"
            response_path = directory / "response.json"
            response_path.unlink(missing_ok=True)
            for index, command in enumerate(commands, 1):
                (directory / f"response-{index}.json").unlink(missing_ok=True)
                original_write(command_path, command, **kwargs)
                command_path.rename(directory / f"command-{index}.lua")
            process = subprocess.run(
                [lua, str(host), str(bridge_path), str(len(commands))],
                cwd=directory,
                env={**os.environ, "MGBA_LIVE_SESSION_DIR": str(directory), **(env or {})},
                check=True,
                capture_output=True,
                text=True,
                timeout=10,
            )
            responses = [
                path.read_bytes() if path.exists() else None
                for index in range(1, len(commands) + 1)
                for path in [directory / f"response-{index}.json"]
            ]
            # Preserve the actual primary response for the manager/MCP caller.
            if responses[0] is not None:
                shutil.copyfile(directory / "response-1.json", response_path)
            return responses, process

        yield manager, execute


@pytest.fixture
def mcp_bridge(packaged_bridge, monkeypatch):
    manager, execute = packaged_bridge
    manager.session_dir("running").mkdir()
    published = []
    executions = []

    def target(session, **kwargs):
        directory = manager.session_dir(session)
        return {
            "id": session,
            "command_path": str(directory / "command.lua"),
            "response_path": str(directory / "response.json"),
        }

    def publish(path: Path, command, **kwargs):
        published.append(command)
        executions.append(execute(command, session=path.parent.name, **kwargs))

    monkeypatch.setattr(manager, "write_command", publish)
    monkeypatch.setattr(manager, "require_session", target)
    monkeypatch.setattr(manager, "start", lambda **k: {"session_id": k["session_id"], "pid": 42})
    monkeypatch.setattr(server, "_controller", LiveControllerClient(manager))
    return manager, published, executions


def _call_tool(name, arguments):
    async def invoke():
        request = types.CallToolRequest(
            method="tools/call",
            params=types.CallToolRequestParams(name=name, arguments=arguments),
        )
        return (await server.server.request_handlers[types.CallToolRequest](request)).root

    return asyncio.run(invoke())


def _command(kind="run_lua_inline", *, request_id="primary", **payload):
    return {"id": request_id, "session_id": "running", "kind": kind, **payload}


def _json_response(raw):
    assert isinstance(raw, bytes), "The bridge did not publish a response"
    assert len(raw) <= MAX_RESPONSE_BYTES
    return json.loads(
        raw.decode("utf-8"),
        parse_constant=lambda value: pytest.fail(f"Non-JSON numeric constant: {value}"),
    )


def _serialization_failure(raw, *, request_id="primary", frame=1, completed=True):
    response = _json_response(raw)
    assert response["id"] == request_id
    assert response["session_id"] == "running"
    assert response["ok"] is False
    assert response["frame"] == frame
    assert response["code"] == "serialization_failed"
    assert response["phase"] == "serialization"
    assert response["execution_outcome"] == ("completed" if completed else "unknown")
    assert response["command_completed"] is completed
    assert isinstance(response["error"], str) and 0 < len(response["error"]) <= 256
    reason = response["serialization_reason"]
    assert isinstance(reason, str) and reason.isascii() and 0 < len(reason) <= 64
    assert "data" not in response
    assert len(raw) <= 1024
    return response


def _recovered(raw, *, frame, result):
    response = _json_response(raw)
    assert response["id"] == "recovered"
    assert response["session_id"] == "running"
    assert response["ok"] is True
    assert response["frame"] == frame
    assert response["data"]["result"] == result


@pytest.mark.parametrize(
    "session_dir", ["/runtime/sessions/running/", "C:\\runtime\\sessions\\running\\"]
)
def test_legacy_session_correlation_uses_directory_name(packaged_bridge, session_dir):
    manager, execute = packaged_bridge
    directory = manager.session_dir("running")
    command = _command(code="local value={}; value.self=value; return value")
    del command["session_id"]
    responses, _ = execute(
        command,
        _command(request_id="recovered", code="return true"),
        env={
            "MGBA_LIVE_SESSION_DIR": session_dir,
            "MGBA_LIVE_COMMAND": str(directory / "command.lua"),
            "MGBA_LIVE_RESPONSE": str(directory / "response.json"),
            "MGBA_LIVE_HEARTBEAT": str(directory / "heartbeat.json"),
        },
    )
    _serialization_failure(responses[0])
    _recovered(responses[1], frame=2, result=True)


@pytest.mark.parametrize("source", ["code", "file"])
@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("return nil", None),
        ("return false", False),
        ("return 0", 0),
        ('return ""', ""),
        ("return {}", []),
        ('return {0, false, ""}', [0, False, ""]),
        (
            "local shared={value=false}; return {left=shared, right=shared}",
            {"left": {"value": False}, "right": {"value": False}},
        ),
        ('return {["한글😀"]="é漢🙂"}', {"한글😀": "é漢🙂"}),
        (
            "local s=string.char(34,92); for i=0,31 do s=s..string.char(i) end; return s",
            '"\\' + "".join(chr(index) for index in range(32)),
        ),
        (
            "local poison=function() error('metamethod was invoked') end; "
            "local mt={__pairs=poison,__len=poison,__index=poison,__tostring=poison}; "
            "return {array=setmetatable({1,2},mt), object=setmetatable({value=false},mt)}",
            {"array": [1, 2], "object": {"value": False}},
        ),
    ],
)
def test_packaged_lua_results_reach_mcp(tmp_path, mcp_bridge, source, code, expected):
    script = tmp_path / "user.lua"
    script.write_text(code)
    for startup in (False, True):
        arguments = {source: str(script) if source == "file" else code}
        if startup:
            arguments.update(
                rom=str(Path(__file__).parent / "fixtures" / "synthetic.gb"),
                mgba_path=sys.executable,
                session_id="boot",
            )
        else:
            arguments["session"] = "running"
        name = "mgba_live_start_with_lua" if startup else "mgba_live_run_lua"
        result = _call_tool(name, arguments)
        assert isinstance(result, types.CallToolResult)
        assert not result.isError, result.structuredContent
        payload = result.structuredContent
        assert payload is not None
        actual = payload["lua"] if startup else payload["data"]["result"]
        assert actual == expected and type(actual) is type(expected)
        first = result.content[0]
        assert isinstance(first, types.TextContent)
        assert json.loads(first.text) == payload


@pytest.mark.parametrize(
    ("code", "completed"),
    [
        ("local t={}; t.self=t; return t", True),
        ("return string.char(255)", True),
        ("return string.char(0xED, 0xA0, 0x80)", True),
        ("utf8=nil; return string.char(0xC0, 0x80)", True),
        ("return {value=function() end}", True),
        ("return {[{}]=1}", True),
        ("return {[1]=true, ['1']=false}", True),
        ("return {[2]=true}", True),
        ("return 0/0", True),
        ("return math.huge", True),
        ("error({kind='structured', detail='not text'})", False),
        ("error(function() end)", False),
        ("error(string.char(255))", False),
        ("error(string.rep('x', 1048577))", False),
    ],
)
def test_packaged_bridge_rejects_unserializable_results_and_recovers(
    packaged_bridge, code, completed
):
    _, execute = packaged_bridge
    first, _ = execute(
        _command(code=code),
        _command(request_id="recovered", code="return {ok=true}"),
    )
    _serialization_failure(first[0], completed=completed)
    _recovered(first[1], frame=2, result={"ok": True})


@pytest.mark.parametrize("code", ["error(false)", "error(nil)"])
def test_packaged_bridge_preserves_false_and_nil_lua_errors(packaged_bridge, code):
    _, execute = packaged_bridge
    responses, _ = execute(
        _command(code=code),
        _command(request_id="recovered", code="return true"),
    )
    failure = _json_response(responses[0])
    assert failure["id"] == "primary"
    assert failure["ok"] is False
    assert isinstance(failure["error"], str)
    assert _json_response(responses[1])["data"]["result"] is True


def test_packaged_bridge_exact_large_integer_and_controls(packaged_bridge):
    _, execute = packaged_bridge
    responses, _ = execute(
        _command(code="return {n=281474976710655, s='line\\n\\tquote\"slash\\\\'}"),
        _command(request_id="recovered", code="return true"),
    )
    result = _json_response(responses[0])["data"]["result"]
    assert result == {"n": 281474976710655, "s": 'line\n\tquote"slash\\'}
    assert _json_response(responses[1])["data"]["result"] is True


def test_packaged_bridge_pointer_reads_remain_exact_at_48_bits(packaged_bridge):
    _, execute = packaged_bridge
    responses, _ = execute(
        _command(code="for i=0,5 do emu.memory[i]=255; emu.memory[6+i]=i+1 end"),
        _command(kind="dump_pointers", start=0, count=2, width=6),
        _command(kind="dump_pointers", start=0, count=1, width=4),
        _command(request_id="recovered", code="return emu.reads"),
    )
    wide = _json_response(responses[1])["data"]["pointers"]
    assert [item["value"] for item in wide] == [
        2**48 - 1,
        int.from_bytes(bytes(range(1, 7)), "little"),
    ]
    assert all(type(item["value"]) is int for item in wide)
    assert _json_response(responses[2])["data"]["pointers"][0]["value"] == 2**32 - 1
    _recovered(responses[3], frame=4, result=16)


def test_pointer_reads_preserve_ascending_mmio_order(packaged_bridge):
    _, execute = packaged_bridge
    responses, _ = execute(
        _command(
            code="emu.order={}; emu.read8=function(self,address) "
            "self.order[#self.order+1]=address; return #self.order end"
        ),
        _command(kind="dump_pointers", start=16, count=1, width=4),
        _command(request_id="recovered", code="return emu.order"),
    )
    assert _json_response(responses[1])["data"]["pointers"][0]["value"] == 0x04030201
    _recovered(responses[2], frame=3, result=[16, 17, 18, 19])


@pytest.mark.parametrize("width", [7, 8])
def test_packaged_bridge_rejects_unsupported_pointer_width(packaged_bridge, width):
    _, execute = packaged_bridge
    memory = b"".join(value.to_bytes(width, "little") for value in (2**53 - 1, 2**53, 2**53 + 1))
    initialization = "; ".join(
        f"emu.memory[{address}]={value}" for address, value in enumerate(memory)
    )
    responses, _ = execute(
        _command(code=initialization),
        _command(kind="dump_pointers", start=0, count=3, width=width),
        _command(request_id="recovered", code="return emu.reads"),
    )
    failure = _json_response(responses[1])
    assert failure["ok"] is False
    assert failure["code"] == "invalid_arguments"
    assert isinstance(failure["error"], str)
    _recovered(responses[2], frame=3, result=0)


def test_packaged_bridge_range_hex_and_delta_are_lossless(packaged_bridge):
    _, execute = packaged_bridge
    current = bytes(range(256)) * 8  # Fits worst-case delta expansion as well as source limits.
    partial = bytearray(current)
    for offset in (0, 1, 1023, len(current) - 1):
        partial[offset] ^= 255
    baselines = [
        current,
        partial,
        bytes(value ^ 255 for value in current),
        bytes(value ^ (255 if i % 2 == 0 else 0) for i, value in enumerate(current)),
    ]
    responses, _ = execute(
        _command(code=f"for i=0,{len(current) - 1} do emu.memory[i]=i%256 end"),
        _command(kind="read_range", start=0, length=len(current)),
        _command(kind="read_range", start=0, length=len(current), encoding="hex"),
        *[
            _command(
                kind="read_range",
                start=0,
                length=len(current),
                encoding="delta",
                baseline={"start": 0, "data": baseline.hex().upper()},
            )
            for baseline in baselines
        ],
        _command(request_id="recovered", code="return emu.reads"),
    )
    assert _json_response(responses[1])["data"]["data"] == list(current)
    encoded = _json_response(responses[2])["data"]
    assert encoded["start"] == 0 and encoded["length"] == len(current)
    assert encoded["encoding"] == "hex" and bytes.fromhex(encoded["data"]) == current
    assert len(responses[2]) < len(responses[1])
    for baseline, raw in zip(baselines, responses[3:7], strict=True):
        result = _json_response(raw)["data"]
        assert result["encoding"] == "delta"
        assert result["start"] == 0 and result["length"] == len(current)
        restored = bytearray(baseline)
        previous_end = -1
        for span in result["spans"]:
            offset, changed = span["offset"], bytes.fromhex(span["data"])
            assert offset > previous_end
            assert all(baseline[offset + i] != value for i, value in enumerate(changed))
            restored[offset : offset + len(changed)] = changed
            previous_end = offset + len(changed)
        assert restored == current
    assert _json_response(responses[3])["data"]["spans"] == []
    assert _json_response(responses[4])["data"]["spans"] == [
        {"offset": 0, "data": "0001"},
        {"offset": 1023, "data": "ff"},
        {"offset": len(current) - 1, "data": "ff"},
    ]
    assert _json_response(responses[5])["data"]["spans"] == [{"offset": 0, "data": current.hex()}]
    assert len(_json_response(responses[6])["data"]["spans"]) == len(current) // 2
    assert _json_response(responses[7])["data"]["result"] == 6 * len(current)


@pytest.mark.parametrize(
    ("kind", "valid", "invalid", "byte_count", "limit_name"),
    [
        (
            "read_memory",
            {"addresses": list(range(MAX_ITEMS))},
            {"addresses": list(range(MAX_ITEMS + 1))},
            MAX_ITEMS,
            "items",
        ),
        (
            "read_range",
            {"start": 0, "length": MAX_READ_BYTES},
            {"start": 0, "length": MAX_READ_BYTES + 1},
            MAX_READ_BYTES,
            "read_bytes",
        ),
        (
            "dump_pointers",
            {"start": 0, "count": 512, "width": 4},
            {"start": 0, "count": MAX_ITEMS, "width": 5},
            2048,
            "read_bytes",
        ),
        (
            "dump_pointers",
            {"start": 0, "count": 512, "width": 1},
            {"start": 0, "count": MAX_ITEMS + 1, "width": 1},
            512,
            "items",
        ),
        (
            "dump_entities",
            {"base": 0, "count": 1, "size": MAX_READ_BYTES},
            {"base": 0, "count": MAX_ITEMS, "size": 5},
            MAX_READ_BYTES,
            "read_bytes",
        ),
        (
            "dump_entities",
            {"base": 0, "count": 512, "size": 1},
            {"base": 0, "count": MAX_ITEMS + 1, "size": 1},
            512,
            "items",
        ),
    ],
)
def test_inspection_limits_preserve_complete_results_and_recovery(
    packaged_bridge, kind, valid, invalid, byte_count, limit_name
):
    _, execute = packaged_bridge
    responses, _ = execute(
        _command(kind=kind, **valid),
        _command(kind=kind, request_id="oversized", **invalid),
        _command(code="return emu.reads"),
        _command(kind="read_range", start=0, length=1),
        _command(request_id="recovered", code="return emu.reads"),
    )
    accepted = _json_response(responses[0])
    assert accepted["ok"] is True
    data = accepted["data"]
    if kind == "read_memory":
        assert data == {f"0x{address:08X}": 0 for address in valid["addresses"]}
    elif kind == "read_range":
        assert data["data"] == [0] * byte_count
    elif kind == "dump_pointers":
        assert data["pointers"] == [
            {"index": i, "address": i * valid["width"], "value": 0} for i in range(valid["count"])
        ]
    else:
        assert data["entities"] == [
            {"index": i, "address": i * valid["size"], "bytes": [0] * valid["size"]}
            for i in range(valid["count"])
        ]
    failure = _json_response(responses[1])
    assert failure["code"] == "inspection_limit"
    assert failure["id"] == "oversized" and failure["session_id"] == "running"
    assert failure["phase"] == "validation" and failure["execution_outcome"] == "not_executed"
    assert failure["limit_name"] == limit_name
    assert failure["limit"] == (MAX_ITEMS if limit_name == "items" else MAX_READ_BYTES)
    assert failure["max_response_bytes"] == inspections.MAX_RESPONSE_BYTES
    assert _json_response(responses[2])["data"]["result"] == byte_count
    assert _json_response(responses[3])["data"]["data"] == [0]
    _recovered(responses[4], frame=5, result=byte_count + 1)


@pytest.mark.parametrize(
    ("kind", "selection", "oversized", "byte_count"),
    [
        (
            "dump_pointers",
            {"start": 0xFFFF0000, "count": 512, "width": 6},
            {"start": 0xFFFF0000, "count": 513, "width": 6},
            3072,
        ),
        (
            "dump_entities",
            {"base": 0xFFFF0000, "count": 480, "size": 4},
            {"base": 0xFFFF0000, "count": 481, "size": 4},
            1920,
        ),
        (
            "read_range",
            {
                "start": 0xFFFF0000,
                "length": 2048,
                "encoding": "delta",
                "baseline": {"start": 0xFFFF0000, "data": "00ff" * 1024},
            },
            {
                "start": 0xFFFF0000,
                "length": 2049,
                "encoding": "delta",
                "baseline": {"start": 0xFFFF0000, "data": "00ff" * 1024 + "00"},
            },
            2048,
        ),
    ],
)
def test_response_expansion_budget_rejects_before_reads(
    packaged_bridge, kind, selection, oversized, byte_count
):
    manager, execute = packaged_bridge
    responses, _ = execute(
        _command(code="emu.read8=function(self,address) self.reads=self.reads+1; return 255 end"),
        _command(kind=kind, request_id="oversized-output", **oversized),
        _command(code="return emu.reads"),
        _command(kind=kind, **selection),
        _command(request_id="recovered", code="return emu.reads"),
    )
    failure = _json_response(responses[1])
    assert failure["ok"] is False and failure["code"] == "inspection_limit"
    assert failure["limit_name"] == "response_bytes" and failure["limit"] == 32768
    assert failure["id"] == "oversized-output" and failure["session_id"] == "running"
    assert failure["execution_outcome"] == "not_executed" and failure["phase"] == "validation"
    assert _json_response(responses[2])["data"]["result"] == 0
    with pytest.raises(DomainError) as caught:
        manager.handle_response(failure, session_id="running")
    assert caught.value.code == "inspection_limit"
    assert caught.value.context["request_id"] == "oversized-output"
    assert caught.value.context["limit_name"] == "response_bytes"
    accepted = _json_response(responses[3])
    assert accepted["ok"] is True and len(responses[3]) <= 32768
    data = accepted["data"]
    if kind == "dump_pointers":
        assert data["pointers"] == [
            {"index": i, "address": selection["start"] + i * 6, "value": 2**48 - 1}
            for i in range(selection["count"])
        ]
    elif kind == "dump_entities":
        assert data["entities"] == [
            {"index": i, "address": selection["base"] + i * 4, "bytes": [255] * 4}
            for i in range(selection["count"])
        ]
    else:
        assert data["spans"] == [{"offset": i, "data": "ff"} for i in range(0, byte_count, 2)]
    _recovered(responses[4], frame=5, result=byte_count)


@pytest.mark.parametrize("field", ["session_id", "request_id"])
def test_escaped_metadata_counts_toward_inspection_budget(packaged_bridge, tmp_path, field):
    _, execute = packaged_bridge
    oversized = {field: "\x01" * 6000}
    fitting = {field: "\x01" * 4000}
    responses, _ = execute(
        _command(kind="read_range", start=0, length=1, **oversized),
        _command(code="return emu.reads"),
        _command(kind="read_range", start=0, length=1, **fitting),
        _command(request_id="recovered", code="return emu.reads"),
    )
    failure = _json_response(responses[0])
    assert failure["ok"] is False and failure["code"] == "inspection_limit"
    assert failure["limit_name"] == "response_bytes"
    assert failure["session_id"] == oversized.get("session_id", "running")
    assert failure["id"] == oversized.get("request_id", "primary")
    assert _json_response(responses[1])["data"]["result"] == 0
    assert _json_response(responses[2])["data"]["data"] == [0]
    assert len(responses[2]) <= inspections.MAX_RESPONSE_BYTES
    _recovered(responses[3], frame=4, result=1)
    if field == "session_id":
        manager = SessionManager(runtime_root=tmp_path / "untouched")
        with pytest.raises(DomainError) as caught:
            manager.read_range(session=oversized[field], start=0, length=1)
        assert caught.value.code == "inspection_limit"
        assert not manager.runtime_root.exists()


def test_inspection_preflight_rejects_every_selection_before_reading(packaged_bridge):
    _, execute = packaged_bridge
    invalid = [
        _command(kind="read_memory", addresses=[]),
        _command(kind="read_memory", addresses=[0, 0x10000]),
        _command(kind="read_memory", addresses=[True]),
        _command(kind="read_range", start=0xFFFF, length=2),
        *[_command(kind="read_range", start=0, length=value) for value in (0, -1, 1.5, True)],
        _command(kind="dump_pointers", start=0xFFFF, count=1, width=2),
        _command(kind="dump_entities", base=0xFFFF, count=1, size=2),
        _command(kind="read_range", start=-1, length=1),
        _command(kind="read_range", start=0, length=1, encoding="base64"),
        _command(kind="read_range", start=0, length=1, encoding="delta"),
        _command(
            kind="read_range",
            start=0,
            length=1,
            encoding="delta",
            baseline={"start": 1, "data": "00"},
        ),
        _command(
            kind="read_range",
            start=0,
            length=2,
            encoding="delta",
            baseline={"start": 0, "data": "00"},
        ),
        _command(
            kind="read_range",
            start=0,
            length=1,
            encoding="delta",
            baseline={"start": 0, "data": "0g"},
        ),
        _command(
            kind="read_range",
            start=0,
            length=1,
            encoding="hex",
            baseline={"start": 0, "data": "00"},
        ),
    ]
    responses, _ = execute(
        _command(code="emu.platform_id=C.PLATFORM.GB"),
        *invalid,
        _command(request_id="recovered", code="return emu.reads"),
    )
    for raw in responses[1:-1]:
        response = _json_response(raw)
        assert response["ok"] is False and response["code"] == "invalid_arguments"
        assert response["execution_outcome"] == "not_executed"
    _recovered(responses[-1], frame=len(responses), result=0)


@pytest.mark.parametrize(("platform", "end"), [("GB", 0xFFFF), ("GBA", 0xFFFFFFFF)])
def test_inspection_platform_last_byte_never_wraps(packaged_bridge, platform, end):
    _, execute = packaged_bridge
    responses, _ = execute(
        _command(code=f"emu.platform_id=C.PLATFORM.{platform}; emu.memory[{end}]=123"),
        _command(kind="read_range", start=end, length=1),
        _command(kind="read_range", start=end, length=2),
        _command(kind="read_memory", addresses=[0, end + 1]),
        _command(request_id="recovered", code="return emu.reads"),
    )
    assert _json_response(responses[1])["data"]["data"] == [123]
    assert all(_json_response(raw)["code"] == "invalid_arguments" for raw in responses[2:4])
    _recovered(responses[4], frame=5, result=1)


@pytest.mark.parametrize(
    "unavailable", ["emu.platform_id=99", "emu.platform=nil", "C.PLATFORM=nil"]
)
def test_unknown_inspection_platform_is_not_guessed(packaged_bridge, unavailable):
    _, execute = packaged_bridge
    responses, _ = execute(
        _command(code=unavailable),
        _command(kind="read_range", start=0, length=1),
        _command(request_id="recovered", code="return emu.reads"),
    )
    failure = _json_response(responses[1])
    assert failure["code"] == "inspection_unsupported"
    assert failure["execution_outcome"] == "not_executed"
    _recovered(responses[2], frame=3, result=0)


@pytest.mark.parametrize("failure", ["return nil", "return 256", "error('unmapped')"])
def test_failed_native_byte_is_not_zero_filled_or_partial_success(packaged_bridge, failure):
    _, execute = packaged_bridge
    responses, _ = execute(
        _command(
            code="saved_read8=emu.read8; emu.read8=function(self, address) "
            f"if address==1 then {failure} end; return saved_read8(self,address) end"
        ),
        _command(kind="read_range", start=0, length=2),
        _command(code="emu.read8=saved_read8"),
        _command(kind="read_range", start=0, length=2),
    )
    rejected = _json_response(responses[1])
    assert rejected["code"] == "inspection_read_failed"
    assert rejected["phase"] == "inspection" and rejected["execution_outcome"] == "unknown"
    assert "data" not in rejected
    assert _json_response(responses[3])["data"]["data"] == [0, 0]


@pytest.mark.parametrize(
    ("kind", "payload", "code"),
    [
        ("read_memory", {"addresses": []}, "invalid_arguments"),
        ("read_memory", {"addresses": [True]}, "invalid_arguments"),
        ("read_range", {"start": 0, "length": MAX_READ_BYTES + 1}, "inspection_limit"),
        ("read_range", {"start": 0xFFFFFFFF, "length": 2}, "invalid_arguments"),
        ("read_range", {"start": 0, "length": 1.5}, "invalid_arguments"),
        ("dump_pointers", {"start": 0, "count": MAX_ITEMS, "width": 5}, "inspection_limit"),
        ("dump_entities", {"base": 0, "count": 2, "size": MAX_READ_BYTES}, "inspection_limit"),
        ("dump_pointers", {"start": 0, "count": 513, "width": 6}, "inspection_limit"),
        ("dump_entities", {"base": 0, "count": 481, "size": 4}, "inspection_limit"),
        (
            "read_range",
            {
                "start": 0,
                "length": 2049,
                "encoding": "delta",
                "baseline": {"start": 0, "data": "00" * 2049},
            },
            "inspection_limit",
        ),
        (
            "read_range",
            {"start": 0, "length": 1, "encoding": "delta", "baseline": {"start": 1, "data": "00"}},
            "invalid_arguments",
        ),
    ],
)
def test_host_inspection_rejection_precedes_session_allocation(tmp_path, kind, payload, code):
    manager = SessionManager(runtime_root=tmp_path / "untouched")
    with pytest.raises(DomainError) as raised:
        getattr(manager, kind)(session="running", **payload)
    assert raised.value.code == code and raised.value.execution_outcome == "not_executed"
    assert raised.value.context["session_id"] == "running"
    assert not manager.runtime_root.exists()


def test_inspection_encoding_and_limit_errors_reach_mcp(mcp_bridge):
    _, published, _ = mcp_bridge
    arguments = {"session": "running", "start": 0, "length": 2, "encoding": "hex"}
    encoded = _call_tool("mgba_live_read_range", arguments)
    assert not encoded.isError
    assert encoded.structuredContent["range"] == {
        "start": 0,
        "length": 2,
        "encoding": "hex",
        "data": "0000",
    }
    delta = _call_tool(
        "mgba_live_read_range",
        {**arguments, "encoding": "delta", "baseline": {"start": 0, "data": "00ff"}},
    )
    assert not delta.isError
    assert delta.structuredContent["range"]["spans"] == [{"offset": 1, "data": "00"}]
    for tool_name, selection, limit in (
        ("read_range", {"start": 0, "length": MAX_READ_BYTES + 1}, MAX_READ_BYTES),
        ("read_memory", {"addresses": [0] * (MAX_ITEMS + 1)}, MAX_ITEMS),
        ("dump_pointers", {"start": 0, "count": MAX_ITEMS + 1}, MAX_ITEMS),
        ("dump_entities", {"count": MAX_ITEMS + 1}, MAX_ITEMS),
        ("dump_entities", {"base": 0, "count": 1024, "size": 4}, inspections.MAX_RESPONSE_BYTES),
        ("dump_pointers", {"start": 0, "count": 513}, inspections.MAX_RESPONSE_BYTES),
        (
            "read_range",
            {
                "start": 0,
                "length": 2049,
                "encoding": "delta",
                "baseline": {"start": 0, "data": "00" * 2049},
            },
            inspections.MAX_RESPONSE_BYTES,
        ),
    ):
        rejected = _call_tool(f"mgba_live_{tool_name}", {"session": "running", **selection})
        assert (
            rejected.isError and rejected.structuredContent["error"]["code"] == "inspection_limit"
        )
        assert rejected.structuredContent["error"]["limit"] == limit
    assert len(published) == 2


def test_packaged_bridge_depth_limit_boundary_and_recovery(packaged_bridge):
    _, execute = packaged_bridge
    responses, _ = execute(
        _command(code="local t={}; for i=1,29 do t={child=t} end; return t"),
        _command(code="local t={}; for i=1,30 do t={child=t} end; return t"),
        _command(request_id="recovered", code="return true"),
    )
    value = _json_response(responses[0])["data"]["result"]
    for _ in range(29):
        value = value["child"]
    assert value == []
    _serialization_failure(responses[1], frame=2)
    _recovered(responses[2], frame=3, result=True)


def test_packaged_bridge_entry_limit_counts_shared_subtrees(packaged_bridge):
    _, execute = packaged_bridge
    responses, _ = execute(
        _command(code="local t={}; for i=1,9994 do t[i]=true end; return t"),
        _command(code="local t={}; for i=1,9995 do t[i]=true end; return t"),
        _command(code="local t={}; for i=1,5000 do t[i]=true end; return {left=t,right=t}"),
        _command(request_id="recovered", code="return true"),
    )
    assert _json_response(responses[0])["data"]["result"] == [True] * 9994
    _serialization_failure(responses[1], frame=2)
    _serialization_failure(responses[2], frame=3)
    _recovered(responses[3], frame=4, result=True)


@pytest.mark.parametrize("escaped", [False, True])
def test_packaged_bridge_encoded_byte_limit_boundary(packaged_bridge, escaped):
    _, execute = packaged_bridge
    envelope = {
        "id": "primary",
        "session_id": "running",
        "ok": True,
        "frame": 1,
        "data": {"result": ""},
    }
    available = MAX_RESPONSE_BYTES - len(json.dumps(envelope, separators=(",", ":")).encode())
    count, remainder = divmod(available, 6 if escaped else 1)
    character = "string.char(0)" if escaped else "'x'"
    expression = f"string.rep({character},{count}) .. string.rep('x',{remainder})"
    responses, _ = execute(
        _command(code=f"return {expression}"),
        _command(code=f"return {expression} .. 'x'"),
        _command(request_id="recovered", code="return true"),
    )
    assert len(responses[0]) == MAX_RESPONSE_BYTES
    assert _json_response(responses[0])["data"]["result"] == (
        ("\0" if escaped else "x") * count + "x" * remainder
    )
    _serialization_failure(responses[1], frame=2)
    _recovered(responses[2], frame=3, result=True)


def test_packaged_bridge_portable_utf8_round_trip(packaged_bridge):
    _, execute = packaged_bridge
    responses, _ = execute(_command(code='utf8=nil; return "é漢😀"'))
    assert _json_response(responses[0])["data"]["result"] == "é漢😀"


@pytest.mark.parametrize("as_key", [False, True])
def test_surrogates_fail_even_with_permissive_native_utf8(packaged_bridge, as_key):
    _, execute = packaged_bridge
    # Lua 5.3 accepts surrogates; newer Lua's real decoder exposes that mode as "lax".
    code = (
        "if utf8 then local len=utf8.len; "
        "utf8.len=function(value) return len(value,1,-1,true) end end; "
        "local invalid=string.char(0xED,0xA0,0x80); "
        + ("return {[invalid]=true}" if as_key else "return invalid")
    )
    responses, _ = execute(
        _command(code=code),
        _command(
            request_id="recovered",
            code="return string.char(0xED,0x9F,0xBF,0xEE,0x80,0x80,0xF4,0x8F,0xBF,0xBF)",
        ),
    )
    _serialization_failure(responses[0])
    _recovered(responses[1], frame=2, result="\ud7ff\ue000\U0010ffff")


def test_mutation_survives_rejected_result_without_replay(packaged_bridge):
    _, execute = packaged_bridge
    responses, _ = execute(
        _command(code="emu:setKeys(7); local t={}; t.self=t; return t"),
        _command(request_id="recovered", code="return emu:getKeys()"),
    )
    _serialization_failure(responses[0], completed=True)
    _recovered(responses[1], frame=2, result=7)


def test_completed_mutation_serialization_failure_reaches_mcp(mcp_bridge):
    _, published, executions = mcp_bridge
    result = _call_tool(
        "mgba_live_run_lua",
        {"session": "running", "code": "emu:setKeys(7); local t={}; t.self=t; return t"},
    )
    assert isinstance(result, types.CallToolResult)
    assert result.isError
    payload = result.structuredContent
    assert payload is not None
    error = payload["error"]
    assert error["code"] == "serialization_failed"
    assert error["phase"] == "serialization"
    assert error["execution_outcome"] == "completed"
    assert error["command_completed"] is True
    assert error["session_id"] == "running"
    assert error["request_id"] == published[0]["id"]
    assert error["frame"] == 1
    assert isinstance(result.content[0], types.TextContent)
    assert json.loads(result.content[0].text) == payload
    raw, _ = executions[0]
    _serialization_failure(raw[0], request_id=published[0]["id"])


def test_response_write_failure_keeps_transaction_unresolved(mcp_bridge, monkeypatch):
    manager, _, executions = mcp_bridge
    (manager.session_dir("running") / "response.json.tmp").mkdir()
    # Expire after Lua attempts publication, not during subprocess startup.
    clock = SimpleNamespace(monotonic=lambda: 0.0)
    monkeypatch.setattr(deadlines, "time", clock)
    original_publish = manager.write_command

    def publish(path, command, **kwargs):
        original_publish(path, command, **kwargs)
        clock.monotonic = lambda: 1.0

    monkeypatch.setattr(manager, "write_command", publish)
    result = _call_tool(
        "mgba_live_run_lua",
        {"session": "running", "code": "emu:setKeys(7); return true", "timeout": 1.0},
    )
    assert isinstance(result, types.CallToolResult)
    assert result.isError
    payload = result.structuredContent
    assert payload is not None
    error = payload["error"]
    assert error["code"] == "command_timeout"
    assert error["execution_outcome"] == "unknown"
    raw, process = executions[0]
    assert raw == [None]
    diagnostic = json.loads(process.stderr)
    assert diagnostic["code"] == "response_write_failed"
    assert diagnostic["phase"] == "publish"
    assert len(process.stderr.encode()) <= 256
    again = _call_tool("mgba_live_run_lua", {"session": "running", "code": "return true"})
    assert isinstance(again, types.CallToolResult)
    assert again.isError
    assert again.structuredContent is not None
    assert again.structuredContent["error"]["code"] == "session_busy"
