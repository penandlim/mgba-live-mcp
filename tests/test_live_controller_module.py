from __future__ import annotations

import asyncio
import sys
from typing import Any

from mgba_live_mcp.live_controller import LiveControllerClient


class _DummyProc:
    def __init__(self) -> None:
        self.returncode = 0

    async def communicate(self) -> tuple[bytes, bytes]:
        return b'{"status":"ok"}', b""

    def kill(self) -> None:
        return None


def test_live_controller_invokes_module_entrypoint(monkeypatch: Any) -> None:
    captured: dict[str, Any] = {}

    async def fake_create_subprocess_exec(*args: str, **kwargs: Any) -> _DummyProc:
        captured["args"] = list(args)
        captured["kwargs"] = kwargs
        return _DummyProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    client = LiveControllerClient()
    result = asyncio.run(client.run("status", ["--all"], timeout=1.0))

    assert captured["args"] == [
        sys.executable,
        "-m",
        "mgba_live_mcp.live_cli",
        "status",
        "--all",
    ]
    assert result.payload == {"status": "ok"}


def test_live_controller_supports_custom_module_name(monkeypatch: Any) -> None:
    captured: dict[str, Any] = {}

    async def fake_create_subprocess_exec(*args: str, **kwargs: Any) -> _DummyProc:
        captured["args"] = list(args)
        return _DummyProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    client = LiveControllerClient(module_name="custom.live_cli")
    asyncio.run(client.run("status", [], timeout=1.0))

    assert captured["args"] == [sys.executable, "-m", "custom.live_cli", "status"]
