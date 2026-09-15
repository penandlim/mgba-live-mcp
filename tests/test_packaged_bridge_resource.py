from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
from importlib.resources import as_file, files
from pathlib import Path

import pytest
from mcp import types

from mgba_live_mcp import server
from mgba_live_mcp.live_controller import LiveControllerClient
from mgba_live_mcp.session_manager import SessionManager

# Only the emulator host API is substituted. Commands and responses pass through
# the unmodified packaged bridge and the real Lua interpreter/serializer.
HOST = """
C = { GBA_KEY = { A=0, B=1, SELECT=2, START=3, RIGHT=4, LEFT=5, UP=6, DOWN=7, R=8, L=9 } }
emu = {
  keys = 0, memory = {}, reads = 0,
  getKeys = function(self) return self.keys end,
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
    assert response["execution_outcome"] == ("partial" if completed else "unknown")
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
    assert isinstance(failure["error"], str)
    _recovered(responses[2], frame=3, result=0)


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
    assert error["execution_outcome"] == "partial"
    assert error["command_completed"] is True
    assert error["session_id"] == "running"
    assert error["request_id"] == published[0]["id"]
    assert error["frame"] == 1
    assert isinstance(result.content[0], types.TextContent)
    assert json.loads(result.content[0].text) == payload
    raw, _ = executions[0]
    _serialization_failure(raw[0], request_id=published[0]["id"])


def test_response_write_failure_keeps_transaction_unresolved(mcp_bridge):
    manager, _, executions = mcp_bridge
    (manager.session_dir("running") / "response.json.tmp").mkdir()
    result = _call_tool(
        "mgba_live_run_lua",
        {"session": "running", "code": "emu:setKeys(7); return true", "timeout": 0.01},
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
