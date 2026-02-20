from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any

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


def _first_payload(contents: list[Any]) -> dict[str, Any]:
    assert contents
    first = contents[0]
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

    assert payload == {"frame": 99, "path": "/tmp/screenshot.png"}
    assert fake.calls == [
        {
            "command": "screenshot",
            "args": ["--session", "session-123"],
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
    screenshot_props = by_name["mgba_live_export_screenshot"].inputSchema["properties"]
    assert "text_format" not in screenshot_props
    assert "text_max_bytes" not in screenshot_props
    assert "png" not in screenshot_props


def test_status_tool_returns_screenshot_without_text(monkeypatch: Any) -> None:
    class _StatusController:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        async def run(self, command: str, args: list[str], *, timeout: float = 20.0) -> _Result:
            self.calls.append({"command": command, "args": list(args), "timeout": timeout})
            if command == "status":
                return _Result({"session_id": "active-session", "alive": True})
            if command == "screenshot":
                return _Result({"frame": 100, "path": "/tmp/status.png"})
            raise AssertionError(f"unexpected command: {command}")

    fake = _StatusController()
    monkeypatch.setattr(mcp_server, "_controller", fake)

    contents = asyncio.run(mcp_server.call_tool("mgba_live_status", {}))
    payload = _first_payload(contents)

    assert payload["session_id"] == "active-session"
    assert payload["alive"] is True
    assert payload["screenshot"] == {"frame": 100, "path": "/tmp/status.png"}


def test_status_all_uses_session_from_payload_for_snapshot(monkeypatch: Any) -> None:
    class _StatusAllController:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        async def run(self, command: str, args: list[str], *, timeout: float = 20.0) -> _Result:
            self.calls.append({"command": command, "args": list(args), "timeout": timeout})
            if command == "status" and args == ["--all"]:
                return _Result({"value": [{"session_id": "session-a"}, {"session_id": "session-b"}]})
            if command == "status":
                raise AssertionError("status fallback should not run for --all when payload has sessions")
            if command == "screenshot":
                return _Result({"frame": 200, "path": "/tmp/status-all.png"})
            raise AssertionError(f"unexpected command: {command}")

    fake = _StatusAllController()
    monkeypatch.setattr(mcp_server, "_controller", fake)

    contents = asyncio.run(mcp_server.call_tool("mgba_live_status", {"all": True}))
    payload = _first_payload(contents)

    assert payload["value"] == [{"session_id": "session-a"}, {"session_id": "session-b"}]
    assert payload["screenshot"] == {"frame": 200, "path": "/tmp/status-all.png"}
    assert fake.calls == [
        {"command": "status", "args": ["--all"], "timeout": 20.0},
        {"command": "screenshot", "args": ["--session", "session-a"], "timeout": 20.0},
    ]
