from __future__ import annotations

import asyncio
import importlib
import json
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
    assert synthetic_rom.exists()
    return synthetic_rom.resolve()


def _first_payload(contents: Any) -> dict[str, Any]:
    assert contents
    first = contents[0]
    assert getattr(first, "type", None) == "text"
    return json.loads(first.text)


class _ServerController:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def start(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("start", dict(kwargs)))
        return {
            "status": "started",
            "session_id": kwargs.get("session_id") or "session-123",
            "pid": 4321,
            "fps_target": 120.0,
            "session_dir": "/tmp/session-123",
        }

    async def start_with_lua(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("start_with_lua", dict(kwargs)))
        return {
            "session_id": kwargs.get("session_id") or "session-123",
            "pid": 4321,
            "lua": {"ok": True},
        }

    async def start_with_lua_and_view(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("start_with_lua_and_view", dict(kwargs)))
        return {
            "session_id": kwargs.get("session_id") or "session-123",
            "pid": 4321,
            "lua": {"ok": True},
            "screenshot": {"frame": 102},
            "png_base64": "AA==",
        }


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


def test_cmd_status_delegates_to_manager(monkeypatch: Any) -> None:
    class _Manager:
        def status(
            self, *, session: str | None = None, all: bool = False
        ) -> dict[str, Any] | list[dict[str, Any]]:
            if all:
                return [{"session_id": "session-a"}]
            return {"session_id": session, "alive": True}

    captured: list[Any] = []
    monkeypatch.setattr(mgba_live, "_manager", lambda: _Manager())
    monkeypatch.setattr(mgba_live, "print_json", lambda payload: captured.append(payload))

    mgba_live.cmd_status(type("Args", (), {"all": True, "session": None})())
    mgba_live.cmd_status(type("Args", (), {"all": False, "session": "session-1"})())

    assert captured == [[{"session_id": "session-a"}], {"session_id": "session-1", "alive": True}]


def test_list_tools_exposes_start_with_lua_variants() -> None:
    tools = asyncio.run(mcp_server.list_tools())
    by_name = {tool.name: tool for tool in tools}

    assert "mgba_live_start_with_lua" in by_name
    assert "mgba_live_start_with_lua_and_view" in by_name
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
                {"rom": str(rom), "script": "/tmp/custom-startup.lua"},
            )
        )


def test_mcp_start_tool_forwards_core_start_args(monkeypatch: Any) -> None:
    rom = rom_fixture_path()
    fake = _ServerController()
    monkeypatch.setattr(mcp_server, "_controller", fake)

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

    assert fake.calls == [
        (
            "start",
            {
                "timeout": 9.0,
                "rom": str(rom),
                "savestate": "/tmp/custom.sav",
                "fps_target": 240.0,
                "session_id": "custom-session",
                "mgba_path": "/usr/local/bin/mgba-qt",
            },
        )
    ]


def test_mcp_start_with_lua_is_metadata_only(monkeypatch: Any) -> None:
    rom = rom_fixture_path()
    fake = _ServerController()
    monkeypatch.setattr(mcp_server, "_controller", fake)

    contents = asyncio.run(
        mcp_server.call_tool(
            "mgba_live_start_with_lua",
            {"rom": str(rom), "code": "return 77", "timeout": 7.0},
        )
    )
    payload = _first_payload(contents)

    assert payload == {"session_id": "session-123", "pid": 4321, "lua": {"ok": True}}
    assert len(contents) == 1
    assert fake.calls == [
        ("start_with_lua", {"timeout": 7.0, "rom": str(rom), "code": "return 77"})
    ]


def test_mcp_start_with_lua_and_view_returns_image(monkeypatch: Any) -> None:
    rom = rom_fixture_path()
    fake = _ServerController()
    monkeypatch.setattr(mcp_server, "_controller", fake)

    contents = asyncio.run(
        mcp_server.call_tool(
            "mgba_live_start_with_lua_and_view",
            {"rom": str(rom), "file": "/tmp/startup.lua", "timeout": 7.0},
        )
    )
    payload = _first_payload(contents)

    assert payload["session_id"] == "session-123"
    assert payload["lua"] == {"ok": True}
    assert payload["screenshot"] == {"frame": 102}
    assert len(contents) == 2
