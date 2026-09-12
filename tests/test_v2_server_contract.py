from __future__ import annotations

import asyncio
import base64
import json
import os
import subprocess
import sys
from importlib.metadata import version
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client

from mgba_live_mcp import process_control, server, session_transactions
from mgba_live_mcp.errors import ERROR_CODES, DomainError
from mgba_live_mcp.live_controller import LiveControllerClient
from mgba_live_mcp.session_manager import SessionManager

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aDQAAAABJRU5ErkJggg=="
)
ROM = Path(__file__).parent / "fixtures" / "synthetic.gb"


def invoke(name: str, arguments: dict[str, Any]) -> types.CallToolResult:
    request = types.CallToolRequest(
        method="tools/call", params=types.CallToolRequestParams(name=name, arguments=arguments)
    )

    async def registered():
        return await server.server.request_handlers[types.CallToolRequest](request)

    result = asyncio.run(registered()).root
    assert isinstance(result, types.CallToolResult)
    text = result.content[0]
    assert isinstance(text, types.TextContent)
    assert json.loads(text.text) == result.structuredContent
    assert text.text == json.dumps(result.structuredContent, separators=(",", ":"))
    return result


def structured(result: types.CallToolResult) -> dict[str, Any]:
    assert result.structuredContent is not None
    return result.structuredContent


def first_text(result: types.CallToolResult) -> str:
    block = result.content[0]
    assert isinstance(block, types.TextContent)
    return block.text


@pytest.fixture
def runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SessionManager:
    """Run the real manager/adapter; substitute only process and bridge boundaries."""
    manager = SessionManager(runtime_root=tmp_path)
    frame = 0

    def bridge(target: dict[str, Any], kind: str, payload=None, **kwargs: Any):
        nonlocal frame
        frame += 10
        payload = payload or {}
        data: Any = {"result": {"ok": True}}
        if kind == "screenshot":
            Path(payload["path"]).write_bytes(PNG)
            data = {"path": payload["path"]}
        elif kind == "tap_key":
            data = {"key": 0, "duration": payload["duration"]}
        elif kind == "set_keys" or (kind == "clear_keys" and payload.get("keys")):
            data = {"keys": [0] if payload.get("keys") else []}
        elif kind == "clear_keys":
            data = {"cleared": "all"}
        elif kind == "read_memory":
            data = {"0x00000001": 0} if payload["addresses"] else []
        elif kind == "read_range":
            data = {"start": payload["start"], "length": 2, "data": [0, 255]}
        elif kind == "dump_pointers":
            data = {
                "start": payload["start"],
                "count": 1,
                "width": 4,
                "pointers": [{"index": 0, "address": payload["start"], "value": 0}],
            }
        elif kind == "dump_oam":
            data = {
                "base": 0x07000000,
                "count": 1,
                "sprites": [
                    {"index": 0, "address": 0x07000000, "attr0": 0, "attr1": 0, "attr2": 0}
                ],
            }
        elif kind == "dump_entities":
            data = {
                "base": payload["base"],
                "size": 2,
                "count": 1,
                "entities": [{"index": 0, "address": payload["base"], "bytes": [0, 255]}],
            }
        return {"id": f"request-{frame}", "ok": True, "frame": frame, "data": data}

    class Process:
        pid = 4321

        def poll(self):
            return None

    monkeypatch.setattr("mgba_live_mcp.session_manager.subprocess.Popen", lambda *a, **k: Process())
    monkeypatch.setattr(process_control, "capture_identity", lambda pid: {"pid": pid})
    monkeypatch.setattr(process_control, "process_state", lambda *a, **k: "alive")
    monkeypatch.setattr(process_control, "terminate_owned_process", lambda *a, **k: "stopped")
    monkeypatch.setattr(manager, "send_command", bridge)
    manager.start(rom=str(ROM), session_id="s1", mgba_path="mgba-qt")
    monkeypatch.setattr(server, "_controller", LiveControllerClient(manager))
    return manager


CASES = [
    ("start", {"rom": str(ROM), "session_id": "new", "mgba_path": "mgba-qt"}),
    (
        "start_with_lua",
        {"rom": str(ROM), "session_id": "new", "mgba_path": "mgba-qt", "code": "return true"},
    ),
    (
        "start_with_lua_and_view",
        {"rom": str(ROM), "session_id": "new", "mgba_path": "mgba-qt", "code": "return true"},
    ),
    ("attach", {"session": "s1"}),
    ("status", {"session": "s1"}),
    ("status", {"all": True}),
    ("get_view", {"session": "s1"}),
    ("stop", {"session": "s1"}),
    ("run_lua", {"session": "s1", "code": "return true"}),
    ("run_lua_and_view", {"session": "s1", "code": "return true"}),
    ("input_tap", {"session": "s1", "key": "A"}),
    ("input_tap_and_view", {"session": "s1", "key": "A", "wait_frames": 0}),
    ("input_set", {"session": "s1", "keys": []}),
    ("input_clear", {"session": "s1"}),
    ("input_clear", {"session": "s1", "keys": ["A"]}),
    ("export_screenshot", {"session": "s1"}),
    ("read_memory", {"session": "s1", "addresses": [1]}),
    ("read_memory", {"session": "s1", "addresses": []}),
    ("read_range", {"session": "s1", "start": 1, "length": 2}),
    ("dump_pointers", {"session": "s1", "start": 0, "count": 1}),
    ("dump_oam", {"session": "s1", "count": 1}),
    ("dump_entities", {"session": "s1", "base": 0, "size": 2, "count": 1}),
]


@pytest.mark.parametrize(("suffix", "arguments"), CASES)
def test_every_success_matches_catalog_and_compact_text(runtime, suffix, arguments):
    name = "mgba_live_" + suffix
    tools = {tool.name: tool for tool in asyncio.run(server.list_tools())}
    result = invoke(name, arguments)
    assert not result.isError, result.structuredContent
    Draft202012Validator(tools[name].outputSchema).validate(result.structuredContent)
    public = structured(result)
    assert public is not None
    assert "png_base64" not in public
    images = [block for block in result.content if isinstance(block, types.ImageContent)]
    visual = suffix.endswith("_and_view") or suffix in {"get_view", "export_screenshot"}
    if visual:
        assert len(images) == 1
        assert base64.b64decode(images[0].data) == PNG
        assert images[0].data not in first_text(result)
        if "frame" in public and "screenshot" in public:
            assert public["frame"] < public["screenshot"]["frame"]
    else:
        assert not images
        assert "screenshot" not in public


def test_catalog_coverage_and_effect_hints():
    tools = {tool.name: tool for tool in asyncio.run(server.list_tools())}
    assert set(tools) == {"mgba_live_" + suffix for suffix, _ in CASES}
    for tool in tools.values():
        assert tool.inputSchema["type"] == tool.outputSchema["type"] == "object"
        Draft202012Validator.check_schema(tool.outputSchema)
        assert tool.annotations is not None
    for name, tool in tools.items():
        hints = tool.annotations
        if "lua" in name:
            assert not hints.readOnlyHint and not hints.idempotentHint
            assert hints.destructiveHint and hints.openWorldHint
    assert not tools["mgba_live_status"].annotations.readOnlyHint
    assert tools["mgba_live_status"].annotations.destructiveHint
    assert tools["mgba_live_stop"].annotations.destructiveHint
    assert tools["mgba_live_stop"].annotations.idempotentHint
    assert tools["mgba_live_export_screenshot"].annotations.openWorldHint


@pytest.mark.parametrize("value", [False, 0, "", None, [], {}, [1, False, None]])
@pytest.mark.parametrize("wrapped", [False, True])
def test_lua_heterogeneity_survives_all_registered_composites(runtime, monkeypatch, value, wrapped):
    original = runtime.send_command

    def bridge(target, kind, payload=None, **kwargs):
        result = original(target, kind, payload, **kwargs)
        if kind.startswith("run_lua_"):
            result["data"] = {"result": value} if wrapped else value
        return result

    monkeypatch.setattr(runtime, "send_command", bridge)
    for suffix in ("run_lua", "run_lua_and_view", "start_with_lua", "start_with_lua_and_view"):
        startup = suffix.startswith("start")
        arguments = {"code": "return nil"}
        if startup:
            arguments.update(rom=str(ROM), session_id=suffix, mgba_path="mgba-qt")
        else:
            arguments["session"] = "s1"
        result = invoke("mgba_live_" + suffix, arguments)
        assert not result.isError, result.structuredContent
        public = structured(result)
        actual = public["lua"] if startup else public["data"]
        expected = value if startup or not wrapped else {"result": value}
        assert actual == expected and type(actual) is type(expected)


@pytest.mark.parametrize(
    ("suffix", "arguments", "code"),
    [
        ("unknown", {"session": "s1"}, "unknown_tool"),
        ("screenshot", {}, "unknown_tool"),
        ("status", {}, "session_required"),
        ("attach", {}, "session_required"),
        ("run_lua", {"code": "return true"}, "invalid_arguments"),
        ("input_tap", {"session": "s1", "key": "A", "frames": False}, "invalid_arguments"),
        ("input_set", {"session": "s1", "keys": [1]}, "invalid_arguments"),
        (
            "input_tap_and_view",
            {"session": "s1", "key": "A", "wait_frames": -1},
            "invalid_arguments",
        ),
        ("run_lua", {"session": "s1", "code": "a", "file": "b"}, "invalid_arguments"),
        ("get_view", {"session": "missing"}, "session_not_found"),
        ("run_lua", {"session": "s1", "file": "/missing/lua/file"}, "resource_not_found"),
    ],
)
def test_registered_failures_never_look_successful(runtime, suffix, arguments, code):
    result = invoke("mgba_live_" + suffix, arguments)
    assert result.isError
    error = structured(result)["error"]
    assert error["code"] == code and code in ERROR_CODES
    assert error["execution_outcome"] == "not_started"
    if "session" in arguments:
        assert error["session_id"] == arguments["session"]


def test_domain_bridge_error_keeps_request_and_execution_context(runtime, monkeypatch):
    monkeypatch.setattr(
        runtime,
        "send_command",
        lambda *a, **k: {"id": "failed-request", "ok": False, "error": "Lua failed after mutation"},
    )
    result = invoke("mgba_live_run_lua", {"session": "s1", "code": "error('failed')"})
    error = structured(result)["error"]
    assert result.isError and error["code"] == "bridge_error"
    assert error["request_id"] == "failed-request" and error["session_id"] == "s1"
    assert error["phase"] == "command" and error["execution_outcome"] == "unknown"


def test_dead_session_is_domain_failure(runtime, monkeypatch):
    monkeypatch.setattr(process_control, "process_state", lambda *a, **k: "dead")
    result = invoke("mgba_live_run_lua", {"session": "s1", "code": "return 1"})
    assert result.isError
    assert structured(result)["error"]["code"] == "session_dead"


def test_busy_session_retains_pending_request(runtime):
    with session_transactions.transaction(runtime.session_dir("s1")) as operation:
        operation.publish("pending-request", lambda: None)
    result = invoke("mgba_live_get_view", {"session": "s1"})
    error = structured(result)["error"]
    assert result.isError and error["code"] == "session_busy"
    assert error["pending_request_id"] == "pending-request"
    assert "request_id" not in error
    assert error["phase"] == "reconcile" and error["execution_outcome"] == "not_started"


def test_real_transport_timeout_remains_ambiguous(runtime, monkeypatch):
    monkeypatch.setattr(runtime, "send_command", SessionManager.send_command.__get__(runtime))
    result = invoke("mgba_live_run_lua", {"session": "s1", "code": "return 1", "timeout": 0.01})
    error = structured(result)["error"]
    assert result.isError and error["code"] == "command_timeout"
    assert error["execution_outcome"] == "unknown" and error["phase"] == "command"
    journal = session_transactions.transaction_status(runtime.session_dir("s1"))
    assert journal is not None
    assert error["request_id"] == journal["operation"]["pending_request"]


@pytest.mark.parametrize("encoded", [None, "", "***"])
def test_missing_visual_content_is_error_not_metadata_success(runtime, monkeypatch, encoded):
    monkeypatch.setattr(
        runtime, "get_view", lambda **k: {"session_id": "s1", "frame": 40, "png_base64": encoded}
    )
    result = invoke("mgba_live_run_lua_and_view", {"session": "s1", "code": "return false"})
    error = structured(result)["error"]
    assert result.isError and error["code"] == "snapshot_failed"
    assert error["phase"] == "snapshot" and error["execution_outcome"] == "partial"
    assert not any(isinstance(block, types.ImageContent) for block in result.content)


def test_composite_domain_failure_does_not_erase_cause(runtime, monkeypatch):
    def fail(**kwargs):
        raise DomainError("command_timeout", "timed out", phase="command", request_id="capture-id")

    monkeypatch.setattr(runtime, "get_view", fail)
    result = invoke("mgba_live_run_lua_and_view", {"session": "s1", "code": "return 0"})
    error = structured(result)["error"]
    assert result.isError and error["code"] == "snapshot_failed"
    assert error["cause_code"] == "command_timeout" and error["cause_phase"] == "command"
    assert error["request_id"] == "capture-id" and error["execution_outcome"] == "partial"


def test_real_stdio_initialize_and_metadata_requests(tmp_path):
    async def exchange():
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "mgba_live_mcp.server"],
            env={**os.environ, "HOME": str(tmp_path)},
        )
        async with stdio_client(params) as (reader, writer):
            async with ClientSession(reader, writer) as client:
                initialized = await client.initialize()
                assert initialized.serverInfo.version == version("mgba-live-mcp")
                assert initialized.capabilities.resources is None
                assert initialized.capabilities.prompts is None
                catalog = await client.list_tools()
                assert {tool.name for tool in catalog.tools} == {
                    "mgba_live_" + suffix for suffix, _ in CASES
                }
                listed = await client.call_tool("mgba_live_status", {"all": True})
                assert not listed.isError and listed.structuredContent == {"value": []}
                for name, args, code in (
                    ("missing_tool", {}, "unknown_tool"),
                    ("mgba_live_attach", {"session": "missing"}, "session_not_found"),
                    ("mgba_live_status", {"all": "yes"}, "invalid_arguments"),
                ):
                    result = await client.call_tool(name, args)
                    assert result.isError and structured(result)["error"]["code"] == code
                    assert "mcp_request_id" in structured(result)["error"]
                    assert json.loads(first_text(result)) == result.structuredContent

    asyncio.run(exchange())


@pytest.mark.parametrize(
    ("arguments", "code", "exit_code"),
    [
        (["run-lua", "--session", "missing", "--code", "return 1"], "session_not_found", 1),
        (
            ["read-range", "--session", "missing", "--start", "0", "--length", "bad"],
            "invalid_arguments",
            2,
        ),
    ],
)
def test_cli_uses_domain_error_envelope(tmp_path, arguments, code, exit_code):
    result = subprocess.run(
        [sys.executable, "-m", "mgba_live_mcp.live_cli", *arguments],
        capture_output=True,
        text=True,
        env={**os.environ, "HOME": str(tmp_path)},
        check=False,
        timeout=10,
    )
    assert result.returncode == exit_code and result.stdout == ""
    error = json.loads(result.stderr)["error"]
    assert error["code"] == code and error["execution_outcome"] == "not_started"
    if code == "session_not_found":
        assert error["session_id"] == "missing" and error["phase"] == "admission"


def test_invalid_success_payload_becomes_error(runtime, monkeypatch):
    monkeypatch.setattr(runtime, "read_memory", lambda **k: {"session_id": "s1", "frame": 3})
    result = invoke("mgba_live_read_memory", {"session": "s1", "addresses": []})
    assert result.isError
    assert structured(result)["error"]["code"] == "invalid_result"
