from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
from importlib.resources import as_file, files
from pathlib import Path

import pytest
from mcp import types

from mgba_live_mcp import server
from mgba_live_mcp.live_controller import LiveControllerClient
from mgba_live_mcp.session_manager import SessionManager

# Only the emulator host API is substituted. Commands and responses pass through
# the unmodified packaged bridge and the real Lua interpreter/serializer.
HOST = """
C = { GBA_KEY = { A=0, B=1, SELECT=2, START=3, RIGHT=4, LEFT=5, UP=6, DOWN=7, R=8, L=9 } }
emu = { getKeys = function() return 0 end }
callbacks = { add = function(self, event, fn) frame_callback = fn end }
dofile(arg[1])
frame_callback()
"""


@pytest.mark.parametrize("source", ["code", "file"])
@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("return nil", None),
        ("return false", False),
        ("return 0", 0),
        ('return ""', ""),
        ("return {}", []),
        ('return {0, false, ""}', [0, False, ""]),
        (
            "local shared={value=false}; return {left=shared, right=shared}",
            {"left": {"value": False}, "right": {"value": False}},
        ),
    ],
)
def test_packaged_lua_results_reach_mcp(tmp_path, monkeypatch, source, code, expected):
    lua = shutil.which("lua") or shutil.which("lua5.4") or shutil.which("luajit")
    if lua is None:
        pytest.skip("Install Lua to exercise the packaged bridge (CI installs lua5.4).")
    manager = SessionManager(runtime_root=tmp_path)
    manager.ensure_runtime_dirs()
    manager.session_dir("running").mkdir()
    host = tmp_path / "host.lua"
    host.write_text(HOST)
    script = tmp_path / "user.lua"
    script.write_text(code)
    original_write = manager.write_command
    bridge = files("mgba_live_mcp").joinpath("resources/mgba_live_bridge.lua")

    def target(session, **kwargs):
        directory = manager.session_dir(session)
        return {
            "id": session,
            "command_path": str(directory / "command.lua"),
            "response_path": str(directory / "response.json"),
        }

    with as_file(bridge) as bridge_path:

        def publish(path: Path, command, **kwargs):
            original_write(path, command, **kwargs)
            subprocess.run(
                [lua, str(host), str(bridge_path)],
                cwd=path.parent,
                env={**os.environ, "MGBA_LIVE_SESSION_DIR": str(path.parent)},
                check=True,
                capture_output=True,
                text=True,
                timeout=5,
            )

        monkeypatch.setattr(manager, "write_command", publish)
        monkeypatch.setattr(manager, "require_session", target)
        monkeypatch.setattr(
            manager, "start", lambda **k: {"session_id": k["session_id"], "pid": 42}
        )
        monkeypatch.setattr(server, "_controller", LiveControllerClient(manager))

        async def invoke(name, arguments):
            request = types.CallToolRequest(
                method="tools/call",
                params=types.CallToolRequestParams(name=name, arguments=arguments),
            )
            return (await server.server.request_handlers[types.CallToolRequest](request)).root

        for startup in (False, True):
            arguments = {source: str(script) if source == "file" else code}
            if startup:
                arguments.update(
                    rom=str(Path(__file__).parent / "fixtures" / "synthetic.gb"),
                    mgba_path=sys.executable,
                    session_id="boot",
                )
            else:
                arguments["session"] = "running"
            name = "mgba_live_start_with_lua" if startup else "mgba_live_run_lua"
            result = asyncio.run(invoke(name, arguments))
            assert isinstance(result, types.CallToolResult)
            assert not result.isError, result.structuredContent
            payload = result.structuredContent
            assert payload is not None
            actual = payload["lua"] if startup else payload["data"]["result"]
            assert actual == expected and type(actual) is type(expected)
            first = result.content[0]
            assert isinstance(first, types.TextContent)
            assert json.loads(first.text) == payload
