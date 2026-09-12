from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from typing import Any

import pytest

from mgba_live_mcp.live_controller import LiveControllerClient
from mgba_live_mcp.session_manager import SessionManager
from mgba_live_mcp.session_transactions import recovery


class _BlockingManager(SessionManager):
    def __init__(self, runtime_root: Path) -> None:
        super().__init__(runtime_root=runtime_root)
        for session in ("session-1", "session-2"):
            self.session_dir(session).mkdir(parents=True, exist_ok=True)
        self.started = threading.Event()
        self.release = threading.Event()
        self.finished = threading.Event()

    def run_lua(self, *, session: str, **kwargs: Any) -> dict[str, Any]:
        try:
            with self.transaction(session) as lease:
                self.started.set()
                if not self.release.wait(5):
                    raise TimeoutError("Test worker was not released")
                lease.check()
                return {"session_id": session, "frame": 10, "data": {"result": True}}
        finally:
            self.finished.set()

    def get_view(self, *, session: str, **kwargs: Any) -> dict[str, Any]:
        with self.transaction(session):
            return {"session_id": session, "frame": 11, "png_base64": "AA=="}

    def stop(self, *, session: str, **kwargs: Any) -> dict[str, Any]:
        # The simulated bridge has no process; exercise the real recovery fence.
        with recovery(self.session_dir(session)) as stopping:
            stopping.finish()
        return {"session_id": session, "stopped": True}


class _FailingManager(_BlockingManager):
    def run_lua(self, *, session: str, **kwargs: Any) -> dict[str, Any]:
        with self.transaction(session):
            raise RuntimeError("boom")


@pytest.mark.anyio
@pytest.mark.parametrize("separate_manager", [False, True])
async def test_same_session_overlap_raises_session_busy(
    tmp_path: Path, separate_manager: bool
) -> None:
    manager = _BlockingManager(tmp_path)
    client = LiveControllerClient(manager=manager)
    contender = (
        LiveControllerClient(manager=_BlockingManager(tmp_path)) if separate_manager else client
    )
    first = asyncio.create_task(client.run_lua(session="session-1", code="return true"))
    try:
        assert await asyncio.to_thread(manager.started.wait, 2)
        with pytest.raises(RuntimeError, match="session_busy"):
            await asyncio.wait_for(contender.get_view(session="session-1"), 2)
        manager.release.set()
        await asyncio.wait_for(first, 2)
    finally:
        manager.release.set()
        await asyncio.gather(first, return_exceptions=True)
        assert await asyncio.to_thread(manager.finished.wait, 2)


@pytest.mark.anyio
async def test_different_sessions_overlap_while_one_worker_is_blocked(tmp_path: Path) -> None:
    manager = _BlockingManager(tmp_path)
    client = LiveControllerClient(manager=manager)
    first = asyncio.create_task(client.run_lua(session="session-1", code="return true"))
    try:
        assert await asyncio.to_thread(manager.started.wait, 2)
        view = await asyncio.wait_for(client.get_view(session="session-2"), 2)
        assert view["session_id"] == "session-2"
        assert not manager.finished.is_set()
    finally:
        manager.release.set()
        await asyncio.gather(first, return_exceptions=True)
        assert await asyncio.to_thread(manager.finished.wait, 2)


@pytest.mark.anyio
async def test_failure_before_publication_releases_ownership(tmp_path: Path) -> None:
    client = LiveControllerClient(manager=_FailingManager(tmp_path))
    with pytest.raises(RuntimeError, match="boom"):
        await client.run_lua(session="session-1", code="return true")
    view = await client.get_view(session="session-1")
    assert view["session_id"] == "session-1"


@pytest.mark.anyio
async def test_cancellation_keeps_ownership_until_worker_finishes(tmp_path: Path) -> None:
    manager = _BlockingManager(tmp_path)
    client = LiveControllerClient(manager=manager)
    other_client = LiveControllerClient(manager=_BlockingManager(tmp_path))
    first = asyncio.create_task(client.run_lua(session="session-1", code="return true"))
    try:
        assert await asyncio.to_thread(manager.started.wait, 2)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert not manager.finished.is_set()
        for contender in (client, other_client):
            with pytest.raises(RuntimeError, match="session_busy"):
                await asyncio.wait_for(contender.get_view(session="session-1"), 2)
        manager.release.set()
        assert await asyncio.to_thread(manager.finished.wait, 2)
        view = await other_client.get_view(session="session-1")
        assert view["session_id"] == "session-1"
    finally:
        manager.release.set()
        await asyncio.gather(first, return_exceptions=True)
        assert await asyncio.to_thread(manager.finished.wait, 2)


@pytest.mark.anyio
async def test_recovery_stop_bypasses_held_ownership_and_fences_worker(tmp_path: Path) -> None:
    manager = _BlockingManager(tmp_path)
    client = LiveControllerClient(manager=manager)
    first = asyncio.create_task(client.run_lua(session="session-1", code="return true"))
    try:
        assert await asyncio.to_thread(manager.started.wait, 2)
        stopped = await asyncio.wait_for(client.stop(session="session-1"), 2)
        assert stopped["stopped"] is True
        assert not manager.finished.is_set()
        with pytest.raises(RuntimeError, match="session_(?:busy|stopp)"):
            await client.get_view(session="session-1")
        manager.release.set()
        with pytest.raises(RuntimeError, match="session_stopp"):
            await asyncio.wait_for(first, 2)
    finally:
        manager.release.set()
        await asyncio.gather(first, return_exceptions=True)
        assert await asyncio.to_thread(manager.finished.wait, 2)
