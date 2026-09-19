"""Async in-process controller for the shared live mGBA session manager."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from .deadlines import operation_timeout, suspend_deadline
from .session_manager import SessionManager


class LiveControllerClient:
    """Run each complete manager operation in a cancellation-independent worker."""

    def __init__(self, manager: Any | None = None) -> None:
        self.manager = manager or SessionManager()

    async def _call(
        self,
        method: Callable[..., Any],
        *,
        timeout: float,
        _timeout_argument: str = "timeout",
        **kwargs: Any,
    ) -> Any:
        session = kwargs.get("session", kwargs.get("session_id"))
        with operation_timeout(timeout, session_id=session) as budget:

            def invoke() -> Any:
                # Queue time consumes the same budget; cancelled awaiters do not own
                # the worker's transaction and cannot release it while Lua is running.
                kwargs[_timeout_argument] = budget.remaining("acquisition")
                return method(**kwargs)

            result = await asyncio.to_thread(invoke)
            budget.execution_outcome = "completed"
            budget.check("result")
            return result

    async def start(self, *, timeout: float = 20.0, **kwargs: Any) -> dict[str, Any]:
        with operation_timeout(timeout, session_id=kwargs.get("session_id")):
            return await self._call(
                self.manager.start,
                timeout=kwargs.pop("ready_timeout", timeout),
                _timeout_argument="ready_timeout",
                **kwargs,
            )

    async def attach(self, *, timeout: float = 20.0, **kwargs: Any) -> dict[str, Any]:
        return await self._call(self.manager.attach, timeout=timeout, **kwargs)

    async def status(
        self, *, timeout: float = 20.0, **kwargs: Any
    ) -> dict[str, Any] | list[dict[str, Any]]:
        return await self._call(self.manager.status, timeout=timeout, **kwargs)

    async def stop(self, *, session: str, **kwargs: Any) -> dict[str, Any]:
        with suspend_deadline():
            return await asyncio.to_thread(self.manager.stop, session=session, **kwargs)

    async def run_lua(
        self, *, session: str, timeout: float = 20.0, **kwargs: Any
    ) -> dict[str, Any]:
        return await self._call(self.manager.run_lua, session=session, timeout=timeout, **kwargs)

    async def input_tap(
        self, *, session: str, timeout: float = 10.0, **kwargs: Any
    ) -> dict[str, Any]:
        return await self._call(self.manager.input_tap, session=session, timeout=timeout, **kwargs)

    async def input_set(
        self, *, session: str, timeout: float = 10.0, **kwargs: Any
    ) -> dict[str, Any]:
        return await self._call(self.manager.input_set, session=session, timeout=timeout, **kwargs)

    async def input_clear(
        self, *, session: str, timeout: float = 10.0, **kwargs: Any
    ) -> dict[str, Any]:
        return await self._call(
            self.manager.input_clear, session=session, timeout=timeout, **kwargs
        )

    async def export_screenshot(
        self, *, session: str, timeout: float = 20.0, **kwargs: Any
    ) -> dict[str, Any]:
        return await self._call(self.manager.screenshot, session=session, timeout=timeout, **kwargs)

    async def get_view(self, *, session: str, timeout: float = 20.0) -> dict[str, Any]:
        return await self._call(self.manager.get_view, session=session, timeout=timeout)

    async def read_memory(
        self, *, session: str, timeout: float = 10.0, **kwargs: Any
    ) -> dict[str, Any]:
        return await self._call(
            self.manager.read_memory, session=session, timeout=timeout, **kwargs
        )

    async def read_range(
        self, *, session: str, timeout: float = 10.0, **kwargs: Any
    ) -> dict[str, Any]:
        return await self._call(self.manager.read_range, session=session, timeout=timeout, **kwargs)

    async def dump_pointers(
        self, *, session: str, timeout: float = 10.0, **kwargs: Any
    ) -> dict[str, Any]:
        return await self._call(
            self.manager.dump_pointers, session=session, timeout=timeout, **kwargs
        )

    async def dump_oam(
        self, *, session: str, timeout: float = 10.0, **kwargs: Any
    ) -> dict[str, Any]:
        return await self._call(self.manager.dump_oam, session=session, timeout=timeout, **kwargs)

    async def dump_entities(
        self, *, session: str, timeout: float = 10.0, **kwargs: Any
    ) -> dict[str, Any]:
        return await self._call(
            self.manager.dump_entities, session=session, timeout=timeout, **kwargs
        )

    async def run_lua_and_view(
        self,
        *,
        session: str,
        timeout: float = 20.0,
        **kwargs: Any,
    ) -> dict[str, Any]:
        return await self._call(
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
        return await self._call(
            self.manager.input_tap_and_view,
            session=session,
            key=key,
            frames=frames,
            wait_frames=wait_frames,
            timeout=timeout,
        )

    async def start_with_lua(self, *, timeout: float = 20.0, **kwargs: Any) -> dict[str, Any]:
        return await self._call(self.manager.start_with_lua, timeout=timeout, **kwargs)

    async def start_with_lua_and_view(
        self,
        *,
        timeout: float = 20.0,
        **kwargs: Any,
    ) -> dict[str, Any]:
        return await self._call(self.manager.start_with_lua_and_view, timeout=timeout, **kwargs)
