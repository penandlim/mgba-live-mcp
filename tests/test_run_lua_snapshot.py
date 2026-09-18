from __future__ import annotations

import asyncio
import base64
import threading
from io import BytesIO
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from mgba_live_mcp.live_controller import LiveControllerClient
from mgba_live_mcp.screenshots import ScreenshotResult
from mgba_live_mcp.session_manager import SessionManager


class _MacroManager(SessionManager):
    def __init__(self, runtime_root: Path, *, pause_at: str | None = None) -> None:
        super().__init__(runtime_root=runtime_root)
        self.session_dir("session-123").mkdir(parents=True, exist_ok=True)
        self.frame = 100
        self.value = 0
        self.remaining: int | None = None
        self.captured_value: int | None = None
        self.pause_at = pause_at
        self.boundary = threading.Event()
        self.release = threading.Event()
        self.finished = threading.Event()

    def _pause(self, phase: str) -> None:
        if self.pause_at == phase and not self.boundary.is_set():
            self.boundary.set()
            if not self.release.wait(5):
                raise TimeoutError("Test composite was not released")

    def run_lua(self, *, session: str, **kwargs: Any) -> dict[str, Any]:
        with self.transaction(session):
            if self.remaining is None:
                self.remaining = 3
                value: Any = {"status": "started", "macro_key": "__macro_wait_test"}
                phase = "mutation"
            else:
                self.frame += 1
                self.remaining -= 1
                value = self.remaining <= 0
                if value:
                    self.value = 42
                phase = "settle"
            result = {"session_id": session, "frame": self.frame, "data": {"result": value}}
        # Probe outside each primitive's lease: only the real manager composite
        # can keep ownership across this return/next-command boundary.
        self._pause(phase)
        return result

    def get_view(self, *, session: str, **kwargs: Any) -> ScreenshotResult:
        with self.transaction(session):
            self.captured_value = self.value
            image = BytesIO()
            Image.new("L", (1, 1), self.value).save(image, format="PNG")
            png = image.getvalue()
            result = ScreenshotResult(
                {
                    "session_id": session,
                    "frame": self.frame,
                    "png_base64": base64.b64encode(png).decode(),
                },
                png,
            )
        self._pause("capture")
        return result

    def run_lua_and_view(self, **kwargs: Any) -> dict[str, Any]:
        try:
            return super().run_lua_and_view(**kwargs)
        finally:
            self.finished.set()


class _TapManager(_MacroManager):
    def input_tap(self, *, session: str, frames: int = 1, **kwargs: Any) -> dict[str, Any]:
        with self.transaction(session):
            self.release_frame = self.frame + frames
            result = {"session_id": session, "frame": self.frame, "data": {"duration": frames}}
        self._pause("mutation")
        return result

    def run_lua(self, *, session: str, **kwargs: Any) -> dict[str, Any]:
        with self.transaction(session):
            self.frame += 1
            if self.frame >= self.release_frame:
                self.value = 42
            result = {"session_id": session, "frame": self.frame, "data": {"result": True}}
        self._pause("settle")
        return result

    def input_tap_and_view(self, **kwargs: Any) -> dict[str, Any]:
        try:
            return super().input_tap_and_view(**kwargs)
        finally:
            self.finished.set()


class _BrokenViewManager(_MacroManager):
    def get_view(self, *, session: str, **kwargs: Any) -> ScreenshotResult:
        with self.transaction(session):
            raise RuntimeError("disk exploded")


def _captured_pixel(result: dict[str, Any]) -> Any:
    with Image.open(BytesIO(base64.b64decode(result["png_base64"]))) as image:
        return image.getpixel((0, 0))


@pytest.mark.anyio
async def test_run_lua_and_view_captures_completed_macro_state(tmp_path: Path) -> None:
    manager = _MacroManager(tmp_path)
    client = LiveControllerClient(manager=manager)
    result = await client.run_lua_and_view(session="session-123", code="return 11", timeout=5)
    assert _captured_pixel(result) == 42


@pytest.mark.anyio
async def test_input_tap_and_view_waits_for_release_and_extra_frames(tmp_path: Path) -> None:
    manager = _TapManager(tmp_path)
    client = LiveControllerClient(manager=manager)
    result = await client.input_tap_and_view(
        session="session-123", key="A", frames=3, wait_frames=2, timeout=5
    )
    assert _captured_pixel(result) == 42
    assert result["screenshot"]["frame"] >= manager.release_frame + 2


@pytest.mark.anyio
@pytest.mark.parametrize("phase", ["mutation", "settle", "capture"])
@pytest.mark.parametrize("operation", ["lua", "tap"])
async def test_cancelled_composite_keeps_ownership_through_every_phase(
    tmp_path: Path, phase: str, operation: str
) -> None:
    manager = (
        _MacroManager(tmp_path, pause_at=phase)
        if operation == "lua"
        else _TapManager(tmp_path, pause_at=phase)
    )
    client = LiveControllerClient(manager=manager)
    contender = LiveControllerClient(manager=_MacroManager(tmp_path))
    request = (
        client.run_lua_and_view(session="session-123", code="return 11", timeout=5)
        if operation == "lua"
        else client.input_tap_and_view(session="session-123", key="A", frames=3, timeout=5)
    )
    first = asyncio.create_task(request)
    try:
        assert await asyncio.to_thread(manager.boundary.wait, 2)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert not manager.finished.is_set()
        with pytest.raises(RuntimeError, match="session_busy"):
            await asyncio.wait_for(contender.get_view(session="session-123"), 2)
        manager.release.set()
        assert await asyncio.to_thread(manager.finished.wait, 2)
        view = await client.get_view(session="session-123")
        assert _captured_pixel(view) == 42
    finally:
        manager.release.set()
        await asyncio.gather(first, return_exceptions=True)
        assert await asyncio.to_thread(manager.finished.wait, 2)


@pytest.mark.anyio
async def test_run_lua_and_view_does_not_capture_unsettled_macro(tmp_path: Path) -> None:
    manager = _MacroManager(tmp_path)
    client = LiveControllerClient(manager=manager)
    with pytest.raises(RuntimeError, match="settle_failed"):
        await client.run_lua_and_view(session="session-123", code="return 11", timeout=0)
    assert manager.captured_value is None


@pytest.mark.anyio
async def test_run_lua_and_view_preserves_snapshot_failure_cause(tmp_path: Path) -> None:
    client = LiveControllerClient(manager=_BrokenViewManager(tmp_path))
    with pytest.raises(RuntimeError, match="snapshot_failed") as error:
        await client.run_lua_and_view(session="session-123", code="return 7", timeout=5)
    assert isinstance(error.value.__cause__, RuntimeError)
    assert str(error.value.__cause__) == "disk exploded"
