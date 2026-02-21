from __future__ import annotations

from importlib.resources import files


def test_packaged_bridge_resource_exists() -> None:
    bridge = files("mgba_live_mcp").joinpath("resources/mgba_live_bridge.lua")

    assert bridge.is_file()
    content = bridge.read_text(encoding="utf-8")
    assert "MGBA_LIVE_SESSION_DIR" in content
