from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from mgba_live_mcp.session_manager import SessionManager


def _manager(tmp_path: Path) -> SessionManager:
    bridge = tmp_path / "bridge.lua"
    bridge.write_text("-- bridge\n")
    manager = SessionManager(runtime_root=tmp_path / "runtime", bridge_script=bridge)
    manager.ensure_runtime_dirs()
    return manager


def _write_session(manager: SessionManager, session_id: str, pid: int) -> None:
    session_dir = manager.session_dir(session_id)
    session_dir.mkdir(parents=True, exist_ok=True)
    (session_dir / "session.json").write_text(
        json.dumps(
            {
                "id": session_id,
                "pid": pid,
                "rom": "/tmp/game.gba",
                "started_at": "2026-04-23T00:00:00+00:00",
            }
        )
    )


def test_require_session_raises_session_dead_for_non_live_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = _manager(tmp_path)
    _write_session(manager, "session-dead", 1234)
    monkeypatch.setattr(manager, "pid_alive", lambda pid: False)

    with pytest.raises(RuntimeError, match="session_dead"):
        manager.require_session("session-dead")


def test_prune_dead_sessions_archives_dead_sessions_and_clears_active_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = _manager(tmp_path)
    _write_session(manager, "session-dead", 1111)
    _write_session(manager, "session-live", 2222)
    manager.set_active_session("session-dead")

    monkeypatch.setattr(manager, "pid_alive", lambda pid: pid == 2222)

    removed = manager.prune_dead_sessions()

    assert removed == ["session-dead"]
    assert not manager.session_dir("session-dead").exists()
    archived = list(manager.archived_sessions_dir.glob("session-dead-*"))
    assert len(archived) == 1
    assert (archived[0] / "session.json").exists()
    assert manager.get_active_session_id() == "session-live"


def test_send_command_raises_session_busy_when_bridge_command_file_is_stuck(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = _manager(tmp_path)
    session_dir = manager.session_dir("session-busy")
    session_dir.mkdir(parents=True, exist_ok=True)
    command_path = session_dir / "command.lua"
    command_path.write_text("return {}\n")
    response_path = session_dir / "response.json"

    timeline = iter([0.0, 0.2])
    monkeypatch.setattr("mgba_live_mcp.session_manager.time.time", lambda: next(timeline))
    monkeypatch.setattr("mgba_live_mcp.session_manager.time.sleep", lambda _: None)

    with pytest.raises(RuntimeError, match="session_busy"):
        manager.send_command(
            {
                "command_path": str(command_path),
                "response_path": str(response_path),
            },
            "ping",
            timeout=0.1,
        )


def test_send_command_removes_stale_own_command_on_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = _manager(tmp_path)
    session_dir = manager.session_dir("session-timeout")
    session_dir.mkdir(parents=True, exist_ok=True)
    command_path = session_dir / "command.lua"
    response_path = session_dir / "response.json"

    monkeypatch.setattr(
        "mgba_live_mcp.session_manager.uuid.uuid4",
        lambda: SimpleNamespace(hex="req-123"),
    )
    timeline = iter([0.0, 0.0, 0.2, 0.2])
    monkeypatch.setattr("mgba_live_mcp.session_manager.time.time", lambda: next(timeline))
    monkeypatch.setattr("mgba_live_mcp.session_manager.time.sleep", lambda _: None)

    with pytest.raises(TimeoutError, match="Timed out waiting for response"):
        manager.send_command(
            {
                "command_path": str(command_path),
                "response_path": str(response_path),
            },
            "ping",
            timeout=0.1,
        )

    assert not command_path.exists()


def test_build_start_command_includes_scripts_bridge_and_rom(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    startup = tmp_path / "boot.lua"
    startup.write_text("-- boot\n")
    rom = tmp_path / "game.gba"
    rom.write_bytes(b"rom")
    savestate = tmp_path / "save.sav"
    savestate.write_bytes(b"save")
    bridge = tmp_path / "session-bridge.lua"
    bridge.write_text("-- session bridge\n")

    command = manager.build_start_command(
        mgba_path="/opt/mgba",
        fps_target=240.0,
        config_overrides=["video.scale=3", "audio.sync=false"],
        savestate=str(savestate),
        startup_scripts=[str(startup.resolve())],
        bridge_script=bridge,
        log_level=2,
        rom=rom,
    )

    assert command == [
        "/opt/mgba",
        "-C",
        "fpsTarget=240",
        "-s",
        "0",
        "-C",
        "video.scale=3",
        "-C",
        "audio.sync=false",
        "-t",
        str(savestate.resolve()),
        "--script",
        str(startup.resolve()),
        "--script",
        str(bridge),
        "-l",
        "2",
        str(rom),
    ]
