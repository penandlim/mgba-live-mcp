from __future__ import annotations

import pytest

from mgba_live_mcp.live_controller import LiveControllerClient


class _StartManager:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def start(self, **kwargs):
        self.calls.append(("start", dict(kwargs)))
        return {"session_id": kwargs.get("session_id") or "session-123", "pid": 4321}

    def run_lua(self, **kwargs):
        self.calls.append(("run_lua", dict(kwargs)))
        return {"session_id": kwargs["session"], "frame": 100, "data": {"result": {"ok": True}}}

    def get_view(self, **kwargs):
        self.calls.append(("get_view", dict(kwargs)))
        return {"session_id": kwargs["session"], "frame": 200, "png_base64": "AA=="}


@pytest.mark.anyio
async def test_start_maps_timeout_to_ready_timeout() -> None:
    manager = _StartManager()
    client = LiveControllerClient(manager=manager)

    result = await client.start(rom="/tmp/game.gba", timeout=9.0, session_id="session-1")

    assert result == {"session_id": "session-1", "pid": 4321}
    assert manager.calls == [
        ("start", {"rom": "/tmp/game.gba", "session_id": "session-1", "ready_timeout": 9.0})
    ]


@pytest.mark.anyio
async def test_start_with_lua_and_view_combines_start_lua_and_snapshot() -> None:
    manager = _StartManager()
    client = LiveControllerClient(manager=manager)

    result = await client.start_with_lua_and_view(
        rom="/tmp/game.gba",
        code="return 1",
        timeout=7.0,
        session_id="session-1",
    )

    assert result["session_id"] == "session-1"
    assert result["pid"] == 4321
    assert result["lua"] == {"ok": True}
    assert result["screenshot"] == {"frame": 200}
    assert [call[0] for call in manager.calls] == ["start", "run_lua", "run_lua", "get_view"]
