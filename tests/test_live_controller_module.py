from __future__ import annotations

import asyncio
import base64
import threading
from pathlib import Path
from typing import Any

import pytest

from mgba_live_mcp.live_controller import LiveControllerClient
from mgba_live_mcp.session_manager import SessionManager


class _StartManager(SessionManager):
    def __init__(self, runtime_root: Path) -> None:
        super().__init__(runtime_root=runtime_root)
        self.started = threading.Event()
        self.release_start = threading.Event()
        self.value = 0

    def start(self, *, session_id: str | None = None, **kwargs: Any) -> dict[str, Any]:
        session = session_id or "session-123"
        with self.transaction(session, create=True):
            self.value = 1
        # This boundary is outside start's primitive transaction, but must remain
        # inside the startup composite's reservation until Lua/capture complete.
        self.started.set()
        if not self.release_start.wait(5):
            raise TimeoutError("Test startup was not released")
        return {"session_id": session, "pid": 4321}

    def run_lua(self, *, session: str, **kwargs: Any) -> dict[str, Any]:
        with self.transaction(session):
            self.value += 1
            return {"session_id": session, "frame": self.value, "data": {"result": self.value}}

    def get_view(self, *, session: str, **kwargs: Any) -> dict[str, Any]:
        with self.transaction(session):
            image = base64.b64encode(str(self.value).encode()).decode()
            return {"session_id": session, "frame": self.value, "png_base64": image}


@pytest.mark.anyio
@pytest.mark.parametrize("with_view", [False, True])
async def test_startup_composite_owns_reserved_session_after_start_returns(
    tmp_path: Path, with_view: bool
) -> None:
    manager = _StartManager(tmp_path)
    client = LiveControllerClient(manager=manager)
    contender = LiveControllerClient(manager=_StartManager(tmp_path))
    operation = client.start_with_lua_and_view if with_view else client.start_with_lua
    first = asyncio.create_task(
        operation(rom="/tmp/game.gba", code="return 1", timeout=7, session_id="session-1")
    )
    try:
        assert await asyncio.to_thread(manager.started.wait, 2)
        with pytest.raises(RuntimeError, match="session_busy"):
            await asyncio.wait_for(contender.get_view(session="session-1"), 2)
        manager.release_start.set()
        result = await asyncio.wait_for(first, 2)
        if with_view:
            assert base64.b64decode(result["png_base64"]) == b"3"
            assert result["screenshot"]["frame"] == 3
        else:
            assert manager.value == 2
            assert "png_base64" not in result
    finally:
        manager.release_start.set()
        await asyncio.gather(first, return_exceptions=True)
