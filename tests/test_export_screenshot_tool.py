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
        return _Result({"frame": 99, "text": {"format": "none"}})


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
                "text_format": "none",
                "timeout": 9.0,
            },
        )
    )
    payload = _first_payload(contents)

    assert payload["tool"] == "mgba_live_export_screenshot"
    assert payload["command"] == "screenshot"
    assert payload["result"] == {"frame": 99}
    assert fake.calls == [
        {
            "command": "screenshot",
            "args": ["--text-format", "none", "--session", "session-123"],
            "timeout": 9.0,
        }
    ]


def test_legacy_screenshot_tool_name_is_an_alias(monkeypatch: Any) -> None:
    fake = _FakeController()
    monkeypatch.setattr(mcp_server, "_controller", fake)

    contents = asyncio.run(
        mcp_server.call_tool(
            "mgba_live_screenshot",
            {
                "text_format": "none",
            },
        )
    )
    payload = _first_payload(contents)

    assert payload["tool"] == "mgba_live_export_screenshot"
    assert payload["command"] == "screenshot"


def test_list_tools_exposes_export_screenshot_name() -> None:
    tools = asyncio.run(mcp_server.list_tools())
    names = {tool.name for tool in tools}
    assert "mgba_live_export_screenshot" in names
    assert "mgba_live_screenshot" not in names


def test_status_tool_strips_screenshot_text_block(monkeypatch: Any) -> None:
    class _StatusController:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        async def run(self, command: str, args: list[str], *, timeout: float = 20.0) -> _Result:
            self.calls.append({"command": command, "args": list(args), "timeout": timeout})
            if command == "status":
                return _Result({"session_id": "active-session", "alive": True})
            if command == "screenshot":
                return _Result({"frame": 100, "text": {"format": "none"}})
            raise AssertionError(f"unexpected command: {command}")

    fake = _StatusController()
    monkeypatch.setattr(mcp_server, "_controller", fake)

    contents = asyncio.run(mcp_server.call_tool("mgba_live_status", {}))
    payload = _first_payload(contents)

    assert payload["tool"] == "mgba_live_status"
    assert payload["command"] == "status"
    assert payload["result"] == {"session_id": "active-session", "alive": True}
    assert payload["screenshot"] == {"frame": 100}
