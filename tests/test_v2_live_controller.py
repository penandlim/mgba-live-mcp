from __future__ import annotations

import asyncio
import threading
import time
from typing import Any

import pytest

from mgba_live_mcp.live_controller import LiveControllerClient


class _BlockingManager:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def run_lua(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("run_lua", dict(kwargs)))
        self.started.set()
        self.release.wait(timeout=5)
        return {"session_id": kwargs["session"], "frame": 10, "data": {"result": True}}

    def get_view(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("get_view", dict(kwargs)))
        return {"session_id": kwargs["session"], "frame": 11, "png_base64": "AA=="}


class _FastManager:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def run_lua(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("run_lua", dict(kwargs)))
        time.sleep(0.05)
        return {"session_id": kwargs["session"], "frame": 10, "data": {"result": True}}

    def get_view(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("get_view", dict(kwargs)))
        time.sleep(0.05)
        return {"session_id": kwargs["session"], "frame": 11, "png_base64": "AA=="}


class _FailingManager:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def run_lua(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("run_lua", dict(kwargs)))
        raise RuntimeError("boom")

    def get_view(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("get_view", dict(kwargs)))
        return {"session_id": kwargs["session"], "frame": 11, "png_base64": "AA=="}


class _CancelableManager:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def run_lua(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("run_lua", dict(kwargs)))
        self.started.set()
        self.release.wait(timeout=5)
        return {"session_id": kwargs["session"], "frame": 10, "data": {"result": True}}

    def get_view(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("get_view", dict(kwargs)))
        return {"session_id": kwargs["session"], "frame": 11, "png_base64": "AA=="}


@pytest.mark.anyio
async def test_same_session_overlap_raises_session_busy() -> None:
    manager = _BlockingManager()
    client = LiveControllerClient(manager=manager)

    first = asyncio.create_task(client.run_lua(session="session-1", code="return true"))
    await asyncio.to_thread(manager.started.wait, 2.0)

    with pytest.raises(RuntimeError, match="session_busy"):
        await client.get_view(session="session-1")

    manager.release.set()
    result = await first
    assert result["session_id"] == "session-1"


@pytest.mark.anyio
async def test_different_sessions_can_run_in_parallel() -> None:
    manager = _FastManager()
    client = LiveControllerClient(manager=manager)

    result_a, result_b = await asyncio.gather(
        client.run_lua(session="session-a", code="return true"),
        client.get_view(session="session-b"),
    )

    assert result_a["session_id"] == "session-a"
    assert result_b["session_id"] == "session-b"


@pytest.mark.anyio
async def test_lock_is_released_after_request_failure() -> None:
    manager = _FailingManager()
    client = LiveControllerClient(manager=manager)

    with pytest.raises(RuntimeError, match="boom"):
        await client.run_lua(session="session-1", code="return true")

    result = await client.get_view(session="session-1")
    assert result["session_id"] == "session-1"


@pytest.mark.anyio
async def test_lock_is_released_after_request_cancellation() -> None:
    manager = _CancelableManager()
    client = LiveControllerClient(manager=manager)

    task = asyncio.create_task(client.run_lua(session="session-1", code="return true"))
    await asyncio.to_thread(manager.started.wait, 2.0)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    result = await client.get_view(session="session-1")
    manager.release.set()
    assert result["session_id"] == "session-1"
