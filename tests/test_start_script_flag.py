from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


def load_mgba_live_module() -> Any:
    return importlib.import_module("mgba_live_mcp.live_cli")


mgba_live = load_mgba_live_module()


def rom_fixture_path() -> Path:
    synthetic_rom = REPO_ROOT / "tests" / "fixtures" / "synthetic.gb"
    assert synthetic_rom.exists()
    return synthetic_rom.resolve()


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
