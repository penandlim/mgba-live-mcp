from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any

import pytest

from mgba_live_mcp import server as mcp_server


@dataclass
class _Result:
    payload: dict[str, Any]


class _FakeController:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def run(self, command: str, args: list[str], *, timeout: float = 20.0) -> _Result:
        self.calls.append({"command": command, "args": list(args), "timeout": timeout})
        if command != "screenshot":
            raise AssertionError(f"unexpected command: {command}")
        return _Result({"frame": 99, "path": "/tmp/screenshot.png"})


def _first_payload(result: Any) -> dict[str, Any]:
    if isinstance(result, tuple):
        _, payload = result
        assert isinstance(payload, dict)
        return payload
    assert result
    first = result[0]
    assert getattr(first, "type", None) == "text"
    return json.loads(first.text)


def test_export_screenshot_tool_maps_to_screenshot_command(monkeypatch: Any) -> None:
    fake = _FakeController()
    monkeypatch.setattr(mcp_server, "_controller", fake)

    contents = asyncio.run(
        mcp_server.call_tool(
            "mgba_live_export_screenshot",
            {
                "session": "session-123",
                "timeout": 9.0,
            },
        )
    )
    payload = _first_payload(contents)

    assert payload == {
        "session_id": "session-123",
        "frame": 99,
        "path": "/tmp/screenshot.png",
    }
    assert fake.calls == [
        {
            "command": "screenshot",
            "args": ["--session", "session-123", "--timeout", "9"],
            "timeout": 9.0,
        }
    ]


def test_legacy_screenshot_tool_name_is_removed() -> None:
    contents = asyncio.run(mcp_server.call_tool("mgba_live_screenshot", {}))
    first = contents[0]
    assert getattr(first, "type", None) == "text"
    assert first.text == "Unknown tool: mgba_live_screenshot"


def test_list_tools_exposes_export_screenshot_name() -> None:
    tools = asyncio.run(mcp_server.list_tools())
    by_name = {tool.name: tool for tool in tools}
    names = set(by_name)
    assert "mgba_live_export_screenshot" in names
    assert "mgba_live_screenshot" not in names
    assert by_name["mgba_live_export_screenshot"].inputSchema["required"] == ["session"]
    screenshot_props = by_name["mgba_live_export_screenshot"].inputSchema["properties"]
    assert screenshot_props["session"]["description"] == "Session id."
    assert "text_format" not in screenshot_props
    assert "text_max_bytes" not in screenshot_props
    assert "png" not in screenshot_props


def test_status_list_tools_schema_allows_session_or_all_true() -> None:
    tools = asyncio.run(mcp_server.list_tools())
    by_name = {tool.name: tool for tool in tools}
    status_schema = by_name["mgba_live_status"].inputSchema

    assert "required" not in status_schema
    assert status_schema["anyOf"] == [
        {"required": ["session"]},
        {"required": ["all"], "properties": {"all": {"const": True}}},
    ]


def test_status_requires_session_unless_all(monkeypatch: Any) -> None:
    class _StatusController:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        async def run(self, command: str, args: list[str], *, timeout: float = 20.0) -> _Result:
            self.calls.append({"command": command, "args": list(args), "timeout": timeout})
            if command == "status":
                return _Result({"session_id": "active-session", "alive": True})
            if command == "screenshot":
                return _Result({"frame": 100, "png_base64": "AA=="})
            raise AssertionError(f"unexpected command: {command}")

    fake = _StatusController()
    monkeypatch.setattr(mcp_server, "_controller", fake)

    with pytest.raises(ValueError, match="session_required"):
        asyncio.run(mcp_server.call_tool("mgba_live_status", {}))
    assert fake.calls == []


def test_status_all_uses_session_from_payload_for_snapshot(monkeypatch: Any) -> None:
    class _StatusAllController:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        async def run(self, command: str, args: list[str], *, timeout: float = 20.0) -> _Result:
            self.calls.append({"command": command, "args": list(args), "timeout": timeout})
            if command == "status" and args == ["--all"]:
                return _Result(
                    {"value": [{"session_id": "session-a"}, {"session_id": "session-b"}]}
                )
            if command == "status":
                raise AssertionError(
                    "status fallback should not run for --all when payload has sessions"
                )
            if command == "screenshot":
                return _Result({"frame": 200, "png_base64": "AA=="})
            raise AssertionError(f"unexpected command: {command}")

    fake = _StatusAllController()
    monkeypatch.setattr(mcp_server, "_controller", fake)

    contents = asyncio.run(mcp_server.call_tool("mgba_live_status", {"all": True}))
    payload = _first_payload(contents)

    assert payload["value"] == [{"session_id": "session-a"}, {"session_id": "session-b"}]
    assert payload["screenshot"] == {"frame": 200}
    assert fake.calls == [
        {"command": "status", "args": ["--all"], "timeout": 20.0},
        {
            "command": "screenshot",
            "args": ["--session", "session-a", "--no-save", "--timeout", "20"],
            "timeout": 20.0,
        },
    ]
