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
    def __init__(self, *, include_status: bool = True) -> None:
        self.include_status = include_status
        self.calls: list[dict[str, Any]] = []

    async def run(self, command: str, args: list[str], *, timeout: float = 20.0) -> _Result:
        self.calls.append({"command": command, "args": list(args), "timeout": timeout})
        if command == "run-lua":
            if args[:2] == ["--code", "return true"]:
                return _Result({"frame": 101, "data": {"settled": True}})
            return _Result({"frame": 100, "data": {"ok": True}})
        if command == "status":
            if not self.include_status:
                raise AssertionError("status fallback should not have been used")
            return _Result({"session_id": "active-session"})
        if command == "screenshot":
            return _Result({"frame": 102, "path": "/tmp/screenshot.png"})
        raise AssertionError(f"unexpected command: {command}")


def _first_payload(contents: list[Any]) -> dict[str, Any]:
    assert contents
    first = contents[0]
    assert getattr(first, "type", None) == "text"
    return json.loads(first.text)


def test_run_lua_snapshot_uses_status_fallback_when_session_not_provided(monkeypatch: Any) -> None:
    fake = _FakeController(include_status=True)
    monkeypatch.setattr(mcp_server, "_controller", fake)

    contents = asyncio.run(
        mcp_server.call_tool(
            "mgba_live_run_lua",
            {
                "code": "return 7",
                "timeout": 7.0,
            },
        )
    )
    payload = _first_payload(contents)

    assert payload["screenshot"] == {"frame": 102, "path": "/tmp/screenshot.png"}
    assert [call["command"] for call in fake.calls] == ["run-lua", "status", "run-lua", "screenshot"]
    assert fake.calls[0]["args"] == ["--code", "return 7"]
    assert fake.calls[1]["args"] == []
    assert fake.calls[2]["args"] == ["--code", "return true", "--session", "active-session"]
    assert fake.calls[3]["args"] == ["--session", "active-session"]
    assert fake.calls[0]["timeout"] == 7.0
    assert fake.calls[1]["timeout"] == 20.0
    assert fake.calls[2]["timeout"] == 20.0
    assert fake.calls[3]["timeout"] == 20.0


def test_run_lua_snapshot_settles_before_screenshot_when_session_is_given(monkeypatch: Any) -> None:
    fake = _FakeController(include_status=False)
    monkeypatch.setattr(mcp_server, "_controller", fake)

    contents = asyncio.run(
        mcp_server.call_tool(
            "mgba_live_run_lua",
            {
                "code": "return 9",
                "session": "session-123",
                "timeout": 5.0,
            },
        )
    )
    payload = _first_payload(contents)

    assert payload["screenshot"] == {"frame": 102, "path": "/tmp/screenshot.png"}
    assert [call["command"] for call in fake.calls] == ["run-lua", "run-lua", "screenshot"]
    assert fake.calls[0]["args"] == ["--code", "return 9", "--session", "session-123"]
    assert fake.calls[1]["args"] == ["--code", "return true", "--session", "session-123"]
    assert fake.calls[2]["args"] == ["--session", "session-123"]


def test_run_lua_snapshot_waits_for_macro_completion_when_macro_key_returned(monkeypatch: Any) -> None:
    class _MacroController:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []
            self.polls = 0

        async def run(self, command: str, args: list[str], *, timeout: float = 20.0) -> _Result:
            self.calls.append({"command": command, "args": list(args), "timeout": timeout})
            if command == "run-lua":
                if args[:2] == ["--code", "return 11"]:
                    return _Result(
                        {
                            "frame": 100,
                            "data": {"result": {"status": "started", "macro_key": "__macro_wait_test"}},
                        }
                    )
                if args[:2] == ["--code", "return true"]:
                    raise AssertionError("no-op settle should not run when macro_key is returned")
                if args[:1] == ["--code"] and "__macro_wait_test" in args[1]:
                    self.polls += 1
                    return _Result({"frame": 100 + self.polls, "data": {"result": self.polls >= 3}})
            if command == "screenshot":
                return _Result({"frame": 104, "path": "/tmp/screenshot.png"})
            raise AssertionError(f"unexpected command: {command}")

    fake = _MacroController()
    monkeypatch.setattr(mcp_server, "_controller", fake)

    contents = asyncio.run(
        mcp_server.call_tool(
            "mgba_live_run_lua",
            {
                "code": "return 11",
                "session": "session-123",
                "timeout": 5.0,
            },
        )
    )
    payload = _first_payload(contents)

    assert payload["screenshot"] == {"frame": 104, "path": "/tmp/screenshot.png"}
    assert "screenshot_settle" not in payload
    assert [call["command"] for call in fake.calls] == ["run-lua", "run-lua", "run-lua", "run-lua", "screenshot"]
    assert fake.calls[0]["args"] == ["--code", "return 11", "--session", "session-123"]
    assert "__macro_wait_test" in fake.calls[1]["args"][1]
