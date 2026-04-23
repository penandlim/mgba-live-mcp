from __future__ import annotations

import argparse
import asyncio
import base64
import importlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from mgba_live_mcp import server as mcp_server

REPO_ROOT = Path(__file__).resolve().parents[1]


def load_mgba_live_module() -> Any:
    return importlib.import_module("mgba_live_mcp.live_cli")


mgba_live = load_mgba_live_module()


def rom_fixture_path() -> Path:
    synthetic_rom = REPO_ROOT / "tests" / "fixtures" / "synthetic.gb"
    assert synthetic_rom.exists(), f"Synthetic ROM fixture not found in {synthetic_rom}"
    return synthetic_rom.resolve()


def _first_payload(contents: Any) -> dict[str, Any]:
    if isinstance(contents, tuple):
        _, payload = contents
        assert isinstance(payload, dict)
        return payload

    assert contents
    first = contents[0]
    assert getattr(first, "type", None) == "text"
    return json.loads(first.text)


@dataclass
class _Result:
    payload: dict[str, Any]


def test_start_parser_accepts_script_flag_with_synthetic_rom() -> None:
    rom = rom_fixture_path()
    parser = mgba_live.build_parser()
    args = parser.parse_args(
        ["start", "--rom", str(rom), "--script", "boot.lua", "--script", "hud.lua"]
    )
    assert args.script == ["boot.lua", "hud.lua"]


def test_screenshot_parser_rejects_removed_text_flags() -> None:
    parser = mgba_live.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["screenshot", "--text-format", "hex"])
    with pytest.raises(SystemExit):
        parser.parse_args(["screenshot", "--png"])


def test_cmd_screenshot_no_save_returns_base64_and_deletes_temp(monkeypatch: Any) -> None:
    captured: dict[str, Any] = {}
    written_paths: list[Path] = []
    png_bytes = b"\x89PNG\r\n\x1a\n\x00"

    monkeypatch.setattr(mgba_live, "resolve_session", lambda *_args, **_kwargs: {"id": "session-1"})

    def fake_send_command(
        _session: dict[str, Any],
        kind: str,
        payload: dict[str, Any],
        timeout: float,
    ) -> dict[str, Any]:
        assert kind == "screenshot"
        assert timeout == 7.0
        path = Path(payload["path"])
        written_paths.append(path)
        path.write_bytes(png_bytes)
        return {"ok": True, "frame": 77, "data": {"path": str(path)}}

    monkeypatch.setattr(mgba_live, "send_command", fake_send_command)
    monkeypatch.setattr(mgba_live, "print_json", lambda payload: captured.update(payload))

    mgba_live.cmd_screenshot(
        argparse.Namespace(
            session="session-1",
            out=None,
            no_save=True,
            timeout=7.0,
        )
    )

    assert captured == {"frame": 77, "png_base64": base64.b64encode(png_bytes).decode()}
    assert len(written_paths) == 1
    assert not written_paths[0].exists()


def test_cmd_screenshot_rejects_out_with_no_save(monkeypatch: Any) -> None:
    monkeypatch.setattr(mgba_live, "resolve_session", lambda *_args, **_kwargs: {"id": "session-1"})

    with pytest.raises(SystemExit, match="either --out or --no-save"):
        mgba_live.cmd_screenshot(
            argparse.Namespace(
                session="session-1",
                out="/tmp/out.png",
                no_save=True,
                timeout=5.0,
            )
        )


def test_build_start_command_includes_user_script_and_bridge_script() -> None:
    rom = rom_fixture_path()
    startup_script = Path(__file__).resolve()
    cmd = mgba_live.build_start_command(
        mgba_path="/usr/local/bin/mgba-qt",
        fps_target=120.0,
        config_overrides=["audioSync=0"],
        savestate=None,
        startup_scripts=[str(startup_script)],
        log_level=0,
        rom=rom,
    )

    script_values = [cmd[index + 1] for index, value in enumerate(cmd) if value == "--script"]
    assert script_values == [str(startup_script), str(mgba_live.BRIDGE_SCRIPT)]
    assert cmd[-1] == str(rom)


def test_cmd_start_passes_startup_script_to_mgba_process(tmp_path: Path, monkeypatch: Any) -> None:
    rom = rom_fixture_path()
    startup_script = tmp_path / "startup.lua"
    startup_script.write_text("-- startup script\n")

    runtime_root = tmp_path / ".runtime"
    monkeypatch.setattr(mgba_live, "RUNTIME_ROOT", runtime_root)
    monkeypatch.setattr(mgba_live, "SESSIONS_DIR", runtime_root / "sessions")
    monkeypatch.setattr(mgba_live, "ARCHIVED_SESSIONS_DIR", runtime_root / "archived_sessions")
    monkeypatch.setattr(mgba_live, "ACTIVE_SESSION_FILE", runtime_root / "active_session")

    captured: dict[str, Any] = {}

    class DummyProcess:
        pid = 4321

    def fake_popen(
        cmd: list[str],
        cwd: str,
        env: dict[str, str],
        stdout: Any,
        stderr: Any,
        start_new_session: bool,
    ) -> DummyProcess:
        captured["cmd"] = cmd
        captured["cwd"] = cwd
        captured["env"] = env
        captured["start_new_session"] = start_new_session
        return DummyProcess()

    monkeypatch.setattr(mgba_live.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(mgba_live, "pid_alive", lambda _pid: True)
    monkeypatch.setattr(mgba_live, "send_command", lambda *_args, **_kwargs: {"ok": True})

    output: dict[str, Any] = {}
    monkeypatch.setattr(mgba_live, "print_json", lambda payload: output.update(payload))

    args = argparse.Namespace(
        rom=str(rom),
        savestate=None,
        fps_target=None,
        fast=False,
        mgba_path="/usr/local/bin/mgba-qt",
        session_id="test-session",
        script=[str(startup_script)],
        log_level=0,
        heartbeat_interval=30,
        ready_timeout=1.0,
        config=[],
    )

    mgba_live.cmd_start(args)

    cmd = captured["cmd"]
    script_values = [cmd[index + 1] for index, value in enumerate(cmd) if value == "--script"]
    expected_bridge = (
        runtime_root / "sessions" / "test-session" / "scripts" / "mgba_live_bridge.lua"
    )
    assert script_values == [str(startup_script.resolve()), str(expected_bridge)]
    assert cmd[-1] == str(rom)
    assert output["status"] == "started"
    assert expected_bridge.exists()
    assert expected_bridge.read_text() == mgba_live.BRIDGE_SCRIPT.read_text()

    session_json = json.loads(
        (runtime_root / "sessions" / "test-session" / "session.json").read_text()
    )
    assert session_json["startup_scripts"] == [str(startup_script.resolve())]


def test_list_tools_exposes_start_with_lua_and_hides_start_script() -> None:
    tools = asyncio.run(mcp_server.list_tools())
    by_name = {tool.name: tool for tool in tools}

    assert "mgba_live_start_with_lua" in by_name
    start_props = by_name["mgba_live_start"].inputSchema["properties"]
    assert "script" not in start_props
    start_with_lua_props = by_name["mgba_live_start_with_lua"].inputSchema["properties"]
    assert "file" in start_with_lua_props
    assert "code" in start_with_lua_props


def test_mcp_start_tool_rejects_script_flag() -> None:
    rom = rom_fixture_path()

    with pytest.raises(ValueError, match="mgba_live_start_with_lua"):
        asyncio.run(
            mcp_server.call_tool(
                "mgba_live_start",
                {
                    "rom": str(rom),
                    "script": "/tmp/custom-startup.lua",
                },
            )
        )


def test_mcp_start_tool_forwards_core_start_args(monkeypatch: Any) -> None:
    rom = rom_fixture_path()
    captured: dict[str, Any] = {}

    async def fake_run_with_snapshot(
        live_command: str,
        command_args: list[str],
        *,
        timeout: float,
        **_: Any,
    ) -> list[Any]:
        captured["live_command"] = live_command
        captured["command_args"] = command_args
        captured["timeout"] = timeout
        return []

    monkeypatch.setattr(mcp_server, "_run_with_snapshot", fake_run_with_snapshot)
    asyncio.run(
        mcp_server.call_tool(
            "mgba_live_start",
            {
                "rom": str(rom),
                "savestate": "/tmp/custom.sav",
                "fps_target": 240,
                "session_id": "custom-session",
                "mgba_path": "/usr/local/bin/mgba-qt",
                "timeout": 9.0,
            },
        )
    )

    assert captured["live_command"] == "start"
    assert captured["command_args"][:2] == ["--rom", str(rom)]
    assert "--script" not in captured["command_args"]
    assert captured["command_args"] == [
        "--rom",
        str(rom),
        "--savestate",
        "/tmp/custom.sav",
        "--fps-target",
        "240.0",
        "--session-id",
        "custom-session",
        "--mgba-path",
        "/usr/local/bin/mgba-qt",
    ]
    assert captured["timeout"] == 9.0


class _StartWithLuaController:
    def __init__(self, *, fail_lua: bool = False) -> None:
        self.fail_lua = fail_lua
        self.calls: list[dict[str, Any]] = []

    async def run(self, command: str, args: list[str], *, timeout: float = 20.0) -> _Result:
        self.calls.append({"command": command, "args": list(args), "timeout": timeout})
        if command == "start":
            return _Result({"status": "started", "session_id": "session-123", "pid": 4321})
        if command == "run-lua":
            if self.fail_lua and args[:2] != ["--code", "return true"]:
                raise RuntimeError("run-lua failed")
            if args[:2] == ["--code", "return true"]:
                return _Result({"frame": 101, "data": {"settled": True}})
            return _Result({"frame": 100, "data": {"result": {"ok": True}}})
        if command == "screenshot":
            return _Result({"frame": 102, "png_base64": "AA=="})
        raise AssertionError(f"unexpected command: {command}")


def test_mcp_start_with_lua_file_mode_runs_start_lua_and_screenshot(monkeypatch: Any) -> None:
    rom = rom_fixture_path()
    fake = _StartWithLuaController()
    monkeypatch.setattr(mcp_server, "_controller", fake)

    contents = asyncio.run(
        mcp_server.call_tool(
            "mgba_live_start_with_lua",
            {
                "rom": str(rom),
                "file": "/tmp/startup.lua",
                "timeout": 7.0,
            },
        )
    )
    payload = _first_payload(contents)

    assert payload["session_id"] == "session-123"
    assert payload["pid"] == 4321
    assert payload["lua"] == {"ok": True}
    assert payload["screenshot"] == {"frame": 102}
    assert [call["command"] for call in fake.calls] == ["start", "run-lua", "run-lua", "screenshot"]
    assert fake.calls[1]["args"] == [
        "--file",
        "/tmp/startup.lua",
        "--session",
        "session-123",
        "--timeout",
        "7",
    ]
    assert fake.calls[2]["args"] == [
        "--code",
        "return true",
        "--session",
        "session-123",
        "--timeout",
        "20",
    ]
    assert fake.calls[3]["args"] == ["--session", "session-123", "--no-save", "--timeout", "20"]


def test_mcp_start_with_lua_code_mode_runs_start_lua_and_screenshot(monkeypatch: Any) -> None:
    rom = rom_fixture_path()
    fake = _StartWithLuaController()
    monkeypatch.setattr(mcp_server, "_controller", fake)

    contents = asyncio.run(
        mcp_server.call_tool(
            "mgba_live_start_with_lua",
            {
                "rom": str(rom),
                "code": "return 77",
                "timeout": 7.0,
            },
        )
    )
    payload = _first_payload(contents)

    assert payload["session_id"] == "session-123"
    assert payload["lua"] == {"ok": True}
    assert payload["screenshot"] == {"frame": 102}
    assert [call["command"] for call in fake.calls] == ["start", "run-lua", "run-lua", "screenshot"]
    assert fake.calls[1]["args"] == [
        "--code",
        "return 77",
        "--session",
        "session-123",
        "--timeout",
        "7",
    ]


def test_mcp_start_with_lua_leaves_session_running_when_lua_step_fails(monkeypatch: Any) -> None:
    rom = rom_fixture_path()
    fake = _StartWithLuaController(fail_lua=True)
    monkeypatch.setattr(mcp_server, "_controller", fake)

    with pytest.raises(RuntimeError, match="session-123"):
        asyncio.run(
            mcp_server.call_tool(
                "mgba_live_start_with_lua",
                {
                    "rom": str(rom),
                    "code": "return 1",
                    "timeout": 7.0,
                },
            )
        )
    assert [call["command"] for call in fake.calls] == ["start", "run-lua"]


def _write_session_fixture(runtime_root: Path, session_id: str, pid: int) -> None:
    session_dir = runtime_root / "sessions" / session_id
    session_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "id": session_id,
        "pid": pid,
        "rom": "/tmp/test.gb",
        "fps_target": 120.0,
        "session_dir": str(session_dir),
        "command_path": str(session_dir / "command.lua"),
        "response_path": str(session_dir / "response.json"),
        "heartbeat_path": str(session_dir / "heartbeat.json"),
        "stdout_log": str(session_dir / "stdout.log"),
        "stderr_log": str(session_dir / "stderr.log"),
    }
    (session_dir / "session.json").write_text(json.dumps(payload))


def test_cmd_status_all_prunes_dead_sessions(tmp_path: Path, monkeypatch: Any) -> None:
    runtime_root = tmp_path / ".runtime"
    monkeypatch.setattr(mgba_live, "RUNTIME_ROOT", runtime_root)
    monkeypatch.setattr(mgba_live, "SESSIONS_DIR", runtime_root / "sessions")
    monkeypatch.setattr(mgba_live, "ARCHIVED_SESSIONS_DIR", runtime_root / "archived_sessions")
    monkeypatch.setattr(mgba_live, "ACTIVE_SESSION_FILE", runtime_root / "active_session")

    _write_session_fixture(runtime_root, "dead-session", 1001)
    _write_session_fixture(runtime_root, "alive-session", 1002)

    monkeypatch.setattr(mgba_live, "pid_alive", lambda pid: pid == 1002)

    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        mgba_live, "print_json", lambda payload: captured.setdefault("value", payload)
    )

    mgba_live.cmd_status(argparse.Namespace(all=True, session=None))

    sessions = captured["value"]
    assert isinstance(sessions, list)
    assert len(sessions) == 1
    assert sessions[0]["session_id"] == "alive-session"
    assert sessions[0]["alive"] is True


def test_cmd_status_skips_dead_active_session(tmp_path: Path, monkeypatch: Any) -> None:
    runtime_root = tmp_path / ".runtime"
    monkeypatch.setattr(mgba_live, "RUNTIME_ROOT", runtime_root)
    monkeypatch.setattr(mgba_live, "SESSIONS_DIR", runtime_root / "sessions")
    monkeypatch.setattr(mgba_live, "ARCHIVED_SESSIONS_DIR", runtime_root / "archived_sessions")
    monkeypatch.setattr(mgba_live, "ACTIVE_SESSION_FILE", runtime_root / "active_session")

    _write_session_fixture(runtime_root, "dead-session", 2001)
    _write_session_fixture(runtime_root, "alive-session", 2002)
    mgba_live.ACTIVE_SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
    mgba_live.ACTIVE_SESSION_FILE.write_text("dead-session")

    monkeypatch.setattr(mgba_live, "pid_alive", lambda pid: pid == 2002)

    captured: dict[str, Any] = {}
    monkeypatch.setattr(mgba_live, "print_json", lambda payload: captured.update(payload))

    with pytest.raises(SystemExit, match="No session specified"):
        mgba_live.cmd_status(argparse.Namespace(all=False, session=None))
    assert captured == {}
    assert mgba_live.ACTIVE_SESSION_FILE.read_text() == "dead-session"


def test_prune_dead_sessions_archives_dead_session_directory(
    tmp_path: Path, monkeypatch: Any
) -> None:
    runtime_root = tmp_path / ".runtime"
    monkeypatch.setattr(mgba_live, "RUNTIME_ROOT", runtime_root)
    monkeypatch.setattr(mgba_live, "SESSIONS_DIR", runtime_root / "sessions")
    monkeypatch.setattr(mgba_live, "ARCHIVED_SESSIONS_DIR", runtime_root / "archived_sessions")
    monkeypatch.setattr(mgba_live, "ACTIVE_SESSION_FILE", runtime_root / "active_session")

    _write_session_fixture(runtime_root, "dead-session", 3001)
    _write_session_fixture(runtime_root, "alive-session", 3002)
    mgba_live.ACTIVE_SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
    mgba_live.ACTIVE_SESSION_FILE.write_text("dead-session")

    monkeypatch.setattr(mgba_live, "pid_alive", lambda pid: pid == 3002)

    removed = mgba_live.prune_dead_sessions()

    assert removed == ["dead-session"]
    assert not (runtime_root / "sessions" / "dead-session").exists()
    assert (runtime_root / "sessions" / "alive-session").exists()
    archived = sorted((runtime_root / "archived_sessions").glob("dead-session-*"))
    assert len(archived) == 1
    assert (archived[0] / "session.json").exists()
    assert not mgba_live.ACTIVE_SESSION_FILE.exists()


def test_prune_dead_sessions_clears_stale_active_pointer_when_no_sessions(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    runtime_root = tmp_path / ".runtime"
    monkeypatch.setattr(mgba_live, "RUNTIME_ROOT", runtime_root)
    monkeypatch.setattr(mgba_live, "SESSIONS_DIR", runtime_root / "sessions")
    monkeypatch.setattr(mgba_live, "ARCHIVED_SESSIONS_DIR", runtime_root / "archived_sessions")
    monkeypatch.setattr(mgba_live, "ACTIVE_SESSION_FILE", runtime_root / "active_session")

    mgba_live.ACTIVE_SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
    mgba_live.ACTIVE_SESSION_FILE.write_text("missing-session")
    monkeypatch.setattr(mgba_live, "pid_alive", lambda _pid: False)

    removed = mgba_live.prune_dead_sessions()

    assert removed == []
    assert not mgba_live.ACTIVE_SESSION_FILE.exists()


def test_main_prunes_sessions_before_dispatch(monkeypatch: Any) -> None:
    calls: list[str] = []

    class DummyParser:
        def parse_args(self) -> argparse.Namespace:
            return argparse.Namespace(func=lambda _args: calls.append("dispatch"))

    monkeypatch.setattr(mgba_live, "build_parser", lambda: DummyParser())
    monkeypatch.setattr(mgba_live, "ensure_runtime_dirs", lambda: calls.append("ensure"))
    monkeypatch.setattr(mgba_live, "prune_dead_sessions", lambda: calls.append("prune"))

    mgba_live.main()

    assert calls == ["ensure", "prune", "dispatch"]


def test_runtime_root_defaults_to_home_directory() -> None:
    assert mgba_live.RUNTIME_ROOT == Path.home() / ".mgba-live-mcp" / "runtime"
