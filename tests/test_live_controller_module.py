from __future__ import annotations

import asyncio
import base64
import sys
import threading
from io import BytesIO
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from mgba_live_mcp.live_controller import LiveControllerClient
from mgba_live_mcp.screenshots import ScreenshotResult
from mgba_live_mcp.session_manager import SessionManager

ROM = Path(__file__).parent / "fixtures" / "synthetic.gb"


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

    def get_view(self, *, session: str, **kwargs: Any) -> ScreenshotResult:
        with self.transaction(session):
            image = BytesIO()
            Image.new("L", (1, 1), self.value).save(image, format="PNG")
            png = image.getvalue()
            return ScreenshotResult(
                {
                    "session_id": session,
                    "frame": self.value,
                    "png_base64": base64.b64encode(png).decode(),
                },
                png,
            )


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
        operation(
            rom=str(ROM),
            mgba_path=sys.executable,
            code="return 1",
            timeout=7,
            session_id="session-1",
        )
    )
    try:
        assert await asyncio.to_thread(manager.started.wait, 2)
        with pytest.raises(RuntimeError, match="session_busy"):
            await asyncio.wait_for(contender.get_view(session="session-1"), 2)
        manager.release_start.set()
        result = await asyncio.wait_for(first, 2)
        if with_view:
            with Image.open(BytesIO(base64.b64decode(result["png_base64"]))) as image:
                assert image.getpixel((0, 0)) == 3
            assert result["screenshot"]["frame"] == 3
        else:
            assert manager.value == 2
            assert "png_base64" not in result
    finally:
        manager.release_start.set()
        await asyncio.gather(first, return_exceptions=True)


@pytest.mark.anyio
@pytest.mark.parametrize("with_view", [False, True])
@pytest.mark.parametrize("value", [False, 0, "", None, {}])
async def test_startup_composite_preserves_falsy_lua_results(
    tmp_path: Path, with_view: bool, value: Any
) -> None:
    class FalsyManager(_StartManager):
        def run_lua(self, *, session: str, **kwargs: Any) -> dict[str, Any]:
            with self.transaction(session):
                return {"session_id": session, "frame": 1, "data": {"result": value}}

    manager = FalsyManager(tmp_path)
    manager.release_start.set()
    client = LiveControllerClient(manager=manager)
    operation = client.start_with_lua_and_view if with_view else client.start_with_lua
    result = await operation(
        rom=str(ROM), mgba_path=sys.executable, code="return nil", session_id="falsy"
    )
    assert result["lua"] == value
    assert type(result["lua"]) is type(value)
