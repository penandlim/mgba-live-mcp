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


def test_generated_reference_is_current() -> None:
    generator = _load_generate_mcp_reference()
    tools = asyncio.run(mcp_server.list_tools())
    expected = generator._render_markdown(tools)
    reference = Path(__file__).resolve().parents[1] / "docs" / "mcp-reference.md"
    assert reference.read_text() == expected
