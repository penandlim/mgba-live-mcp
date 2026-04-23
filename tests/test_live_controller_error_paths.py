from __future__ import annotations

import pytest

from mgba_live_mcp.live_controller import LiveControllerClient


class _BadTapManager:
    def input_tap(self, *, session: str, key: str, frames: int = 1, timeout: float = 10.0):
        del key, frames, timeout
        return {"session_id": session, "data": {"duration": 1}}


class _BrokenLuaManager:
    def start(self, **kwargs):
        return {"session_id": kwargs.get("session_id") or "session-123", "pid": 4321}

    def run_lua(self, **kwargs):
        raise RuntimeError("lua exploded")


@pytest.mark.anyio
async def test_input_tap_and_view_requires_frame_and_duration() -> None:
    client = LiveControllerClient(manager=_BadTapManager())

    with pytest.raises(RuntimeError, match="settle_failed"):
        await client.input_tap_and_view(session="session-123", key="A", timeout=5.0)


@pytest.mark.anyio
async def test_start_with_lua_keeps_session_context_in_failure() -> None:
    client = LiveControllerClient(manager=_BrokenLuaManager())

    with pytest.raises(RuntimeError, match="session-123"):
        await client.start_with_lua(rom="/tmp/game.gba", code="return 1", timeout=5.0)
