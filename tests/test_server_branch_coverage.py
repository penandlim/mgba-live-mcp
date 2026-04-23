from __future__ import annotations

import base64
from pathlib import Path
from typing import Any, cast

import pytest
from mcp.types import ImageContent, TextContent

import mgba_live_mcp.server as server
from mgba_live_mcp.live_controller import LiveCommandResult


def _result(payload: Any) -> LiveCommandResult:
    return LiveCommandResult(returncode=0, payload=payload, stderr="")


class _QueueController:
    def __init__(self, outcomes: list[Any]) -> None:
        self.outcomes = outcomes
        self.calls: list[tuple[str, list[str], float]] = []

    async def run(
        self, command: str, args: list[str], *, timeout: float = 20.0
    ) -> LiveCommandResult:
        self.calls.append((command, list(args), timeout))
        if not self.outcomes:
            raise AssertionError("No queued outcome")
        value = self.outcomes.pop(0)
        if isinstance(value, Exception):
            raise value
        return _result(value)


def test_text_payload_validation_errors() -> None:
    image = ImageContent(type="image", data="AA==", mimeType="image/png")
    with pytest.raises(RuntimeError, match="Expected text payload"):
        server._text_payload(image)

    bad_text: Any = type("BadText", (), {"type": "text", "text": None})()
    with pytest.raises(RuntimeError, match="missing JSON"):
        server._text_payload(cast(TextContent, bad_text))

    invalid_json = TextContent(type="text", text="{")
    with pytest.raises(RuntimeError, match="parse JSON"):
        server._text_payload(invalid_json)

    non_object = TextContent(type="text", text='["x"]')
    with pytest.raises(RuntimeError, match="must be an object"):
        server._text_payload(non_object)

    assert server._parse_args_list(None) == []


def test_image_and_snapshot_helper_edges(tmp_path: Path) -> None:
    assert server._image_bytes_from_screenshot({"png_base64": "***"}) is None
    assert server._image_bytes_from_screenshot({"path": str(tmp_path / "missing.png")}) is None
    assert server._public_snapshot_payload({"frame": True}) == {}
    assert server._public_snapshot_payload({"frame": "x"}) == {}


def test_extract_helper_edge_cases() -> None:
    assert server._extract_run_lua_result(cast(Any, "x")) is None
    assert server._extract_run_lua_result({"data": "x"}) is None
    assert server._extract_run_lua_macro_key({"data": {"result": 1}}) is None
    assert server._extract_session_id({"session_id": "s1"}) == "s1"
    assert server._extract_session_id({"value": [{"session_id": "s1"}]}) is None
    assert server._extract_session_id([{"session_id": "s1"}]) is None
    assert server._extract_response_frame("x") is None
    assert server._extract_response_frame({"frame": True}) is None
    assert server._extract_response_frame({"frame": "x"}) is None
    assert server._extract_input_tap_duration("x") is None
    assert server._extract_input_tap_duration({"data": "x"}) is None
    assert server._extract_input_tap_duration({"data": {"duration": True}}) is None
    assert server._extract_input_tap_duration({"data": {"duration": 0}}) is None


@pytest.mark.anyio
async def test_wait_helpers_timeout_and_error_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    queue = _QueueController([{"data": {"result": False}}])
    monkeypatch.setattr(server, "_controller", queue)
    out = await server._wait_for_macro_completion(
        session_id="s1",
        macro_key="m1",
        timeout=0.0,
        poll_seconds=0.0,
    )
    assert out["completed"] is False

    queue = _QueueController([{}])
    monkeypatch.setattr(server, "_controller", queue)
    with pytest.raises(RuntimeError, match="did not return a frame"):
        await server._wait_for_target_frame(
            session_id="s1", target_frame=2, timeout=1.0, poll_seconds=0.0
        )

    queue = _QueueController([{"frame": 1}])
    monkeypatch.setattr(server, "_controller", queue)
    with pytest.raises(TimeoutError, match="Timed out waiting for frame"):
        await server._wait_for_target_frame(
            session_id="s1", target_frame=2, timeout=0.0, poll_seconds=0.0
        )


@pytest.mark.anyio
async def test_run_with_snapshot_edge_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    queue = _QueueController([[1]])
    monkeypatch.setattr(server, "_controller", queue)
    contents = await server._run_with_snapshot("status", [], timeout=1.0, include_snapshot=False)
    assert server._text_payload(contents[0]) == {"value": [1]}

    queue = _QueueController([{"ok": True}])
    monkeypatch.setattr(server, "_controller", queue)

    async def no_session(*_args, **_kwargs):
        return None

    monkeypatch.setattr(server, "_resolve_snapshot_session", no_session)
    contents = await server._run_with_snapshot("status", [], timeout=1.0)
    assert len(contents) == 1

    queue = _QueueController([{"data": {}}])
    monkeypatch.setattr(server, "_controller", queue)
    with pytest.raises(RuntimeError, match="missing frame/duration"):
        await server._run_with_snapshot(
            "input-tap",
            ["--key", "A"],
            timeout=1.0,
            session_id="s1",
            input_tap_wait_frames=0,
        )

    async def wait_raises(**_kwargs):
        raise RuntimeError("boom")

    queue = _QueueController(
        [
            {"data": {"result": {"macro_key": "macro1"}}},
            {},
            {},
        ]
    )
    monkeypatch.setattr(server, "_controller", queue)
    monkeypatch.setattr(server, "_wait_for_macro_completion", wait_raises)
    contents = await server._run_with_snapshot(
        "run-lua",
        ["--code", "return 1"],
        timeout=1.0,
        session_id="s1",
        ensure_post_lua_settle=True,
    )
    assert len(contents) == 1

    queue = _QueueController(
        [
            {"data": {"result": {"value": 1}}},
            RuntimeError("no-op failed"),
            {},
            {},
        ]
    )
    monkeypatch.setattr(server, "_controller", queue)
    contents = await server._run_with_snapshot(
        "run-lua",
        ["--code", "return 1"],
        timeout=1.0,
        session_id="s1",
        ensure_post_lua_settle=True,
    )
    assert len(contents) == 1

    queue = _QueueController(
        [
            {"session_id": "s1"},
            RuntimeError("shot failed"),
        ]
    )
    monkeypatch.setattr(server, "_controller", queue)
    with pytest.raises(RuntimeError, match="Screenshot capture failed"):
        await server._run_with_snapshot(
            "status",
            [],
            timeout=1.0,
            session_id="s1",
            require_screenshot=True,
        )


@pytest.mark.anyio
async def test_resolve_snapshot_session_does_not_fallback_to_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _UnexpectedStatusController:
        async def run(self, command: str, args: list[str], *, timeout: float = 20.0) -> Any:
            raise AssertionError(f"unexpected fallback command: {command} {args} {timeout}")

    monkeypatch.setattr(server, "_controller", _UnexpectedStatusController())
    assert (
        await server._resolve_snapshot_session(
            [],
            {"value": [{"session_id": "session-a"}]},
            timeout=1.0,
        )
        is None
    )


@pytest.mark.anyio
async def test_run_with_snapshot_wraps_stall_errors_with_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queue = _QueueController(
        [
            TimeoutError("Timed out waiting for response to command 'run_lua'"),
            {
                "session_id": "other-session",
                "alive": True,
                "heartbeat": {"frame": 77, "unix_time": 123},
            },
        ]
    )
    monkeypatch.setattr(server, "_controller", queue)

    with pytest.raises(RuntimeError, match="The session appears stuck") as exc_info:
        await server._run_with_snapshot(
            "run-lua",
            ["--code", "return 1", "--session", "requested-session"],
            timeout=1.0,
            include_snapshot=False,
        )

    message = str(exc_info.value)
    assert "bad ROM build/patch" in message
    assert "session_id=requested-session" in message
    assert "status_session_mismatch=True" in message
    assert "timeout_reached=True" in message
    assert [call[0] for call in queue.calls] == ["run-lua", "status"]


def test_build_start_command_args_edges() -> None:
    with pytest.raises(ValueError, match="rom is required"):
        server._build_start_command_args({})

    args = {"rom": "x.gba", "fast": True}
    cmd = server._build_start_command_args(args)
    assert "--fast" in cmd


@pytest.mark.anyio
async def test_call_tool_start_with_lua_validation_and_missing_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ValueError, match="Exactly one of file or code"):
        await server.call_tool("mgba_live_start_with_lua", {"rom": "x.gba"})

    queue = _QueueController([{}])
    monkeypatch.setattr(server, "_controller", queue)
    with pytest.raises(RuntimeError, match="did not return session_id"):
        await server.call_tool("mgba_live_start_with_lua", {"rom": "x.gba", "file": "boot.lua"})


@pytest.mark.anyio
async def test_call_tool_attach_stop_run_lua_input_branches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, list[str], dict[str, Any]]] = []

    async def fake_snapshot(cmd: str, cmd_args: list[str], **kwargs):
        calls.append((cmd, list(cmd_args), kwargs))
        return [server._text_content({"ok": True})]

    monkeypatch.setattr(server, "_run_with_snapshot", fake_snapshot)

    await server.call_tool("mgba_live_attach", {"pid": 777})
    assert calls[-1][0] == "attach"
    assert "--pid" in calls[-1][1]

    await server.call_tool("mgba_live_stop", {"grace": 1.5})
    assert calls[-1][0] == "stop"
    assert "--grace" in calls[-1][1]

    await server.call_tool("mgba_live_run_lua", {"file": "x.lua"})
    assert calls[-1][0] == "run-lua"
    assert "--file" in calls[-1][1]

    with pytest.raises(ValueError, match="One of file or code is required"):
        await server.call_tool("mgba_live_run_lua", {})

    with pytest.raises(ValueError, match="key is required"):
        await server.call_tool("mgba_live_input_tap", {})

    await server.call_tool("mgba_live_input_tap", {"key": "A", "wait_frames": None})
    assert calls[-1][2]["input_tap_wait_frames"] == 0

    await server.call_tool("mgba_live_input_tap", {"key": "A", "wait_frames": 2.0})
    assert calls[-1][2]["input_tap_wait_frames"] == 2

    with pytest.raises(ValueError, match="non-negative integer"):
        await server.call_tool("mgba_live_input_tap", {"key": "A", "wait_frames": True})

    with pytest.raises(ValueError, match="non-negative integer"):
        await server.call_tool("mgba_live_input_tap", {"key": "A", "wait_frames": 1.2})

    await server.call_tool("mgba_live_input_clear", {"keys": ["A", "B"]})
    assert "--keys" in calls[-1][1]


@pytest.mark.anyio
async def test_call_tool_export_screenshot_branches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queue = _QueueController(["raw-value"])
    monkeypatch.setattr(server, "_controller", queue)
    contents = await server.call_tool("mgba_live_export_screenshot", {"out": "/tmp/a.png"})
    payload = server._text_payload(contents[0])
    assert payload == {"value": "raw-value"}

    png_base64 = base64.b64encode(b"png-bytes").decode()
    queue = _QueueController([{"png_base64": png_base64}])
    monkeypatch.setattr(server, "_controller", queue)
    contents = await server.call_tool("mgba_live_export_screenshot", {})
    assert len(contents) == 2
    assert getattr(contents[1], "type", None) == "image"


@pytest.mark.anyio
async def test_call_tool_data_command_arg_mapping(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, list[str], dict[str, Any]]] = []

    async def fake_snapshot(cmd: str, cmd_args: list[str], **kwargs):
        calls.append((cmd, list(cmd_args), kwargs))
        return [server._text_content({"ok": True})]

    monkeypatch.setattr(server, "_run_with_snapshot", fake_snapshot)

    await server.call_tool("mgba_live_read_memory", {"addresses": [1, 2]})
    await server.call_tool("mgba_live_read_range", {"start": 100, "length": 8})
    await server.call_tool("mgba_live_dump_pointers", {"start": 256, "count": 2, "width": 8})
    await server.call_tool("mgba_live_dump_oam", {"count": 12})
    await server.call_tool("mgba_live_dump_entities", {"base": 10, "size": 24, "count": 4})

    by_cmd = {cmd: args for (cmd, args, _kwargs) in calls}
    assert by_cmd["read-memory"][:1] == ["--addresses"]
    assert by_cmd["read-range"][:2] == ["--start", "100"]
    assert by_cmd["dump-pointers"][:2] == ["--start", "256"]
    assert "--width" in by_cmd["dump-pointers"]
    assert by_cmd["dump-oam"][:2] == ["--count", "12"]
    assert "--base" in by_cmd["dump-entities"]
    assert "--size" in by_cmd["dump-entities"]
    assert "--count" in by_cmd["dump-entities"]


def test_server_main_executes_stdio_run(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    class _DummyStdio:
        async def __aenter__(self):
            return "read", "write"

        async def __aexit__(self, exc_type, exc, tb):
            del exc_type, exc, tb
            return False

    async def fake_run(read_stream: Any, write_stream: Any, init_options: Any) -> None:
        captured["read"] = read_stream
        captured["write"] = write_stream
        captured["init"] = init_options

    monkeypatch.setattr(server, "stdio_server", lambda: _DummyStdio())
    monkeypatch.setattr(server.server, "run", fake_run)
    monkeypatch.setattr(server.server, "create_initialization_options", lambda: {"x": 1})

    server.main()

    assert captured["read"] == "read"
    assert captured["write"] == "write"
    assert captured["init"] == {"x": 1}


@pytest.mark.anyio
async def test_unknown_tool_response() -> None:
    result = await server.call_tool("unknown", {})
    assert len(result) == 1
    assert getattr(result[0], "type", None) == "text"
