from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
from types import ModuleType

from mgba_live_mcp import server as mcp_server


def _load_generate_mcp_reference() -> ModuleType:
    script = Path(__file__).resolve().parents[1] / "scripts" / "generate_mcp_reference.py"
    spec = importlib.util.spec_from_file_location("generate_mcp_reference", script)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_reference_uses_server_runtime_argument_rules() -> None:
    generate_mcp_reference = _load_generate_mcp_reference()
    assert not hasattr(generate_mcp_reference, "RUNTIME_ARGUMENT_RULES")

    tools = asyncio.run(mcp_server.list_tools())
    run_lua = next(tool for tool in tools if tool.name == "mgba_live_run_lua")

    rendered = generate_mcp_reference._render_tool_section(run_lua)

    runtime_rule = mcp_server.TOOL_RUNTIME_ARGUMENT_RULES[run_lua.name]
    assert f"- Runtime argument rule: {runtime_rule}" in rendered
