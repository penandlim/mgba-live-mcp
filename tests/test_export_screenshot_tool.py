from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from mgba_live_mcp import server as mcp_server


def _first_payload(contents: Any) -> dict[str, Any]:
    assert contents
    first = contents[0]
    assert getattr(first, "type", None) == "text"
    return json.loads(first.text)


class _FakeController:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def export_screenshot(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("export_screenshot", dict(kwargs)))
        return {
            "session_id": kwargs["session"],
            "frame": 99,
            "path": kwargs.get("out") or "/tmp/screenshot.png",
            "png_base64": "AA==",
        }

    async def status(self, **kwargs: Any) -> dict[str, Any] | list[dict[str, Any]]:
        self.calls.append(("status", dict(kwargs)))
        if kwargs.get("all"):
            return [{"session_id": "session-a"}, {"session_id": "session-b"}]
        return {"session_id": kwargs["session"], "alive": True}


def test_export_screenshot_tool_returns_image_and_session_id(monkeypatch: Any) -> None:
    fake = _FakeController()
    monkeypatch.setattr(mcp_server, "_controller", fake)

    contents = asyncio.run(
        mcp_server.call_tool(
            "mgba_live_export_screenshot",
            {"session": "session-123", "timeout": 9.0, "out": "/tmp/out.png"},
        )
    )
    payload = _first_payload(contents)

    assert payload == {
        "session_id": "session-123",
        "frame": 99,
        "path": "/tmp/out.png",
    }
    assert len(contents) == 2
    assert fake.calls == [
        ("export_screenshot", {"session": "session-123", "out": "/tmp/out.png", "timeout": 9.0})
    ]


def test_legacy_screenshot_tool_name_is_removed() -> None:
    contents = asyncio.run(mcp_server.call_tool("mgba_live_screenshot", {}))
    first = contents[0]
    assert getattr(first, "type", None) == "text"
    assert first.text == "Unknown tool: mgba_live_screenshot"


def test_list_tools_exposes_export_screenshot_name() -> None:
    tools = asyncio.run(mcp_server.list_tools())
    by_name = {tool.name: tool for tool in tools}

    assert "mgba_live_export_screenshot" in by_name
    assert "mgba_live_screenshot" not in by_name
    screenshot_props = by_name["mgba_live_export_screenshot"].inputSchema["properties"]
    assert "out" in screenshot_props


def test_status_is_metadata_only_and_requires_session_unless_all(monkeypatch: Any) -> None:
    fake = _FakeController()
    monkeypatch.setattr(mcp_server, "_controller", fake)

    with pytest.raises(ValueError, match="session_required"):
        asyncio.run(mcp_server.call_tool("mgba_live_status", {}))

    one = asyncio.run(mcp_server.call_tool("mgba_live_status", {"session": "session-1"}))
    one_payload = _first_payload(one)
    assert one_payload == {"session_id": "session-1", "alive": True}
    assert len(one) == 1

    all_contents = asyncio.run(mcp_server.call_tool("mgba_live_status", {"all": True}))
    all_payload = _first_payload(all_contents)
    assert all_payload == {"value": [{"session_id": "session-a"}, {"session_id": "session-b"}]}
    assert len(all_contents) == 1
