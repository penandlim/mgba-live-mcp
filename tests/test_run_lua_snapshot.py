from __future__ import annotations

import pytest

from mgba_live_mcp.live_controller import LiveControllerClient


class _MacroManager:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.polls = 0

    def run_lua(
        self,
        *,
        session: str,
        code: str | None = None,
        file: str | None = None,
        timeout: float = 20.0,
    ):
        self.calls.append(
            ("run_lua", {"session": session, "code": code, "file": file, "timeout": timeout})
        )
        if code == "return 11":
            return {
                "session_id": session,
                "frame": 100,
                "data": {"result": {"status": "started", "macro_key": "__macro_wait_test"}},
            }
        if code and "__macro_wait_test" in code:
            self.polls += 1
            return {
                "session_id": session,
                "frame": 100 + self.polls,
                "data": {"result": self.polls >= 3},
            }
        if code == "return true":
            return {"session_id": session, "frame": 104, "data": {"result": True}}
        raise AssertionError(f"unexpected run_lua code: {code!r}")

    def get_view(self, *, session: str, timeout: float = 20.0):
        self.calls.append(("get_view", {"session": session, "timeout": timeout}))
        return {"session_id": session, "frame": 200, "png_base64": "AA=="}


class _BrokenViewManager:
    def run_lua(
        self,
        *,
        session: str,
        code: str | None = None,
        file: str | None = None,
        timeout: float = 20.0,
    ):
        del file, timeout
        return {"session_id": session, "frame": 100, "data": {"result": {"ok": True}}}

    def get_view(self, *, session: str, timeout: float = 20.0):
        del session, timeout
        raise RuntimeError("disk exploded")


@pytest.mark.anyio
async def test_run_lua_and_view_waits_for_macro_completion() -> None:
    manager = _MacroManager()
    client = LiveControllerClient(manager=manager)

    result = await client.run_lua_and_view(session="session-123", code="return 11", timeout=5.0)

    assert result["session_id"] == "session-123"
    assert result["screenshot"] == {"frame": 200}
    assert result["png_base64"] == "AA=="
    assert [call[0] for call in manager.calls] == [
        "run_lua",
        "run_lua",
        "run_lua",
        "run_lua",
        "get_view",
    ]


@pytest.mark.anyio
async def test_run_lua_and_view_wraps_snapshot_failures() -> None:
    client = LiveControllerClient(manager=_BrokenViewManager())

    with pytest.raises(RuntimeError, match="snapshot_failed"):
        await client.run_lua_and_view(session="session-123", code="return 7", timeout=5.0)
