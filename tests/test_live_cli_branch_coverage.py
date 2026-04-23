from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import pytest

import mgba_live_mcp.live_cli as live_cli


class _Manager:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def start(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("start", dict(kwargs)))
        return {"status": "started", "session_id": kwargs.get("session_id") or "session-1"}

    def attach(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("attach", dict(kwargs)))
        return {"status": "attached", "session_id": kwargs["session"] or "session-1"}

    def require_session(self, session: str | None, *, require_alive: bool = True) -> dict[str, Any]:
        self.calls.append(("require_session", {"session": session, "require_alive": require_alive}))
        if not session:
            raise ValueError("session_required: session is required.")
        return {"id": session, "pid": 123}

    def stop(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("stop", dict(kwargs)))
        return {"session_id": kwargs["session"], "stopped": True}

    def run_lua(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("run_lua", dict(kwargs)))
        return {"session_id": kwargs["session"], "frame": 12}

    def input_tap(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("input_tap", dict(kwargs)))
        return {"session_id": kwargs["session"], "frame": 13}

    def input_set(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("input_set", dict(kwargs)))
        return {"session_id": kwargs["session"], "frame": 14}

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

    def read_range(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("read_range", dict(kwargs)))
        return {"session_id": kwargs["session"], "frame": 11, "range": {"length": kwargs["length"]}}

    def dump_pointers(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("dump_pointers", dict(kwargs)))
        return {
            "session_id": kwargs["session"],
            "frame": 12,
            "pointers": {"count": kwargs["count"]},
        }

    def dump_oam(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("dump_oam", dict(kwargs)))
        return {"session_id": kwargs["session"], "frame": 13, "oam": {"count": kwargs["count"]}}

    def dump_entities(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("dump_entities", dict(kwargs)))
        return {
            "session_id": kwargs["session"],
            "frame": 14,
            "entities": {"count": kwargs["count"]},
        }


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


def test_all_command_handlers_delegate_and_main_runs_selected_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _Manager()
    captured: list[Any] = []
    parser_calls: list[str] = []
    monkeypatch.setattr(live_cli, "_manager", lambda: manager)
    monkeypatch.setattr(live_cli, "print_json", lambda payload: captured.append(payload))

    live_cli.cmd_start(
        argparse.Namespace(
            rom="/tmp/game.gba",
            savestate=None,
            fps_target=120.0,
            fast=False,
            mgba_path="mgba",
            session_id="session-1",
            script=["boot.lua"],
            log_level=0,
            heartbeat_interval=30,
            ready_timeout=20.0,
            config=["audio.sync=0"],
        )
    )
    live_cli.cmd_attach(argparse.Namespace(session="session-1", pid=None))
    live_cli.cmd_stop(argparse.Namespace(session="session-1", grace=2.0))
    live_cli.cmd_run_lua(
        argparse.Namespace(session="session-1", file=None, code="return true", timeout=5.0)
    )
    live_cli.cmd_input_tap(argparse.Namespace(session="session-1", key="A", frames=2, timeout=5.0))
    live_cli.cmd_input_set(argparse.Namespace(session="session-1", keys=["A", "B"], timeout=5.0))
    live_cli.cmd_read_range(
        argparse.Namespace(session="session-1", start="0x20", length=8, timeout=5.0)
    )
    live_cli.cmd_dump_pointers(
        argparse.Namespace(session="session-1", start="0x30", count=2, width=4, timeout=5.0)
    )
    live_cli.cmd_dump_oam(argparse.Namespace(session="session-1", count=3, timeout=5.0))
    live_cli.cmd_dump_entities(
        argparse.Namespace(session="session-1", base="0x40", size=24, count=2, timeout=5.0)
    )

    class _Parser:
        def parse_args(self) -> argparse.Namespace:
            return argparse.Namespace(
                func=lambda _args: parser_calls.append("called"),
            )

    monkeypatch.setattr(live_cli, "build_parser", lambda: _Parser())
    monkeypatch.setattr(live_cli, "ensure_runtime_dirs", lambda: parser_calls.append("dirs"))
    monkeypatch.setattr(live_cli, "prune_dead_sessions", lambda: parser_calls.append("prune"))
    live_cli.main()

    assert captured[0]["status"] == "started"
    assert captured[1]["status"] == "attached"
    assert captured[2]["stopped"] is True
    assert captured[3]["frame"] == 12
    assert captured[4]["frame"] == 13
    assert captured[5]["frame"] == 14
    assert captured[6]["range"] == {"length": 8}
    assert captured[7]["pointers"] == {"count": 2}
    assert captured[8]["oam"] == {"count": 3}
    assert captured[9]["entities"] == {"count": 2}
    assert parser_calls == ["dirs", "prune", "called"]
