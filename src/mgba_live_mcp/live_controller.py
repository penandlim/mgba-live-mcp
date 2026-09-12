"""Async in-process controller for the shared live mGBA session manager."""

from __future__ import annotations

import asyncio
from typing import Any

from .session_manager import SessionManager


class LiveControllerClient:
    """Run each complete manager operation in a cancellation-independent worker."""

    def __init__(self, manager: Any | None = None) -> None:
        self.manager = manager or SessionManager()

    async def start(self, *, timeout: float = 20.0, **kwargs: Any) -> dict[str, Any]:
        kwargs.setdefault("ready_timeout", timeout)
        return await asyncio.to_thread(self.manager.start, **kwargs)

    async def attach(self, **kwargs: Any) -> dict[str, Any]:
        return await asyncio.to_thread(self.manager.attach, **kwargs)

    async def status(self, **kwargs: Any) -> dict[str, Any] | list[dict[str, Any]]:
        return await asyncio.to_thread(self.manager.status, **kwargs)

    async def stop(self, *, session: str, **kwargs: Any) -> dict[str, Any]:
        return await asyncio.to_thread(self.manager.stop, session=session, **kwargs)

    async def run_lua(self, *, session: str, **kwargs: Any) -> dict[str, Any]:
        return await asyncio.to_thread(self.manager.run_lua, session=session, **kwargs)

    async def input_tap(self, *, session: str, **kwargs: Any) -> dict[str, Any]:
        return await asyncio.to_thread(self.manager.input_tap, session=session, **kwargs)

    async def input_set(self, *, session: str, **kwargs: Any) -> dict[str, Any]:
        return await asyncio.to_thread(self.manager.input_set, session=session, **kwargs)

    async def input_clear(self, *, session: str, **kwargs: Any) -> dict[str, Any]:
        return await asyncio.to_thread(self.manager.input_clear, session=session, **kwargs)

    async def export_screenshot(self, *, session: str, **kwargs: Any) -> dict[str, Any]:
        return await asyncio.to_thread(self.manager.screenshot, session=session, **kwargs)

    async def get_view(self, *, session: str, timeout: float = 20.0) -> dict[str, Any]:
        return await asyncio.to_thread(self.manager.get_view, session=session, timeout=timeout)

    async def read_memory(self, *, session: str, **kwargs: Any) -> dict[str, Any]:
        return await asyncio.to_thread(self.manager.read_memory, session=session, **kwargs)

    async def read_range(self, *, session: str, **kwargs: Any) -> dict[str, Any]:
        return await asyncio.to_thread(self.manager.read_range, session=session, **kwargs)

    async def dump_pointers(self, *, session: str, **kwargs: Any) -> dict[str, Any]:
        return await asyncio.to_thread(self.manager.dump_pointers, session=session, **kwargs)

    async def dump_oam(self, *, session: str, **kwargs: Any) -> dict[str, Any]:
        return await asyncio.to_thread(self.manager.dump_oam, session=session, **kwargs)

    async def dump_entities(self, *, session: str, **kwargs: Any) -> dict[str, Any]:
        return await asyncio.to_thread(self.manager.dump_entities, session=session, **kwargs)

    async def run_lua_and_view(
        self,
        *,
        session: str,
        timeout: float = 20.0,
        **kwargs: Any,
    ) -> dict[str, Any]:
        return await asyncio.to_thread(
            self.manager.run_lua_and_view, session=session, timeout=timeout, **kwargs
        )

    async def input_tap_and_view(
        self,
        *,
        session: str,
        key: str,
        frames: int = 1,
        wait_frames: int = 0,
        timeout: float = 20.0,
    ) -> dict[str, Any]:
        return await asyncio.to_thread(
            self.manager.input_tap_and_view,
            session=session,
            key=key,
            frames=frames,
            wait_frames=wait_frames,
            timeout=timeout,
        )

    async def start_with_lua(self, *, timeout: float = 20.0, **kwargs: Any) -> dict[str, Any]:
        return await asyncio.to_thread(self.manager.start_with_lua, timeout=timeout, **kwargs)

    async def start_with_lua_and_view(
        self,
        *,
        timeout: float = 20.0,
        **kwargs: Any,
    ) -> dict[str, Any]:
        return await asyncio.to_thread(
            self.manager.start_with_lua_and_view, timeout=timeout, **kwargs
        )
