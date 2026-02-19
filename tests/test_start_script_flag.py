from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
from pathlib import Path
from types import ModuleType
from typing import Any

from mgba_live_mcp import server as mcp_server


REPO_ROOT = Path(__file__).resolve().parents[1]


def load_mgba_live_module() -> ModuleType:
    module_path = REPO_ROOT / "scripts" / "mgba_live.py"
    spec = importlib.util.spec_from_file_location("mgba_live_script", module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mgba_live = load_mgba_live_module()


def pokemon_red_rom_path() -> Path:
    roms_dir = REPO_ROOT / "roms"
    matches = sorted(roms_dir.glob("*Red Version.gb"))
    assert matches, f"Pokemon Red ROM fixture not found in {roms_dir}"
    return matches[0].resolve()


def test_start_parser_accepts_script_flag_with_known_pokemon_rom() -> None:
    rom = pokemon_red_rom_path()
    parser = mgba_live.build_parser()
    args = parser.parse_args(["start", "--rom", str(rom), "--script", "boot.lua", "--script", "hud.lua"])
    assert args.script == ["boot.lua", "hud.lua"]


def test_build_start_command_includes_user_script_and_bridge_script() -> None:
    rom = pokemon_red_rom_path()
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
    rom = pokemon_red_rom_path()
    startup_script = tmp_path / "startup.lua"
    startup_script.write_text("-- startup script\n")

    runtime_root = tmp_path / ".runtime"
    monkeypatch.setattr(mgba_live, "RUNTIME_ROOT", runtime_root)
    monkeypatch.setattr(mgba_live, "SESSIONS_DIR", runtime_root / "sessions")
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
    assert script_values == [str(startup_script.resolve()), str(mgba_live.BRIDGE_SCRIPT)]
    assert cmd[-1] == str(rom)
    assert output["status"] == "started"

    session_json = json.loads((runtime_root / "sessions" / "test-session" / "session.json").read_text())
    assert session_json["startup_scripts"] == [str(startup_script.resolve())]


def test_mcp_start_tool_forwards_script_flag() -> None:
    rom = pokemon_red_rom_path()
    captured: dict[str, Any] = {}

    async def fake_run_with_snapshot(
        tool_name: str,
        live_command: str,
        command_args: list[str],
        *,
        timeout: float,
        **_: Any,
    ) -> list[Any]:
        captured["tool_name"] = tool_name
        captured["live_command"] = live_command
        captured["command_args"] = command_args
        captured["timeout"] = timeout
        return []

    original = mcp_server._run_with_snapshot
    mcp_server._run_with_snapshot = fake_run_with_snapshot
    try:
        asyncio.run(
            mcp_server.call_tool(
                "mgba_live_start",
                {
                    "rom": str(rom),
                    "script": "/tmp/custom-startup.lua",
                    "timeout": 9.0,
                },
            )
        )
    finally:
        mcp_server._run_with_snapshot = original

    assert captured["tool_name"] == "mgba_live_start"
    assert captured["live_command"] == "start"
    assert captured["command_args"][:2] == ["--rom", str(rom)]
    assert "--script" in captured["command_args"]
    assert captured["command_args"][captured["command_args"].index("--script") + 1] == "/tmp/custom-startup.lua"
