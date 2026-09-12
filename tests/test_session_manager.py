from __future__ import annotations

import base64
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from mgba_live_mcp import process_control, session_transactions
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
                "command_path": str(session_dir / "command.lua"),
                "response_path": str(session_dir / "response.json"),
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

    monkeypatch.setattr(
        manager, "_process_state", lambda session: "alive" if session["pid"] == 2222 else "dead"
    )

    removed = manager.prune_dead_sessions()

    assert removed == ["session-dead"]
    assert not manager.session_dir("session-dead").exists()
    archived = list(manager.archived_sessions_dir.glob("session-dead-*"))
    assert len(archived) == 1
    assert (archived[0] / "session.json").exists()
    assert manager.get_active_session_id() == "session-live"


def test_send_command_does_not_overwrite_an_unclaimed_command(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    session_dir = manager.session_dir("session-busy")
    session_dir.mkdir()
    command_path = session_dir / "command.lua"
    command_path.write_text("return {}\n")
    with pytest.raises(RuntimeError, match="session_busy"):
        manager.send_command(
            {
                "command_path": str(command_path),
                "response_path": str(session_dir / "response.json"),
            },
            "ping",
            timeout=0.1,
        )
    assert command_path.read_text() == "return {}\n"


def test_timeout_keeps_unresolved_work_fenced_until_its_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = _manager(tmp_path)
    session_dir = manager.session_dir("session-timeout")
    session_dir.mkdir()
    command_path = session_dir / "command.lua"
    response_path = session_dir / "response.json"
    target = {"command_path": str(command_path), "response_path": str(response_path)}
    clock = iter([0.0, 0.2])
    with monkeypatch.context() as timing:
        timing.setattr(
            "mgba_live_mcp.session_manager.time",
            SimpleNamespace(monotonic=lambda: next(clock), sleep=lambda _: None),
        )
        with pytest.raises(TimeoutError, match="execution outcome unknown"):
            manager.send_command(target, "ping", timeout=0.1)
    pending = session_transactions.transaction_status(session_dir)
    assert pending is not None
    request_id = pending["operation"]["pending_request"]
    with pytest.raises(RuntimeError, match="session_busy"):
        manager.send_command(target, "ping", timeout=0.1)
    command_path.unlink()
    response_path.write_text(json.dumps({"id": request_id, "ok": True}))

    def completed_command(path: Path, command: dict[str, Any]) -> None:
        response_path.write_text(json.dumps({"id": command["id"], "ok": True, "frame": 7}))

    monkeypatch.setattr(manager, "write_command", completed_command)
    assert manager.send_command(target, "ping", timeout=0.1)["frame"] == 7


@pytest.mark.parametrize("state", ["permission_denied", "identity_unverified"])
def test_owned_response_wait_survives_indeterminate_process_inspection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    manager = _manager(tmp_path)
    _write_session(manager, "inspection-race", 1234)
    target = manager.load_session("inspection-race")
    directory = manager.session_dir("inspection-race")

    def uncertain_inspection(session: dict[str, Any]) -> str:
        pending = session_transactions.transaction_status(directory)
        assert pending is not None
        Path(target["command_path"]).unlink()
        Path(target["response_path"]).write_text(
            json.dumps({"id": pending["operation"]["pending_request"], "ok": True, "frame": 7})
        )
        return state

    monkeypatch.setattr(manager, "_process_state", uncertain_inspection)
    assert manager.send_command(target, "ping", timeout=1)["frame"] == 7
    journal = session_transactions.transaction_status(directory)
    assert journal is not None and journal["operation"] is None


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

    monkeypatch.setattr(
        manager, "_process_state", lambda session: "alive" if session["pid"] == 2222 else "dead"
    )

    payload = manager.status(all=True)

    assert isinstance(payload, list)
    assert [item["session_id"] for item in payload] == ["session-live"]
    assert payload[0]["heartbeat"] == heartbeat


def test_recovery_stop_fences_an_active_command_and_clears_active_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = _manager(tmp_path)
    _write_session(manager, "session-1", 1234)
    manager.set_active_session("session-1")
    alive = True

    def terminate(pid: int, identity: Any, *, grace: float) -> str:
        nonlocal alive
        alive = False
        return "stopped"

    monkeypatch.setattr(process_control, "terminate_owned_process", terminate)
    monkeypatch.setattr(manager, "_process_state", lambda session: "alive" if alive else "dead")
    with pytest.raises(RuntimeError, match="session_stopped"):
        with manager.transaction("session-1") as operation:
            result = manager.stop(session="session-1")
            assert result["stopped"] is True
            assert result["alive_after"] is False
            assert manager.get_active_session_id() is None
            operation.check()
    with pytest.raises(RuntimeError, match="session_stopped"):
        with manager.transaction("session-1"):
            pytest.fail("stopped generation admitted another operation")


def test_unconfirmed_stop_preserves_session_and_blocks_new_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = _manager(tmp_path)
    _write_session(manager, "session-1", 1234)
    manager.set_active_session("session-1")

    def denied(*args: Any, **kwargs: Any) -> str:
        raise RuntimeError("permission_denied: pid=1234 stage=TERM")

    monkeypatch.setattr(process_control, "terminate_owned_process", denied)
    monkeypatch.setattr(manager, "_process_state", lambda session: "permission_denied")
    with pytest.raises(RuntimeError, match="permission_denied"):
        manager.stop(session="session-1")
    assert manager.prune_dead_sessions() == []
    assert manager.get_active_session_id() == "session-1"
    status = manager.status(session="session-1")
    assert isinstance(status, dict)
    assert status["process_state"] == "permission_denied"
    with pytest.raises(RuntimeError, match="session_stopping"):
        with manager.transaction("session-1"):
            pytest.fail("unconfirmed stop admitted another operation")


def test_screenshot_supports_no_save_and_output_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = _manager(tmp_path)
    manager.session_dir("session-1").mkdir()
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
