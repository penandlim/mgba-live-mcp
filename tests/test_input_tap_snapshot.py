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


def _first_payload(result: Any) -> dict[str, Any]:
    if isinstance(result, tuple):
        _, payload = result
        assert isinstance(payload, dict)
        return payload
    assert result
    first = result[0]
    assert getattr(first, "type", None) == "text"
    return json.loads(first.text)


def test_list_tools_input_tap_exposes_wait_frames() -> None:
    tools = asyncio.run(mcp_server.list_tools())
    by_name = {tool.name: tool for tool in tools}
    tap_schema = by_name["mgba_live_input_tap"].inputSchema
    tap_props = tap_schema["properties"]

    assert "wait_frames" in tap_props
    assert tap_props["wait_frames"]["type"] == "integer"
    assert tap_props["wait_frames"]["default"] == 0
    assert tap_props["wait_frames"]["minimum"] == 0
    assert tap_schema["required"] == ["session", "key"]


class _InputTapController:
    def __init__(
        self,
        *,
        poll_frames: list[int] | None = None,
        fail_poll: bool = False,
        status_ok: bool = True,
    ) -> None:
        self.poll_frames = list(poll_frames or [])
        self.fail_poll = fail_poll
        self.status_ok = status_ok
        self.calls: list[dict[str, Any]] = []

    async def run(self, command: str, args: list[str], *, timeout: float = 20.0) -> _Result:
        self.calls.append({"command": command, "args": list(args), "timeout": timeout})
        if command == "input-tap":
            return _Result({"frame": 100, "data": {"key": 0, "duration": 3}})
        if command == "run-lua":
            if self.fail_poll:
                raise RuntimeError("frame polling failed")
            frame = self.poll_frames.pop(0) if self.poll_frames else 110
            return _Result({"frame": frame, "data": {"result": True}})
        if command == "status":
            if not self.status_ok:
                raise RuntimeError("status unavailable")
            return _Result({"session_id": "active-session"})
        if command == "screenshot":
            return _Result({"frame": 200, "png_base64": "AA=="})
        raise AssertionError(f"unexpected command: {command}")


def test_input_tap_waits_after_release_then_returns_screenshot(monkeypatch: Any) -> None:
    fake = _InputTapController(poll_frames=[101, 104, 105])
    monkeypatch.setattr(mcp_server, "_controller", fake)

    contents = asyncio.run(
        mcp_server.call_tool(
            "mgba_live_input_tap",
            {
                "key": "A",
                "frames": 3,
                "wait_frames": 2,
                "session": "session-123",
                "timeout": 7.0,
            },
        )
    )
    payload = _first_payload(contents)

    assert payload["frame"] == 100
    assert payload["data"]["duration"] == 3
    assert payload["screenshot"] == {"frame": 200}
    assert [call["command"] for call in fake.calls] == [
        "input-tap",
        "run-lua",
        "run-lua",
        "run-lua",
        "screenshot",
    ]
    assert fake.calls[0]["args"] == [
        "--key",
        "A",
        "--frames",
        "3",
        "--session",
        "session-123",
        "--timeout",
        "7",
    ]
    assert fake.calls[1]["args"] == [
        "--code",
        "return true",
        "--session",
        "session-123",
        "--timeout",
        "5",
    ]
    assert fake.calls[2]["args"] == [
        "--code",
        "return true",
        "--session",
        "session-123",
        "--timeout",
        "5",
    ]
    assert fake.calls[3]["args"] == [
        "--code",
        "return true",
        "--session",
        "session-123",
        "--timeout",
        "5",
    ]
    assert fake.calls[4]["args"] == [
        "--session",
        "session-123",
        "--no-save",
        "--timeout",
        "20",
    ]


def test_input_tap_wait_frames_zero_still_waits_through_tap_duration(monkeypatch: Any) -> None:
    fake = _InputTapController(poll_frames=[102, 103])
    monkeypatch.setattr(mcp_server, "_controller", fake)

    contents = asyncio.run(
        mcp_server.call_tool(
            "mgba_live_input_tap",
            {
                "key": "B",
                "frames": 3,
                "wait_frames": 0,
                "session": "session-123",
                "timeout": 7.0,
            },
        )
    )
    payload = _first_payload(contents)

    assert payload["screenshot"] == {"frame": 200}
    assert [call["command"] for call in fake.calls] == [
        "input-tap",
        "run-lua",
        "run-lua",
        "screenshot",
    ]


def test_input_tap_rejects_negative_wait_frames() -> None:
    with pytest.raises(ValueError, match="wait_frames must be >= 0"):
        asyncio.run(
            mcp_server.call_tool(
                "mgba_live_input_tap",
                {
                    "key": "A",
                    "wait_frames": -1,
                },
            )
        )


def test_input_tap_wait_failure_includes_session_id(monkeypatch: Any) -> None:
    fake = _InputTapController(fail_poll=True)
    monkeypatch.setattr(mcp_server, "_controller", fake)

    with pytest.raises(RuntimeError, match="session-123"):
        asyncio.run(
            mcp_server.call_tool(
                "mgba_live_input_tap",
                {
                    "key": "A",
                    "session": "session-123",
                    "timeout": 7.0,
                },
            )
        )
    assert [call["command"] for call in fake.calls] == ["input-tap", "run-lua", "status"]


def test_input_tap_errors_when_session_cannot_be_resolved(monkeypatch: Any) -> None:
    fake = _InputTapController(status_ok=False)
    monkeypatch.setattr(mcp_server, "_controller", fake)

    with pytest.raises(ValueError, match="session_required"):
        asyncio.run(
            mcp_server.call_tool(
                "mgba_live_input_tap",
                {
                    "key": "A",
                    "timeout": 7.0,
                },
            )
        )
    assert fake.calls == []


def test_input_set_and_clear_remain_no_snapshot(monkeypatch: Any) -> None:
    class _NoSnapshotController:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        async def run(self, command: str, args: list[str], *, timeout: float = 20.0) -> _Result:
            self.calls.append({"command": command, "args": list(args), "timeout": timeout})
            if command == "input-set":
                return _Result({"frame": 50, "data": {"keys": [0, 1]}})
            if command == "input-clear":
                return _Result({"frame": 51, "data": {"cleared": "all"}})
            raise AssertionError(f"unexpected command: {command}")

    fake = _NoSnapshotController()
    monkeypatch.setattr(mcp_server, "_controller", fake)

    set_contents = asyncio.run(
        mcp_server.call_tool(
            "mgba_live_input_set",
            {"keys": ["A", "B"], "session": "session-123", "timeout": 7.0},
        )
    )
    clear_contents = asyncio.run(
        mcp_server.call_tool(
            "mgba_live_input_clear",
            {"session": "session-123", "timeout": 7.0},
        )
    )

    set_payload = _first_payload(set_contents)
    clear_payload = _first_payload(clear_contents)
    assert "screenshot" not in set_payload
    assert "screenshot" not in clear_payload
    assert [call["command"] for call in fake.calls] == ["input-set", "input-clear"]
