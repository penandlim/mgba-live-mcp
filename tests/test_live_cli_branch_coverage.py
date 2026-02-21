from __future__ import annotations

import argparse
import json
import signal
import threading
import time
from pathlib import Path
from typing import Any

import pytest

import mgba_live_mcp.live_cli as live_cli


def _ns(**kwargs: Any) -> argparse.Namespace:
    return argparse.Namespace(**kwargs)


@pytest.fixture
def isolated_runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[Path, Path, Path]:
    runtime = tmp_path / "runtime"
    sessions_dir = runtime / "sessions"
    active_file = runtime / "active_session"
    monkeypatch.setattr(live_cli, "RUNTIME_ROOT", runtime)
    monkeypatch.setattr(live_cli, "SESSIONS_DIR", sessions_dir)
    monkeypatch.setattr(live_cli, "ACTIVE_SESSION_FILE", active_file)
    return runtime, sessions_dir, active_file


def test_parse_and_lua_conversion_helpers() -> None:
    assert live_cli.parse_int("0x10") == 16
    assert live_cli.to_lua_string('a"b') == '"a\\"b"'
    assert live_cli.to_lua_value(None) == "nil"
    assert live_cli.to_lua_value(True) == "true"
    assert live_cli.to_lua_value(3) == "3"
    assert live_cli.to_lua_value("x") == '"x"'
    assert live_cli.to_lua_value([1, "a"]) == '{1, "a"}'
    assert "alpha" in live_cli.to_lua_value({"alpha": 1, "1-key": 2})
    with pytest.raises(TypeError, match="Unsupported command value type"):
        live_cli.to_lua_value({1, 2})


def test_load_and_iter_sessions_with_invalid_entries(
    isolated_runtime: tuple[Path, Path, Path],
) -> None:
    _runtime, sessions_dir, _active_file = isolated_runtime
    sessions_dir.mkdir(parents=True)

    good_dir = sessions_dir / "good"
    good_dir.mkdir()
    (good_dir / "session.json").write_text(json.dumps({"id": "good", "pid": 1}))

    bad_dir = sessions_dir / "bad"
    bad_dir.mkdir()
    (bad_dir / "session.json").write_text("{")

    loaded = live_cli.load_session("good")
    assert loaded["id"] == "good"

    sessions = live_cli.iter_sessions()
    assert [s["id"] for s in sessions] == ["good"]


def test_pid_alive_handles_os_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def raise_os_error(_pid: int, _sig: int) -> None:
        raise OSError("dead")

    monkeypatch.setattr(live_cli.os, "kill", raise_os_error)
    assert live_cli.pid_alive(123) is False


def test_pid_alive_true(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(live_cli.os, "kill", lambda _pid, _sig: None)
    assert live_cli.pid_alive(123) is True


def test_refresh_active_session_paths(
    isolated_runtime: tuple[Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _runtime, sessions_dir, active_file = isolated_runtime
    sessions_dir.mkdir(parents=True)

    live_cli._refresh_active_session()
    assert active_file.exists() is False

    active_file.write_text("dead")
    dead_dir = sessions_dir / "dead"
    dead_dir.mkdir()
    (dead_dir / "session.json").write_text("{")

    good_dir = sessions_dir / "alive"
    good_dir.mkdir()
    (good_dir / "session.json").write_text(json.dumps({"id": "alive", "pid": 99}))

    monkeypatch.setattr(live_cli, "pid_alive", lambda pid: int(pid) == 99)
    live_cli._refresh_active_session()
    assert active_file.read_text().strip() == "alive"

    active_file.write_text("dead")
    monkeypatch.setattr(live_cli, "iter_sessions", lambda: [{"id": "oops"}])
    live_cli._refresh_active_session()


def test_refresh_active_session_keeps_alive_active(
    isolated_runtime: tuple[Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _runtime, sessions_dir, active_file = isolated_runtime
    sessions_dir.mkdir(parents=True)
    active_file.write_text("alive")
    alive_dir = sessions_dir / "alive"
    alive_dir.mkdir()
    (alive_dir / "session.json").write_text(json.dumps({"id": "alive", "pid": 7}))
    monkeypatch.setattr(live_cli, "pid_alive", lambda pid: int(pid) == 7)
    live_cli._refresh_active_session()
    assert active_file.read_text().strip() == "alive"


def test_prune_dead_sessions_handles_removal_errors(
    isolated_runtime: tuple[Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _runtime, sessions_dir, _active_file = isolated_runtime
    sessions_dir.mkdir(parents=True)

    bad_json_dir = sessions_dir / "bad"
    bad_json_dir.mkdir()
    (bad_json_dir / "session.json").write_text("{")

    dead_dir = sessions_dir / "dead"
    dead_dir.mkdir()
    (dead_dir / "session.json").write_text(json.dumps({"id": "dead", "pid": 1}))

    monkeypatch.setattr(live_cli, "pid_alive", lambda _pid: False)
    monkeypatch.setattr(
        live_cli.shutil, "rmtree", lambda _path: (_ for _ in ()).throw(OSError("x"))
    )
    removed = live_cli.prune_dead_sessions()
    assert removed == []


def test_detect_mgba_binary_raises_when_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(live_cli.shutil, "which", lambda _name: None)
    with pytest.raises(SystemExit, match="No mGBA binary found"):
        live_cli.detect_mgba_binary()


def test_detect_mgba_binary_returns_found_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        live_cli.shutil,
        "which",
        lambda name: "/usr/bin/mgba-qt" if name == "mgba-qt" else None,
    )
    assert live_cli.detect_mgba_binary() == "/usr/bin/mgba-qt"


def test_resolve_session_branches(
    isolated_runtime: tuple[Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _runtime, sessions_dir, active_file = isolated_runtime
    sessions_dir.mkdir(parents=True)

    monkeypatch.setattr(live_cli, "get_active_session_id", lambda: None)
    monkeypatch.setattr(live_cli, "iter_sessions", lambda: [])
    with pytest.raises(SystemExit, match="No session specified"):
        live_cli.resolve_session(_ns(session=None))

    with pytest.raises(SystemExit, match="Session not found"):
        live_cli.resolve_session(_ns(session="missing"))

    dead_dir = sessions_dir / "dead"
    dead_dir.mkdir()
    (dead_dir / "session.json").write_text(json.dumps({"id": "dead", "pid": 10}))
    alive_dir = sessions_dir / "alive"
    alive_dir.mkdir()
    (alive_dir / "session.json").write_text(json.dumps({"id": "alive", "pid": 20}))

    monkeypatch.setattr(
        live_cli,
        "iter_sessions",
        lambda: [{"id": "dead", "pid": 10}, {"id": "alive", "pid": 20}],
    )
    monkeypatch.setattr(live_cli, "pid_alive", lambda pid: int(pid) == 20)
    resolved = live_cli.resolve_session(_ns(session=None), require_alive=True)
    assert resolved["id"] == "alive"
    assert active_file.exists() is False

    monkeypatch.setattr(live_cli, "iter_sessions", lambda: [{"id": "dead", "pid": 10}])
    monkeypatch.setattr(live_cli, "pid_alive", lambda _pid: False)
    with pytest.raises(SystemExit, match="process is not alive"):
        live_cli.resolve_session(_ns(session="dead"), require_alive=True)


def test_write_command_and_send_command_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    command_path = tmp_path / "command.lua"
    response_path = tmp_path / "response.json"
    session = {"command_path": str(command_path), "response_path": str(response_path)}

    class _U:
        hex = "req1"

    monkeypatch.setattr(live_cli.uuid, "uuid4", lambda: _U())

    def writer() -> None:
        while not command_path.exists():
            time.sleep(0.005)
        response_path.write_text("{")
        time.sleep(0.03)
        response_path.write_text(json.dumps({"id": "other", "ok": True}))
        time.sleep(0.03)
        response_path.write_text(json.dumps({"id": "req1", "ok": True, "data": {"x": 1}}))

    thread = threading.Thread(target=writer, daemon=True)
    thread.start()
    response = live_cli.send_command(session, "ping", timeout=1.0)
    thread.join(timeout=1.0)
    assert response["id"] == "req1"


def test_send_command_unlinks_stale_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    command_path = tmp_path / "command.lua"
    response_path = tmp_path / "response.json"
    response_path.write_text(json.dumps({"id": "stale", "ok": True}))
    session = {"command_path": str(command_path), "response_path": str(response_path)}

    class _U:
        hex = "req2"

    monkeypatch.setattr(live_cli.uuid, "uuid4", lambda: _U())

    def writer() -> None:
        while not command_path.exists():
            time.sleep(0.005)
        response_path.write_text(json.dumps({"id": "req2", "ok": True}))

    thread = threading.Thread(target=writer, daemon=True)
    thread.start()
    response = live_cli.send_command(session, "ping", timeout=1.0)
    thread.join(timeout=1.0)
    assert response["id"] == "req2"


def test_send_command_busy_and_response_timeouts(tmp_path: Path) -> None:
    command_path = tmp_path / "command.lua"
    response_path = tmp_path / "response.json"
    session = {"command_path": str(command_path), "response_path": str(response_path)}

    command_path.write_text("busy")
    with pytest.raises(TimeoutError, match="Bridge is busy"):
        live_cli.send_command(session, "ping", timeout=0.05)

    command_path.unlink()
    with pytest.raises(TimeoutError, match="Timed out waiting for response"):
        live_cli.send_command(session, "ping", timeout=0.05)


def test_print_json_output(capsys: pytest.CaptureFixture[str]) -> None:
    live_cli.print_json({"ok": True})
    out = capsys.readouterr().out
    assert '"ok": true' in out


def test_startup_and_bridge_script_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(SystemExit, match="Script not found"):
        live_cli.resolve_startup_scripts([str(tmp_path / "missing.lua")])

    monkeypatch.setattr(live_cli, "BRIDGE_SCRIPT", tmp_path / "missing_bridge.lua")
    with pytest.raises(SystemExit, match="Bridge script missing"):
        live_cli.prepare_bridge_script(tmp_path)

    bridge = tmp_path / "bridge.lua"
    bridge.write_text("-- bridge")
    monkeypatch.setattr(live_cli, "BRIDGE_SCRIPT", bridge)
    monkeypatch.setattr(
        live_cli.shutil, "copy2", lambda _src, _dst: (_ for _ in ()).throw(OSError("x"))
    )
    with pytest.raises(SystemExit, match="Failed to stage bridge script"):
        live_cli.prepare_bridge_script(tmp_path)


def test_build_start_command_with_savestate(tmp_path: Path) -> None:
    rom = tmp_path / "game.gba"
    rom.write_bytes(b"rom")
    state = tmp_path / "save.ss0"
    state.write_bytes(b"s")
    cmd = live_cli.build_start_command(
        mgba_path="mgba-qt",
        fps_target=120.0,
        config_overrides=[],
        savestate=str(state),
        startup_scripts=[],
        bridge_script=tmp_path / "bridge.lua",
        log_level=0,
        rom=rom,
    )
    assert "-t" in cmd
    assert str(state.resolve()) in cmd


def test_cmd_start_error_paths(
    isolated_runtime: tuple[Path, Path, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _runtime, sessions_dir, _active_file = isolated_runtime
    sessions_dir.mkdir(parents=True)

    args = _ns(
        rom=str(tmp_path / "missing.gba"),
        savestate=None,
        fps_target=None,
        fast=False,
        mgba_path="mgba-qt",
        session_id="s1",
        script=[],
        log_level=0,
        heartbeat_interval=30,
        ready_timeout=0.05,
        config=[],
    )
    with pytest.raises(SystemExit, match="ROM not found"):
        live_cli.cmd_start(args)

    rom = tmp_path / "game.gba"
    rom.write_bytes(b"rom")
    (sessions_dir / "s1").mkdir()
    args.rom = str(rom)
    with pytest.raises(SystemExit, match="Session already exists"):
        live_cli.cmd_start(args)

    args.session_id = "s2"
    monkeypatch.setattr(
        live_cli, "prepare_bridge_script", lambda scripts_dir: scripts_dir / "bridge.lua"
    )

    class _Proc:
        pid = 123

    monkeypatch.setattr(live_cli.subprocess, "Popen", lambda *_a, **_k: _Proc())
    monkeypatch.setattr(live_cli, "pid_alive", lambda _pid: False)
    with pytest.raises(SystemExit, match="exited early"):
        live_cli.cmd_start(args)

    args.session_id = "s3"
    monkeypatch.setattr(live_cli, "pid_alive", lambda _pid: True)
    monkeypatch.setattr(
        live_cli, "send_command", lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("x"))
    )
    monkeypatch.setattr(live_cli.time, "sleep", lambda _secs: None)
    with pytest.raises(SystemExit, match="did not become ready"):
        live_cli.cmd_start(args)


def test_cmd_attach_status_stop_and_handle_response(
    isolated_runtime: tuple[Path, Path, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _runtime, _sessions_dir, active_file = isolated_runtime

    monkeypatch.setattr(live_cli, "iter_sessions", lambda: [])
    with pytest.raises(SystemExit, match="PID is not a managed"):
        live_cli.cmd_attach(_ns(session=None, pid=999))

    with pytest.raises(SystemExit, match="Provide --session or --pid"):
        live_cli.cmd_attach(_ns(session=None, pid=None))

    monkeypatch.setattr(live_cli, "load_session", lambda _sid: {"pid": 1})
    monkeypatch.setattr(live_cli, "pid_alive", lambda _pid: False)
    with pytest.raises(SystemExit, match="process is not alive"):
        live_cli.cmd_attach(_ns(session="s1", pid=None))

    hb = tmp_path / "heartbeat.json"
    hb.write_text("{")
    captured: list[Any] = []
    monkeypatch.setattr(live_cli, "print_json", lambda value: captured.append(value))
    monkeypatch.setattr(
        live_cli,
        "iter_sessions",
        lambda: [{"id": "s1", "pid": 10, "rom": "r", "fps_target": 120, "heartbeat_path": str(hb)}],
    )
    monkeypatch.setattr(live_cli, "pid_alive", lambda _pid: True)
    live_cli.cmd_status(_ns(all=True, session=None))
    assert captured[-1][0]["heartbeat"] is None

    monkeypatch.setattr(
        live_cli,
        "resolve_session",
        lambda _args, require_alive=True: {
            "id": "s2",
            "pid": 20,
            "rom": "r",
            "fps_target": 120,
            "heartbeat_path": str(hb),
            "session_dir": str(tmp_path),
        },
    )
    live_cli.cmd_status(_ns(all=False, session=None))
    assert captured[-1]["heartbeat"] is None

    monkeypatch.setattr(
        live_cli, "resolve_session", lambda _args, require_alive=False: {"id": "s2", "pid": 20}
    )
    states = iter([True, False])
    monkeypatch.setattr(live_cli, "pid_alive", lambda _pid: next(states))
    monkeypatch.setattr(live_cli, "terminate_session_process", lambda _pid, grace=1.0: None)
    active_file.write_text("s2")
    live_cli.cmd_stop(_ns(session="s2", grace=0.1))
    assert active_file.exists() is False

    with pytest.raises(SystemExit, match="Bridge error"):
        live_cli.handle_response({"ok": False, "error": "boom"})

    monkeypatch.setattr(
        live_cli,
        "iter_sessions",
        lambda: [{"id": "attached", "pid": 777, "rom": "r", "fps_target": 120}],
    )
    monkeypatch.setattr(
        live_cli,
        "load_session",
        lambda _sid: {"pid": 777, "rom": "r", "fps_target": 120},
    )
    monkeypatch.setattr(live_cli, "pid_alive", lambda _pid: True)
    live_cli.cmd_attach(_ns(session=None, pid=777))
    assert captured[-1]["status"] == "attached"


def test_terminate_session_process_branches(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        live_cli.os,
        "getpgid",
        lambda _pid: (_ for _ in ()).throw(ProcessLookupError()),
    )
    live_cli.terminate_session_process(1)

    monkeypatch.setattr(live_cli.os, "getpgid", lambda _pid: 1)
    monkeypatch.setattr(
        live_cli.os,
        "killpg",
        lambda _pgid, _sig: (_ for _ in ()).throw(ProcessLookupError()),
    )
    live_cli.terminate_session_process(1)

    signals: list[int] = []
    monkeypatch.setattr(live_cli.os, "getpgid", lambda _pid: 2)
    monkeypatch.setattr(live_cli.os, "killpg", lambda _pgid, sig: signals.append(int(sig)))
    monkeypatch.setattr(live_cli, "pid_alive", lambda _pid: True)
    live_cli.terminate_session_process(2, grace=0.0)
    assert int(signal.SIGTERM) in signals
    assert int(signal.SIGKILL) in signals

    monkeypatch.setattr(live_cli.os, "getpgid", lambda _pid: 3)
    monkeypatch.setattr(live_cli.os, "killpg", lambda _pgid, _sig: None)
    state = iter([True, False])
    monkeypatch.setattr(live_cli, "pid_alive", lambda _pid: next(state))
    live_cli.terminate_session_process(3, grace=0.2)

    monkeypatch.setattr(live_cli.os, "getpgid", lambda _pid: 4)
    sig_seen = {"count": 0}

    def killpg_raise_on_kill(_pgid: int, sig: int) -> None:
        sig_seen["count"] += 1
        if sig == int(signal.SIGKILL):
            raise ProcessLookupError()

    monkeypatch.setattr(live_cli.os, "killpg", killpg_raise_on_kill)
    monkeypatch.setattr(live_cli, "pid_alive", lambda _pid: True)
    live_cli.terminate_session_process(4, grace=0.0)


def test_command_handlers_and_screenshot_branches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[Any] = []
    monkeypatch.setattr(live_cli, "print_json", lambda value: captured.append(value))
    monkeypatch.setattr(live_cli, "resolve_session", lambda _args: {"session_dir": str(tmp_path)})

    with pytest.raises(SystemExit, match="Use either --file or --code"):
        live_cli.cmd_run_lua(_ns(file="a.lua", code="print(1)", timeout=1.0, session=None))
    with pytest.raises(SystemExit, match="Provide --file or --code"):
        live_cli.cmd_run_lua(_ns(file=None, code=None, timeout=1.0, session=None))
    with pytest.raises(SystemExit, match="Lua file not found"):
        live_cli.cmd_run_lua(
            _ns(file=str(tmp_path / "missing.lua"), code=None, timeout=1.0, session=None)
        )

    lua_file = tmp_path / "ok.lua"
    lua_file.write_text("return 1")
    monkeypatch.setattr(
        live_cli,
        "send_command",
        lambda _session, _kind, _payload, timeout=10.0: {"ok": True, "frame": 9, "data": {"ok": 1}},
    )
    live_cli.cmd_run_lua(_ns(file=str(lua_file), code=None, timeout=1.0, session=None))
    live_cli.cmd_run_lua(_ns(file=None, code="return 1", timeout=1.0, session=None))
    live_cli.cmd_input_tap(_ns(key="A", frames=2, timeout=1.0, session=None))
    live_cli.cmd_input_set(_ns(keys=["A", "B"], timeout=1.0, session=None))
    live_cli.cmd_input_clear(_ns(keys=["A"], timeout=1.0, session=None))

    with pytest.raises(SystemExit, match="either --out or --no-save"):
        live_cli.cmd_screenshot(
            _ns(no_save=True, out=str(tmp_path / "x.png"), timeout=1.0, session=None)
        )

    out_path = tmp_path / "shot.png"
    monkeypatch.setattr(
        live_cli,
        "send_command",
        lambda _session, _kind, _payload, timeout=10.0: {
            "ok": True,
            "frame": 3,
            "data": {"path": str(out_path)},
        },
    )
    live_cli.cmd_screenshot(_ns(no_save=False, out=str(out_path), timeout=1.0, session=None))
    assert captured[-1]["path"] == str(out_path)

    no_save_path = tmp_path / "nosave.png"

    class _Tmp:
        def __init__(self, name: Path) -> None:
            self.name = str(name)

        def __enter__(self) -> _Tmp:
            return self

        def __exit__(self, exc_type, exc, tb) -> bool:
            del exc_type, exc, tb
            return False

    monkeypatch.setattr(
        live_cli.tempfile,
        "NamedTemporaryFile",
        lambda suffix, delete: _Tmp(no_save_path),
    )

    def screenshot_send(_session, _kind, _payload, timeout=10.0):
        del timeout
        no_save_path.write_bytes(b"png")
        return {"ok": True, "frame": 4, "data": {"path": str(no_save_path)}}

    monkeypatch.setattr(live_cli, "send_command", screenshot_send)
    original_unlink = Path.unlink

    def flaky_unlink(self: Path, *args: Any, **kwargs: Any) -> None:
        if self == no_save_path:
            raise OSError("cannot delete")
        original_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", flaky_unlink)
    live_cli.cmd_screenshot(_ns(no_save=True, out=None, timeout=1.0, session=None))

    other_path = tmp_path / "other.png"
    temp_path = tmp_path / "tmp-no-save.png"

    monkeypatch.setattr(
        live_cli.tempfile,
        "NamedTemporaryFile",
        lambda suffix, delete: _Tmp(temp_path),
    )

    def screenshot_send_other(_session, _kind, _payload, timeout=10.0):
        del timeout
        other_path.write_bytes(b"png2")
        return {"ok": True, "frame": 7, "data": {"path": str(other_path)}}

    monkeypatch.setattr(live_cli, "send_command", screenshot_send_other)
    monkeypatch.setattr(Path, "unlink", flaky_unlink)
    live_cli.cmd_screenshot(_ns(no_save=True, out=None, timeout=1.0, session=None))

    monkeypatch.setattr(
        live_cli,
        "resolve_session",
        lambda _args: {"session_dir": str(tmp_path)},
    )
    monkeypatch.setattr(
        live_cli,
        "send_command",
        lambda _session, _kind, _payload, timeout=10.0: {
            "ok": True,
            "frame": 8,
            "data": {"path": str(tmp_path / "screenshots" / "auto.png")},
        },
    )
    live_cli.cmd_screenshot(_ns(no_save=False, out=None, timeout=1.0, session=None))

    monkeypatch.setattr(
        live_cli,
        "send_command",
        lambda _session, _kind, _payload, timeout=10.0: {"ok": True, "frame": 5, "data": [1, 2]},
    )
    live_cli.cmd_read_memory(_ns(addresses=["0x10", "32"], timeout=1.0, session=None))
    live_cli.cmd_read_range(_ns(start="0x20", length=8, timeout=1.0, session=None))
    live_cli.cmd_dump_pointers(_ns(start="0x30", count=2, width=4, timeout=1.0, session=None))
    live_cli.cmd_dump_oam(_ns(count=4, timeout=1.0, session=None))
    live_cli.cmd_dump_entities(_ns(base="0x40", size=24, count=2, timeout=1.0, session=None))


def test_live_cli_main_invokes_selected_command(monkeypatch: pytest.MonkeyPatch) -> None:
    called: dict[str, Any] = {}

    parser = live_cli.build_parser()

    def fake_parse_args() -> argparse.Namespace:
        return _ns(cmd="status", func=lambda args: called.setdefault("args", args))

    monkeypatch.setattr(parser, "parse_args", fake_parse_args)
    monkeypatch.setattr(live_cli, "build_parser", lambda: parser)
    monkeypatch.setattr(live_cli, "ensure_runtime_dirs", lambda: None)
    monkeypatch.setattr(live_cli, "prune_dead_sessions", lambda: [])

    live_cli.main()
    assert "args" in called


def test_module_main_guard_executes_main(monkeypatch: pytest.MonkeyPatch) -> None:
    called = {"value": False}

    def fake_main() -> None:
        called["value"] = True

    monkeypatch.setattr(live_cli, "main", fake_main)
    exec(
        "if __name__ == '__main__':\n    main()\n", {"__name__": "__main__", "main": live_cli.main}
    )
    assert called["value"] is True
