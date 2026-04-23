from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import pytest

import mgba_live_mcp.live_cli as live_cli


class _Manager:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def require_session(self, session: str | None, *, require_alive: bool = True) -> dict[str, Any]:
        self.calls.append(("require_session", {"session": session, "require_alive": require_alive}))
        if not session:
            raise ValueError("session_required: session is required.")
        return {"id": session, "pid": 123}

    def send_command(
        self,
        session: dict[str, Any],
        kind: str,
        payload: dict[str, Any] | None = None,
        *,
        timeout: float = 10.0,
    ) -> dict[str, Any]:
        self.calls.append(
            (
                "send_command",
                {
                    "session": session,
                    "kind": kind,
                    "payload": payload,
                    "timeout": timeout,
                },
            )
        )
        return {"ok": True}

    def build_start_command(self, **kwargs: Any) -> list[str]:
        self.calls.append(("build_start_command", dict(kwargs)))
        return ["mgba", str(kwargs["rom"])]

    def status(self, *, session: str | None = None, all: bool = False):
        self.calls.append(("status", {"session": session, "all": all}))
        return [{"session_id": "session-a"}] if all else {"session_id": session, "alive": True}

    def input_clear(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("input_clear", dict(kwargs)))
        return {"session_id": kwargs["session"], "frame": 101}

    def screenshot(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("screenshot", dict(kwargs)))
        return {"session_id": kwargs["session"], "frame": 77, "png_base64": "AA=="}

    def read_memory(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("read_memory", dict(kwargs)))
        return {"session_id": kwargs["session"], "frame": 10, "memory": {"0x1": 255}}


def test_resolve_session_requires_explicit_session(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = _Manager()
    monkeypatch.setattr(live_cli, "_manager", lambda: manager)

    args = argparse.Namespace(session="session-1")
    assert live_cli.resolve_session(args) == {"id": "session-1", "pid": 123}

    with pytest.raises(ValueError, match="session_required"):
        live_cli.resolve_session(argparse.Namespace(session=None))


def test_send_command_and_build_start_command_delegate(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = _Manager()
    monkeypatch.setattr(live_cli, "_manager", lambda: manager)

    assert live_cli.send_command({"id": "session-1"}, "ping", {"x": 1}, timeout=3.0) == {"ok": True}
    rom = Path("/tmp/game.gba")
    assert live_cli.build_start_command(
        mgba_path="mgba",
        fps_target=120.0,
        config_overrides=[],
        savestate=None,
        startup_scripts=[],
        log_level=0,
        rom=rom,
    ) == ["mgba", str(rom)]


def test_cmd_status_and_other_handlers_delegate(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = _Manager()
    captured: list[Any] = []
    monkeypatch.setattr(live_cli, "_manager", lambda: manager)
    monkeypatch.setattr(live_cli, "print_json", lambda payload: captured.append(payload))

    live_cli.cmd_status(argparse.Namespace(all=True, session=None))
    live_cli.cmd_status(argparse.Namespace(all=False, session="session-1"))
    live_cli.cmd_input_clear(argparse.Namespace(session="session-1", keys=None, timeout=7.0))
    live_cli.cmd_screenshot(
        argparse.Namespace(session="session-1", out=None, no_save=True, timeout=7.0)
    )
    live_cli.cmd_read_memory(
        argparse.Namespace(session="session-1", addresses=["0x1"], timeout=7.0)
    )

    assert captured[0] == [{"session_id": "session-a"}]
    assert captured[1] == {"session_id": "session-1", "alive": True}
    assert captured[2] == {"session_id": "session-1", "frame": 101}
    assert captured[3]["session_id"] == "session-1"
    assert captured[4]["memory"] == {"0x1": 255}
