"""Async in-process controller for the shared live mGBA session manager."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .session_manager import SessionManager


@dataclass
class LiveCommandResult:
    """Compatibility container for controller results."""

    returncode: int
    payload: dict[str, Any]
    stderr: str


def _lua_quote(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("'", "\\'")
    return f"'{escaped}'"


def _extract_run_lua_result(payload: dict[str, Any]) -> Any:
    data = payload.get("data")
    if not isinstance(data, dict):
        return None
    if "result" in data:
        return data.get("result")
    return data


def _extract_run_lua_macro_key(payload: dict[str, Any]) -> str | None:
    result = _extract_run_lua_result(payload)
    if not isinstance(result, dict):
        return None
    macro_key = result.get("macro_key")
    if isinstance(macro_key, str) and macro_key:
        return macro_key
    return None


def _extract_response_frame(payload: dict[str, Any]) -> int | None:
    frame = payload.get("frame")
    if isinstance(frame, bool):
        return None
    if isinstance(frame, (int, float)):
        return int(frame)
    return None


def _extract_input_tap_duration(payload: dict[str, Any]) -> int | None:
    data = payload.get("data")
    if not isinstance(data, dict):
        return None
    duration = data.get("duration")
    if isinstance(duration, bool):
        return None
    if isinstance(duration, (int, float)):
        parsed = int(duration)
        if parsed >= 1:
            return parsed
    return None


class LiveControllerClient:
    """Expose async tool-friendly methods over the synchronous SessionManager."""

    def __init__(self, manager: Any | None = None) -> None:
        self.manager = manager or SessionManager()
        self._session_locks: dict[str, asyncio.Lock] = {}

    async def _invoke(self, func: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
        return await asyncio.to_thread(func, *args, **kwargs)

    def _lock_for(self, session: str) -> asyncio.Lock:
        lock = self._session_locks.get(session)
        if lock is None:
            lock = asyncio.Lock()
            self._session_locks[session] = lock
        return lock

    async def _with_session_lock(
        self,
        session: str,
        operation: Callable[[], Any],
    ) -> Any:
        lock = self._lock_for(session)
        if lock.locked():
            raise RuntimeError(f"session_busy: session '{session}' is already handling a request.")
        await lock.acquire()
        try:
            result = operation()
            if asyncio.iscoroutine(result):
                return await result
            return result
        finally:
            lock.release()

    async def start(self, *, timeout: float = 20.0, **kwargs: Any) -> dict[str, Any]:
        start_kwargs = dict(kwargs)
        start_kwargs.setdefault("ready_timeout", timeout)
        return await self._invoke(self.manager.start, **start_kwargs)

    async def attach(self, **kwargs: Any) -> dict[str, Any]:
        return await self._invoke(self.manager.attach, **kwargs)

    async def status(self, **kwargs: Any) -> dict[str, Any] | list[dict[str, Any]]:
        return await self._invoke(self.manager.status, **kwargs)

    async def stop(self, *, session: str, **kwargs: Any) -> dict[str, Any]:
        return await self._with_session_lock(
            session,
            lambda: self._invoke(self.manager.stop, session=session, **kwargs),
        )

    async def _run_lua_unlocked(self, *, session: str, **kwargs: Any) -> dict[str, Any]:
        return await self._invoke(self.manager.run_lua, session=session, **kwargs)

    async def run_lua(self, *, session: str, **kwargs: Any) -> dict[str, Any]:
        return await self._with_session_lock(
            session,
            lambda: self._run_lua_unlocked(session=session, **kwargs),
        )

    async def _input_tap_unlocked(self, *, session: str, **kwargs: Any) -> dict[str, Any]:
        return await self._invoke(self.manager.input_tap, session=session, **kwargs)

    async def input_tap(self, *, session: str, **kwargs: Any) -> dict[str, Any]:
        return await self._with_session_lock(
            session,
            lambda: self._input_tap_unlocked(session=session, **kwargs),
        )

    async def _input_set_unlocked(self, *, session: str, **kwargs: Any) -> dict[str, Any]:
        return await self._invoke(self.manager.input_set, session=session, **kwargs)

    async def input_set(self, *, session: str, **kwargs: Any) -> dict[str, Any]:
        return await self._with_session_lock(
            session,
            lambda: self._input_set_unlocked(session=session, **kwargs),
        )

    async def _input_clear_unlocked(self, *, session: str, **kwargs: Any) -> dict[str, Any]:
        return await self._invoke(self.manager.input_clear, session=session, **kwargs)

    async def input_clear(self, *, session: str, **kwargs: Any) -> dict[str, Any]:
        return await self._with_session_lock(
            session,
            lambda: self._input_clear_unlocked(session=session, **kwargs),
        )

    async def _export_screenshot_unlocked(self, *, session: str, **kwargs: Any) -> dict[str, Any]:
        return await self._invoke(self.manager.screenshot, session=session, **kwargs)

    async def export_screenshot(self, *, session: str, **kwargs: Any) -> dict[str, Any]:
        return await self._with_session_lock(
            session,
            lambda: self._export_screenshot_unlocked(session=session, **kwargs),
        )

    async def _get_view_unlocked(self, *, session: str, timeout: float = 20.0) -> dict[str, Any]:
        return await self._invoke(self.manager.get_view, session=session, timeout=timeout)

    async def get_view(self, *, session: str, timeout: float = 20.0) -> dict[str, Any]:
        return await self._with_session_lock(
            session,
            lambda: self._get_view_unlocked(session=session, timeout=timeout),
        )

    async def _read_memory_unlocked(self, *, session: str, **kwargs: Any) -> dict[str, Any]:
        return await self._invoke(self.manager.read_memory, session=session, **kwargs)

    async def read_memory(self, *, session: str, **kwargs: Any) -> dict[str, Any]:
        return await self._with_session_lock(
            session,
            lambda: self._read_memory_unlocked(session=session, **kwargs),
        )

    async def _read_range_unlocked(self, *, session: str, **kwargs: Any) -> dict[str, Any]:
        return await self._invoke(self.manager.read_range, session=session, **kwargs)

    async def read_range(self, *, session: str, **kwargs: Any) -> dict[str, Any]:
        return await self._with_session_lock(
            session,
            lambda: self._read_range_unlocked(session=session, **kwargs),
        )

    async def _dump_pointers_unlocked(self, *, session: str, **kwargs: Any) -> dict[str, Any]:
        return await self._invoke(self.manager.dump_pointers, session=session, **kwargs)

    async def dump_pointers(self, *, session: str, **kwargs: Any) -> dict[str, Any]:
        return await self._with_session_lock(
            session,
            lambda: self._dump_pointers_unlocked(session=session, **kwargs),
        )

    async def _dump_oam_unlocked(self, *, session: str, **kwargs: Any) -> dict[str, Any]:
        return await self._invoke(self.manager.dump_oam, session=session, **kwargs)

    async def dump_oam(self, *, session: str, **kwargs: Any) -> dict[str, Any]:
        return await self._with_session_lock(
            session,
            lambda: self._dump_oam_unlocked(session=session, **kwargs),
        )

    async def _dump_entities_unlocked(self, *, session: str, **kwargs: Any) -> dict[str, Any]:
        return await self._invoke(self.manager.dump_entities, session=session, **kwargs)

    async def dump_entities(self, *, session: str, **kwargs: Any) -> dict[str, Any]:
        return await self._with_session_lock(
            session,
            lambda: self._dump_entities_unlocked(session=session, **kwargs),
        )

    async def _wait_for_macro_completion(
        self,
        *,
        session: str,
        macro_key: str,
        timeout: float,
        poll_seconds: float = 0.05,
    ) -> dict[str, Any]:
        settle_timeout = max(float(timeout), 0.0)
        poll_interval = max(float(poll_seconds), 0.01)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + settle_timeout
        polls = 0
        wait_code = (
            f"local macro = _G[{_lua_quote(macro_key)}]; "
            "if macro == nil then return true end; "
            "local active = macro.active; "
            "if active == nil then return true end; "
            "return active == false"
        )
        while True:
            polls += 1
            result = await self._run_lua_unlocked(
                session=session,
                code=wait_code,
                timeout=min(max(settle_timeout, 1.0), 5.0),
            )
            result_value = _extract_run_lua_result(result)
            if result_value is True:
                return {"completed": True, "polls": polls, "frame": result.get("frame")}
            if loop.time() >= deadline:
                return {"completed": False, "polls": polls, "frame": result.get("frame")}
            await asyncio.sleep(poll_interval)

    async def _wait_for_target_frame(
        self,
        *,
        session: str,
        target_frame: int,
        timeout: float,
        poll_seconds: float = 0.05,
    ) -> dict[str, Any]:
        settle_timeout = max(float(timeout), 0.0)
        poll_interval = max(float(poll_seconds), 0.01)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + settle_timeout
        polls = 0
        while True:
            polls += 1
            result = await self._run_lua_unlocked(
                session=session,
                code="return true",
                timeout=min(max(settle_timeout, 1.0), 5.0),
            )
            frame = _extract_response_frame(result)
            if frame is None:
                raise RuntimeError("settle_failed: frame polling did not return a frame.")
            if frame >= target_frame:
                return {"completed": True, "polls": polls, "frame": frame}
            if loop.time() >= deadline:
                raise TimeoutError(
                    f"settle_failed: Timed out waiting for frame >= {target_frame} "
                    f"for session '{session}'."
                )
            await asyncio.sleep(poll_interval)

    async def _settle_after_lua_unlocked(
        self,
        *,
        session: str,
        command_payload: dict[str, Any],
        timeout: float,
    ) -> None:
        macro_key = _extract_run_lua_macro_key(command_payload)
        try:
            if macro_key:
                settled = await self._wait_for_macro_completion(
                    session=session,
                    macro_key=macro_key,
                    timeout=timeout,
                )
                if not settled.get("completed"):
                    raise TimeoutError(
                        f"settle_failed: Lua macro did not complete for session '{session}'."
                    )
                return
            await self._run_lua_unlocked(
                session=session,
                code="return true",
                timeout=min(max(float(timeout), 1.0), 5.0),
            )
        except Exception as exc:
            if isinstance(exc, RuntimeError) and str(exc).startswith("settle_failed:"):
                raise
            raise RuntimeError(
                f"settle_failed: Failed to settle session '{session}' after Lua execution. {exc}"
            ) from exc

    async def run_lua_and_view(
        self,
        *,
        session: str,
        timeout: float = 20.0,
        **kwargs: Any,
    ) -> dict[str, Any]:
        async def operation() -> dict[str, Any]:
            result = await self._run_lua_unlocked(session=session, timeout=timeout, **kwargs)
            await self._settle_after_lua_unlocked(
                session=session,
                command_payload=result,
                timeout=timeout,
            )
            try:
                view = await self._get_view_unlocked(session=session, timeout=timeout)
            except Exception as exc:
                raise RuntimeError(
                    f"snapshot_failed: Failed to capture screenshot for session '{session}'. {exc}"
                ) from exc
            return {
                **result,
                "screenshot": {"frame": view.get("frame")},
                "png_base64": view.get("png_base64"),
            }

        return await self._with_session_lock(session, operation)

    async def input_tap_and_view(
        self,
        *,
        session: str,
        key: str,
        frames: int = 1,
        wait_frames: int = 0,
        timeout: float = 20.0,
    ) -> dict[str, Any]:
        async def operation() -> dict[str, Any]:
            result = await self._input_tap_unlocked(
                session=session,
                key=key,
                frames=frames,
                timeout=timeout,
            )
            tap_frame = _extract_response_frame(result)
            duration = _extract_input_tap_duration(result)
            if tap_frame is None or duration is None:
                raise RuntimeError(
                    "settle_failed: input_tap did not return frame/duration for screenshot settle."
                )
            target_frame = tap_frame + duration + int(wait_frames)
            await self._wait_for_target_frame(
                session=session,
                target_frame=target_frame,
                timeout=timeout,
            )
            try:
                view = await self._get_view_unlocked(session=session, timeout=timeout)
            except Exception as exc:
                raise RuntimeError(
                    f"snapshot_failed: Failed to capture screenshot for session '{session}'. {exc}"
                ) from exc
            return {
                **result,
                "screenshot": {"frame": view.get("frame")},
                "png_base64": view.get("png_base64"),
            }

        return await self._with_session_lock(session, operation)

    async def start_with_lua(self, *, timeout: float = 20.0, **kwargs: Any) -> dict[str, Any]:
        start_kwargs = {key: value for key, value in kwargs.items() if key not in {"file", "code"}}
        start_result = await self.start(timeout=timeout, **start_kwargs)
        session = start_result.get("session_id")
        if not isinstance(session, str) or not session:
            raise RuntimeError("session_not_found: start did not return session_id.")

        lua_kwargs: dict[str, Any] = {"session": session, "timeout": timeout}
        if kwargs.get("file"):
            lua_kwargs["file"] = kwargs["file"]
        if kwargs.get("code"):
            lua_kwargs["code"] = kwargs["code"]
        try:
            lua_result = await self.run_lua(**lua_kwargs)
        except Exception as exc:
            raise RuntimeError(
                f"Lua execution failed after starting session '{session}'. "
                f"Session is still running. {exc}"
            ) from exc
        return {
            "session_id": session,
            "pid": start_result.get("pid"),
            "lua": _extract_run_lua_result(lua_result) or lua_result.get("data"),
        }

    async def start_with_lua_and_view(
        self,
        *,
        timeout: float = 20.0,
        **kwargs: Any,
    ) -> dict[str, Any]:
        start_kwargs = {key: value for key, value in kwargs.items() if key not in {"file", "code"}}
        start_result = await self.start(timeout=timeout, **start_kwargs)
        session = start_result.get("session_id")
        if not isinstance(session, str) or not session:
            raise RuntimeError("session_not_found: start did not return session_id.")

        async def operation() -> dict[str, Any]:
            lua_kwargs: dict[str, Any] = {"session": session, "timeout": timeout}
            if kwargs.get("file"):
                lua_kwargs["file"] = kwargs["file"]
            if kwargs.get("code"):
                lua_kwargs["code"] = kwargs["code"]
            try:
                lua_result = await self._run_lua_unlocked(**lua_kwargs)
                await self._settle_after_lua_unlocked(
                    session=session,
                    command_payload=lua_result,
                    timeout=timeout,
                )
                view = await self._get_view_unlocked(session=session, timeout=timeout)
            except Exception as exc:
                raise RuntimeError(
                    f"Failed after starting session '{session}'. Session is still running. {exc}"
                ) from exc
            return {
                "session_id": session,
                "pid": start_result.get("pid"),
                "lua": _extract_run_lua_result(lua_result) or lua_result.get("data"),
                "screenshot": {"frame": view.get("frame")},
                "png_base64": view.get("png_base64"),
            }

        return await self._with_session_lock(session, operation)
