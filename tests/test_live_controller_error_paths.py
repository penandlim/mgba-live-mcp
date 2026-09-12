from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from mgba_live_mcp.live_controller import LiveControllerClient
from mgba_live_mcp.session_manager import SessionManager


class _BadTapManager(SessionManager):
    def input_tap(self, *, session: str, **kwargs: Any) -> dict[str, Any]:
        with self.transaction(session):
            return {"session_id": session, "data": {"duration": 1}}


class _BrokenLuaManager(SessionManager):
    def start(self, *, session_id: str | None = None, **kwargs: Any) -> dict[str, Any]:
        session = session_id or "session-123"
        with self.transaction(session, create=True):
            (self.session_dir(session) / "running").touch()
            return {"session_id": session, "pid": 4321}

    def run_lua(self, *, session: str, **kwargs: Any) -> dict[str, Any]:
        with self.transaction(session):
            raise RuntimeError("lua exploded")


@pytest.mark.anyio
async def test_input_tap_and_view_requires_frame_and_duration(tmp_path: Path) -> None:
    manager = _BadTapManager(runtime_root=tmp_path)
    manager.session_dir("session-123").mkdir(parents=True)
    client = LiveControllerClient(manager=manager)
    with pytest.raises(RuntimeError, match="settle_failed"):
        await client.input_tap_and_view(session="session-123", key="A", timeout=5)


@pytest.mark.anyio
@pytest.mark.parametrize("with_view", [False, True])
async def test_startup_lua_failure_keeps_session_context_and_running_session(
    tmp_path: Path, with_view: bool
) -> None:
    manager = _BrokenLuaManager(runtime_root=tmp_path)
    client = LiveControllerClient(manager=manager)
    operation = client.start_with_lua_and_view if with_view else client.start_with_lua
    with pytest.raises(RuntimeError, match="session-123") as error:
        await operation(rom="/tmp/game.gba", code="return 1", timeout=5, session_id="session-123")
    assert isinstance(error.value.__cause__, RuntimeError)
    assert "lua exploded" in str(error.value.__cause__)
    assert (manager.session_dir("session-123") / "running").exists()
