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


class _Controller:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def input_tap(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("input_tap", dict(kwargs)))
        return {
            "session_id": kwargs["session"],
            "frame": 100,
            "data": {"key": 0, "duration": kwargs.get("frames", 1)},
        }

    async def input_tap_and_view(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("input_tap_and_view", dict(kwargs)))
        return {
            "session_id": kwargs["session"],
            "frame": 100,
            "data": {"key": 0, "duration": kwargs.get("frames", 1)},
            "screenshot": {"frame": 200},
            "png_base64": "AA==",
        }

    async def input_set(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("input_set", dict(kwargs)))
        return {"session_id": kwargs["session"], "frame": 101, "data": {"keys": [0, 1]}}

    async def input_clear(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("input_clear", dict(kwargs)))
        return {"session_id": kwargs["session"], "frame": 102, "data": {"cleared": "all"}}


def test_list_tools_input_tap_and_view_exposes_wait_frames() -> None:
    tools = asyncio.run(mcp_server.list_tools())
    by_name = {tool.name: tool for tool in tools}

    tap_props = by_name["mgba_live_input_tap"].inputSchema["properties"]
    assert "wait_frames" not in tap_props

    view_props = by_name["mgba_live_input_tap_and_view"].inputSchema["properties"]
    assert view_props["wait_frames"]["type"] == "integer"
    assert view_props["wait_frames"]["default"] == 0
    assert view_props["wait_frames"]["minimum"] == 0


def test_input_tap_metadata_only(monkeypatch: Any) -> None:
    fake = _Controller()
    monkeypatch.setattr(mcp_server, "_controller", fake)

    contents = asyncio.run(
        mcp_server.call_tool(
            "mgba_live_input_tap",
            {"session": "session-123", "key": "A", "frames": 3, "timeout": 7.0},
        )
    )
    payload = _first_payload(contents)

    assert payload == {
        "session_id": "session-123",
        "frame": 100,
        "data": {"key": 0, "duration": 3},
    }
    assert len(contents) == 1
    assert fake.calls == [
        ("input_tap", {"session": "session-123", "key": "A", "frames": 3, "timeout": 7.0})
    ]


def test_input_tap_and_view_returns_screenshot(monkeypatch: Any) -> None:
    fake = _Controller()
    monkeypatch.setattr(mcp_server, "_controller", fake)

    contents = asyncio.run(
        mcp_server.call_tool(
            "mgba_live_input_tap_and_view",
            {
                "session": "session-123",
                "key": "A",
                "frames": 3,
                "wait_frames": 2,
                "timeout": 7.0,
            },
        )
    )
    payload = _first_payload(contents)

    assert payload["session_id"] == "session-123"
    assert payload["data"]["duration"] == 3
    assert payload["screenshot"] == {"frame": 200}
    assert len(contents) == 2
    assert fake.calls == [
        (
            "input_tap_and_view",
            {
                "session": "session-123",
                "key": "A",
                "frames": 3,
                "wait_frames": 2,
                "timeout": 7.0,
            },
        )
    ]


def test_input_tap_and_view_rejects_negative_wait_frames() -> None:
    with pytest.raises(ValueError, match="wait_frames must be >= 0"):
        asyncio.run(
            mcp_server.call_tool(
                "mgba_live_input_tap_and_view",
                {"session": "session-123", "key": "A", "wait_frames": -1},
            )
        )


def test_input_set_and_clear_remain_no_snapshot(monkeypatch: Any) -> None:
    fake = _Controller()
    monkeypatch.setattr(mcp_server, "_controller", fake)

    set_contents = asyncio.run(
        mcp_server.call_tool(
            "mgba_live_input_set",
            {"session": "session-123", "keys": ["A", "B"], "timeout": 7.0},
        )
    )
    clear_contents = asyncio.run(
        mcp_server.call_tool(
            "mgba_live_input_clear",
            {"session": "session-123", "timeout": 7.0},
        )
    )

    assert "screenshot" not in _first_payload(set_contents)
    assert "screenshot" not in _first_payload(clear_contents)
    assert len(set_contents) == 1
    assert len(clear_contents) == 1
