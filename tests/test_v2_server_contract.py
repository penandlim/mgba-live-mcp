from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from mgba_live_mcp import server as mcp_server


def _first_payload(result: Any) -> dict[str, Any]:
    assert result
    first = result[0]
    assert getattr(first, "type", None) == "text"
    return json.loads(first.text)


class _FakeController:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def start(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("start", dict(kwargs)))
        return {
            "status": "started",
            "session_id": kwargs.get("session_id") or "session-123",
            "pid": 4321,
            "fps_target": 120.0,
            "session_dir": "/tmp/session-123",
        }

    async def start_with_lua(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("start_with_lua", dict(kwargs)))
        return {
            "session_id": kwargs.get("session_id") or "session-123",
            "pid": 4321,
            "lua": {"ok": True},
        }

    async def attach(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("attach", dict(kwargs)))
        return {
            "status": "attached",
            "session_id": kwargs["session"],
            "pid": 4321,
            "rom": "/tmp/game.gba",
            "fps_target": 120.0,
        }

    async def status(self, **kwargs: Any) -> dict[str, Any] | list[dict[str, Any]]:
        self.calls.append(("status", dict(kwargs)))
        if kwargs.get("all"):
            return [
                {
                    "session_id": "session-a",
                    "pid": 11,
                    "alive": True,
                    "rom": "/tmp/a.gba",
                    "fps_target": 120.0,
                    "mgba_path": "/opt/mgba",
                    "heartbeat": {"frame": 1},
                    "is_active": False,
                    "session_dir": "/tmp/a",
                },
                {
                    "session_id": "session-b",
                    "pid": 22,
                    "alive": True,
                    "rom": "/tmp/b.gba",
                    "fps_target": 120.0,
                    "mgba_path": "/opt/mgba",
                    "heartbeat": {"frame": 2},
                    "is_active": True,
                    "session_dir": "/tmp/b",
                },
            ]
        return {
            "session_id": kwargs["session"],
            "pid": 4321,
            "alive": True,
            "rom": "/tmp/game.gba",
            "fps_target": 120.0,
            "mgba_path": "/opt/mgba",
            "heartbeat": {"frame": 99},
            "is_active": True,
            "session_dir": "/tmp/session-123",
        }

    async def stop(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("stop", dict(kwargs)))
        return {
            "session_id": kwargs["session"],
            "pid": 4321,
            "alive_before": True,
            "alive_after": False,
            "stopped": True,
        }

    async def run_lua(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("run_lua", dict(kwargs)))
        return {
            "session_id": kwargs["session"],
            "frame": 100,
            "data": {"result": {"ok": True}},
        }

    async def input_tap(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("input_tap", dict(kwargs)))
        return {
            "session_id": kwargs["session"],
            "frame": 100,
            "data": {"key": 0, "duration": kwargs.get("frames", 1)},
        }

    async def input_set(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("input_set", dict(kwargs)))
        return {"session_id": kwargs["session"], "frame": 100, "data": {"keys": [0]}}

    async def input_clear(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("input_clear", dict(kwargs)))
        return {"session_id": kwargs["session"], "frame": 101, "data": {"cleared": "all"}}

    async def export_screenshot(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("export_screenshot", dict(kwargs)))
        return {
            "session_id": kwargs["session"],
            "frame": 200,
            "path": kwargs.get("out") or "/tmp/shot.png",
            "png_base64": "AA==",
        }

    async def get_view(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("get_view", dict(kwargs)))
        return {
            "session_id": kwargs["session"],
            "frame": 201,
            "png_base64": "AA==",
        }

    async def run_lua_and_view(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("run_lua_and_view", dict(kwargs)))
        return {
            "session_id": kwargs["session"],
            "frame": 100,
            "data": {"result": {"ok": True}},
            "screenshot": {"frame": 201},
            "png_base64": "AA==",
        }

    async def input_tap_and_view(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("input_tap_and_view", dict(kwargs)))
        return {
            "session_id": kwargs["session"],
            "frame": 101,
            "data": {"duration": kwargs.get("frames", 1), "key": 0},
            "screenshot": {"frame": 202},
            "png_base64": "AA==",
        }

    async def start_with_lua_and_view(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("start_with_lua_and_view", dict(kwargs)))
        session_id = kwargs.get("session_id") or "session-123"
        return {
            "session_id": session_id,
            "pid": 4321,
            "lua": {"ok": True},
            "screenshot": {"frame": 203},
            "png_base64": "AA==",
        }

    async def read_memory(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("read_memory", dict(kwargs)))
        return {"session_id": kwargs["session"], "frame": 100, "memory": {"0x00000001": 255}}

    async def read_range(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("read_range", dict(kwargs)))
        return {
            "session_id": kwargs["session"],
            "frame": 100,
            "range": {"start": kwargs["start"], "length": kwargs["length"], "data": [1, 2]},
        }

    async def dump_pointers(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("dump_pointers", dict(kwargs)))
        return {"session_id": kwargs["session"], "frame": 100, "pointers": {"count": 1}}

    async def dump_oam(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("dump_oam", dict(kwargs)))
        return {"session_id": kwargs["session"], "frame": 100, "oam": {"count": 1}}

    async def dump_entities(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("dump_entities", dict(kwargs)))
        return {"session_id": kwargs["session"], "frame": 100, "entities": {"count": 1}}


def test_list_tools_exposes_v2_visual_tools() -> None:
    tools = asyncio.run(mcp_server.list_tools())
    names = {tool.name for tool in tools}

    assert "mgba_live_get_view" in names
    assert "mgba_live_run_lua_and_view" in names
    assert "mgba_live_input_tap_and_view" in names
    assert "mgba_live_start_with_lua_and_view" in names
    # Strict function-calling clients reject top-level JSON Schema combinators.
    forbidden_top = ("oneOf", "anyOf", "allOf", "not", "enum")
    for tool in tools:
        schema = tool.inputSchema
        assert isinstance(schema, dict), f"{tool.name}: inputSchema must be an object schema"
        assert schema.get("type") == "object", f"{tool.name}: inputSchema type must be object"
        bad = [k for k in forbidden_top if k in schema]
        assert not bad, f"{tool.name}: inputSchema must not use top-level {bad}"


def test_status_requires_session_unless_all(monkeypatch: Any) -> None:
    monkeypatch.setattr(mcp_server, "_controller", _FakeController())

    with pytest.raises(ValueError, match="session_required"):
        asyncio.run(mcp_server.call_tool("mgba_live_status", {}))
    with pytest.raises(ValueError, match="all must be a boolean"):
        asyncio.run(mcp_server.call_tool("mgba_live_status", {"all": "yes"}))

    contents = asyncio.run(mcp_server.call_tool("mgba_live_status", {"all": True}))
    assert len(contents) == 1
    payload = _first_payload(contents)
    assert isinstance(payload["value"], list)
    assert "screenshot" not in payload


def test_single_session_tools_require_session(monkeypatch: Any) -> None:
    monkeypatch.setattr(mcp_server, "_controller", _FakeController())

    with pytest.raises(ValueError, match="session_required"):
        asyncio.run(mcp_server.call_tool("mgba_live_run_lua", {"code": "return true"}))
    with pytest.raises(ValueError, match="session_required"):
        asyncio.run(mcp_server.call_tool("mgba_live_input_tap", {"key": "A"}))
    with pytest.raises(ValueError, match="session_required"):
        asyncio.run(mcp_server.call_tool("mgba_live_export_screenshot", {}))
    with pytest.raises(ValueError, match="session_required"):
        asyncio.run(mcp_server.call_tool("mgba_live_read_memory", {"addresses": [1]}))


def test_server_rejects_invalid_optional_arguments(monkeypatch: Any) -> None:
    monkeypatch.setattr(mcp_server, "_controller", _FakeController())

    with pytest.raises(ValueError, match="pid must be an integer"):
        asyncio.run(mcp_server.call_tool("mgba_live_attach", {"pid": True}))
    with pytest.raises(ValueError, match="grace must be a number"):
        asyncio.run(mcp_server.call_tool("mgba_live_stop", {"session": "s1", "grace": False}))
    with pytest.raises(ValueError, match="keys is required"):
        asyncio.run(mcp_server.call_tool("mgba_live_input_set", {"session": "s1"}))


def test_metadata_tools_return_session_id_without_images(monkeypatch: Any) -> None:
    fake = _FakeController()
    monkeypatch.setattr(mcp_server, "_controller", fake)

    contents = asyncio.run(
        mcp_server.call_tool(
            "mgba_live_run_lua",
            {"session": "session-123", "code": "return true", "timeout": 9.0},
        )
    )
    assert len(contents) == 1
    payload = _first_payload(contents)
    assert payload["session_id"] == "session-123"
    assert payload["data"] == {"result": {"ok": True}}
    assert "screenshot" not in payload

    contents = asyncio.run(
        mcp_server.call_tool(
            "mgba_live_start_with_lua",
            {"rom": "/tmp/game.gba", "code": "return true", "session_id": "boot-1"},
        )
    )
    assert len(contents) == 1
    payload = _first_payload(contents)
    assert payload["session_id"] == "boot-1"
    assert "screenshot" not in payload


def test_visual_tools_return_image_and_session_id(monkeypatch: Any) -> None:
    fake = _FakeController()
    monkeypatch.setattr(mcp_server, "_controller", fake)

    run_contents = asyncio.run(
        mcp_server.call_tool(
            "mgba_live_run_lua_and_view",
            {"session": "session-123", "code": "return true"},
        )
    )
    run_payload = _first_payload(run_contents)
    assert run_payload["session_id"] == "session-123"
    assert run_payload["screenshot"] == {"frame": 201}
    assert len(run_contents) == 2

    tap_contents = asyncio.run(
        mcp_server.call_tool(
            "mgba_live_input_tap_and_view",
            {"session": "session-123", "key": "A", "wait_frames": 3},
        )
    )
    tap_payload = _first_payload(tap_contents)
    assert tap_payload["session_id"] == "session-123"
    assert tap_payload["screenshot"] == {"frame": 202}
    assert len(tap_contents) == 2

    view_contents = asyncio.run(
        mcp_server.call_tool("mgba_live_get_view", {"session": "session-123"})
    )
    view_payload = _first_payload(view_contents)
    assert view_payload["session_id"] == "session-123"
    assert view_payload["screenshot"] == {"frame": 201}
    assert len(view_contents) == 2


def test_export_screenshot_returns_session_id_path_and_image(monkeypatch: Any) -> None:
    fake = _FakeController()
    monkeypatch.setattr(mcp_server, "_controller", fake)

    contents = asyncio.run(
        mcp_server.call_tool(
            "mgba_live_export_screenshot",
            {"session": "session-123", "out": "/tmp/capture.png"},
        )
    )
    payload = _first_payload(contents)

    assert payload == {
        "session_id": "session-123",
        "frame": 200,
        "path": "/tmp/capture.png",
    }
    assert len(contents) == 2
