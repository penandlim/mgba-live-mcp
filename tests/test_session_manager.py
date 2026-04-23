from __future__ import annotations

import base64
import json
import signal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

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
    heartbeat_path = session_dir / "heartbeat.json"
    (session_dir / "session.json").write_text(
        json.dumps(
            {
                "id": session_id,
                "pid": pid,
                "rom": "/tmp/game.gba",
                "fps_target": 120.0,
                "mgba_path": "/opt/mgba",
                "session_dir": str(session_dir),
                "heartbeat_path": str(heartbeat_path),
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


def test_pid_alive_reaps_exited_child_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = _manager(tmp_path)
    kill_probes: list[int] = []

    monkeypatch.setattr("mgba_live_mcp.session_manager.os.waitpid", lambda pid, flags: (pid, 0))
    monkeypatch.setattr(
        "mgba_live_mcp.session_manager.os.kill",
        lambda pid, signal_number: kill_probes.append(signal_number),
    )

    assert manager.pid_alive(1234) is False
    assert kill_probes == []


def test_pid_alive_falls_back_to_signal_probe_for_non_child_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = _manager(tmp_path)
    probed: list[int] = []

    def fake_kill(pid: int, signal_number: int) -> None:
        probed.append(signal_number)

    monkeypatch.setattr(
        "mgba_live_mcp.session_manager.os.waitpid",
        lambda pid, flags: (_ for _ in ()).throw(ChildProcessError()),
    )
    monkeypatch.setattr("mgba_live_mcp.session_manager.os.kill", fake_kill)

    assert manager.pid_alive(1234) is True
    assert probed == [0]


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


def test_status_all_filters_dead_sessions_and_includes_heartbeat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = _manager(tmp_path)
    _write_session(manager, "session-dead", 1111)
    _write_session(manager, "session-live", 2222)
    heartbeat = {"frame": 77}
    heartbeat_path = manager.session_dir("session-live") / "heartbeat.json"
    heartbeat_path.write_text(json.dumps(heartbeat))

    session_live = manager.load_session("session-live")
    session_live["heartbeat_path"] = str(heartbeat_path)
    manager.write_session(session_live)
    manager.set_active_session("session-live")

    monkeypatch.setattr(manager, "pid_alive", lambda pid: pid == 2222)

    payload = manager.status(all=True)

    assert payload == [
        {
            "session_id": "session-live",
            "pid": 2222,
            "alive": True,
            "rom": "/tmp/game.gba",
            "fps_target": session_live.get("fps_target"),
            "mgba_path": session_live.get("mgba_path"),
            "heartbeat": heartbeat,
            "is_active": True,
            "session_dir": str(manager.session_dir("session-live")),
        }
    ]


def test_stop_refreshes_active_session_after_process_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = _manager(tmp_path)
    calls: list[tuple[str, Any]] = []
    target = {"id": "session-1", "pid": 1234}
    alive_values = iter([True, False])

    monkeypatch.setattr(manager, "require_session", lambda session, require_alive=False: target)
    monkeypatch.setattr(manager, "pid_alive", lambda pid: next(alive_values))
    monkeypatch.setattr(
        manager,
        "terminate_session_process",
        lambda pid, grace=1.0: calls.append(("terminate", (pid, grace))),
    )
    monkeypatch.setattr(manager, "get_active_session_id", lambda: "session-1")
    monkeypatch.setattr(manager, "_refresh_active_session", lambda: calls.append(("refresh", None)))

    payload = manager.stop(session="session-1", grace=2.5)

    assert payload == {
        "session_id": "session-1",
        "pid": 1234,
        "alive_before": True,
        "alive_after": False,
        "stopped": True,
    }
    assert calls == [("terminate", (1234, 2.5)), ("refresh", None)]


def test_terminate_session_process_treats_permission_error_as_exit_race(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = _manager(tmp_path)
    signals: list[int] = []

    def fake_killpg(pgid: int, sig: int) -> None:
        assert pgid == 1234
        signals.append(sig)
        if sig == signal.SIGKILL:
            raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr("mgba_live_mcp.session_manager.os.getpgid", lambda pid: 1234)
    monkeypatch.setattr("mgba_live_mcp.session_manager.os.killpg", fake_killpg)
    monkeypatch.setattr(manager, "pid_alive", lambda pid: False)

    manager.terminate_session_process(1234, grace=0.0)

    assert signals == [signal.SIGTERM, signal.SIGKILL]


def test_terminate_session_process_rechecks_after_permission_error_race(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = _manager(tmp_path)
    alive_values = iter([True, False])
    sleeps: list[float] = []

    def fake_killpg(pgid: int, sig: int) -> None:
        del pgid
        if sig == signal.SIGKILL:
            raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr("mgba_live_mcp.session_manager.os.getpgid", lambda pid: 1234)
    monkeypatch.setattr("mgba_live_mcp.session_manager.os.killpg", fake_killpg)
    monkeypatch.setattr(manager, "pid_alive", lambda pid: next(alive_values))
    monkeypatch.setattr(
        "mgba_live_mcp.session_manager.time.sleep", lambda value: sleeps.append(value)
    )

    manager.terminate_session_process(1234, grace=0.0)

    assert sleeps == [0.05]


def test_terminate_session_process_reraises_permission_error_when_pid_survives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = _manager(tmp_path)
    timeline = iter([0.0, 1.1, 2.0, 3.1])

    def fake_killpg(pgid: int, sig: int) -> None:
        del pgid
        if sig == signal.SIGKILL:
            raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr("mgba_live_mcp.session_manager.os.getpgid", lambda pid: 1234)
    monkeypatch.setattr("mgba_live_mcp.session_manager.os.killpg", fake_killpg)
    monkeypatch.setattr(manager, "pid_alive", lambda pid: True)
    monkeypatch.setattr("mgba_live_mcp.session_manager.time.time", lambda: next(timeline))
    monkeypatch.setattr("mgba_live_mcp.session_manager.time.sleep", lambda value: None)

    with pytest.raises(PermissionError):
        manager.terminate_session_process(1234, grace=0.0)


def test_run_lua_file_executes_existing_script(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = _manager(tmp_path)
    script = tmp_path / "script.lua"
    script.write_text("return true\n")
    target = {"id": "session-1"}
    sent: list[tuple[str, dict[str, Any], float]] = []

    monkeypatch.setattr(manager, "require_session", lambda session, require_alive=True: target)
    monkeypatch.setattr(
        manager,
        "send_command",
        lambda session, kind, payload, timeout=10.0: (
            sent.append((kind, payload or {}, timeout)) or {"frame": 12}
        ),
    )
    monkeypatch.setattr(manager, "handle_response", lambda response: {"ok": True})

    payload = manager.run_lua(session="session-1", file=str(script), timeout=9.0)

    assert payload == {
        "session_id": "session-1",
        "frame": 12,
        "data": {"ok": True},
    }
    assert sent == [("run_lua_file", {"path": str(script.resolve())}, 9.0)]


@pytest.mark.parametrize(
    ("method_name", "kwargs", "expected_kind", "expected_payload", "expected_key", "response_data"),
    [
        (
            "input_tap",
            {"session": "session-1", "key": "A", "frames": 3, "timeout": 4.0},
            "tap_key",
            {"key": "A", "duration": 3},
            "data",
            {"pressed": True},
        ),
        (
            "input_set",
            {"session": "session-1", "keys": ["A", "B"], "timeout": 4.0},
            "set_keys",
            {"keys": ["A", "B"]},
            "data",
            {"keys": [0, 1]},
        ),
        (
            "input_clear",
            {"session": "session-1", "keys": ["A"], "timeout": 4.0},
            "clear_keys",
            {"keys": ["A"]},
            "data",
            {"cleared": True},
        ),
        (
            "read_memory",
            {"session": "session-1", "addresses": ["0x10", 32], "timeout": 4.0},
            "read_memory",
            {"addresses": [16, 32]},
            "memory",
            {"0x10": 255},
        ),
        (
            "read_range",
            {"session": "session-1", "start": "0x20", "length": 8, "timeout": 4.0},
            "read_range",
            {"start": 32, "length": 8},
            "range",
            {"start": 32, "length": 8, "data": [1, 2]},
        ),
        (
            "dump_pointers",
            {"session": "session-1", "start": "0x30", "count": 2, "width": 4, "timeout": 4.0},
            "dump_pointers",
            {"start": 48, "count": 2, "width": 4},
            "pointers",
            {"count": 2},
        ),
        (
            "dump_oam",
            {"session": "session-1", "count": 5, "timeout": 4.0},
            "dump_oam",
            {"count": 5},
            "oam",
            {"count": 5},
        ),
        (
            "dump_entities",
            {"session": "session-1", "base": "0x40", "size": 24, "count": 2, "timeout": 4.0},
            "dump_entities",
            {"base": 64, "size": 24, "count": 2},
            "entities",
            {"count": 2},
        ),
    ],
)
def test_runtime_wrapper_commands_return_structured_payloads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    method_name: str,
    kwargs: dict[str, Any],
    expected_kind: str,
    expected_payload: dict[str, Any],
    expected_key: str,
    response_data: dict[str, Any],
) -> None:
    manager = _manager(tmp_path)
    target = {"id": "session-1"}
    sent: list[tuple[str, dict[str, Any], float]] = []

    monkeypatch.setattr(manager, "require_session", lambda session, require_alive=True: target)
    monkeypatch.setattr(
        manager,
        "send_command",
        lambda session, kind, payload, timeout=10.0: (
            sent.append((kind, payload or {}, timeout)) or {"frame": 33}
        ),
    )
    monkeypatch.setattr(manager, "handle_response", lambda response: response_data)

    payload = getattr(manager, method_name)(**kwargs)

    assert payload["session_id"] == "session-1"
    assert payload["frame"] == 33
    assert payload[expected_key] == response_data
    assert sent == [(expected_kind, expected_payload, 4.0)]


def test_screenshot_supports_no_save_and_output_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = _manager(tmp_path)
    target = {"id": "session-1"}
    sent: list[tuple[str, dict[str, Any], float]] = []

    def fake_send_command(
        session: dict[str, Any],
        kind: str,
        payload: dict[str, Any] | None = None,
        timeout: float = 10.0,
    ) -> dict[str, Any]:
        assert payload is not None
        path = Path(payload["path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"png-bytes")
        sent.append((kind, payload, timeout))
        return {"frame": 44}

    monkeypatch.setattr(manager, "require_session", lambda session, require_alive=True: target)
    monkeypatch.setattr(manager, "send_command", fake_send_command)
    monkeypatch.setattr(manager, "handle_response", lambda response: {"path": sent[-1][1]["path"]})

    in_memory = manager.screenshot(session="session-1", no_save=True, timeout=6.0)
    out_path = tmp_path / "shot.png"
    persisted = manager.screenshot(session="session-1", out=str(out_path), timeout=7.0)

    assert in_memory == {
        "session_id": "session-1",
        "frame": 44,
        "png_base64": base64.b64encode(b"png-bytes").decode(),
    }
    assert persisted == {
        "session_id": "session-1",
        "frame": 44,
        "path": str(out_path.resolve()),
    }
    assert sent[0][0] == "screenshot"
    assert sent[0][2] == 6.0
    assert sent[1] == ("screenshot", {"path": str(out_path.resolve())}, 7.0)
