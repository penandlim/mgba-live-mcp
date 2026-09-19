from __future__ import annotations

import asyncio
from typing import Any

import pytest
from mcp import types

from mgba_live_mcp import server
from mgba_live_mcp.errors import DomainError
from mgba_live_mcp.live_cli import build_parser
from mgba_live_mcp.session_manager import SessionManager


def invoke(name: str, arguments: dict[str, Any]) -> types.CallToolResult:
    request = types.CallToolRequest(
        method="tools/call", params=types.CallToolRequestParams(name=name, arguments=arguments)
    )

    async def registered():
        return (await server.server.request_handlers[types.CallToolRequest](request)).root

    result = asyncio.run(registered())
    assert isinstance(result, types.CallToolResult)
    return result


def test_cli_pointer_width_is_strictly_bounded() -> None:
    parser = build_parser()
    for width in range(1, 7):
        args = parser.parse_args(
            [
                "dump-pointers",
                "--session",
                "s",
                "--start",
                "0",
                "--count",
                "1",
                "--width",
                str(width),
            ]
        )
        assert args.width == width
    for width in ("0", "7", "8", "1.0", "true"):
        with pytest.raises(SystemExit):
            parser.parse_args(
                [
                    "dump-pointers",
                    "--session",
                    "s",
                    "--start",
                    "0",
                    "--count",
                    "1",
                    "--width",
                    width,
                ]
            )


@pytest.mark.parametrize("width", [0, 7, 4.0, True])
def test_manager_rejects_invalid_width_before_session_lookup(tmp_path, width) -> None:
    manager = SessionManager(runtime_root=tmp_path)
    with pytest.raises(DomainError) as error:
        manager.dump_pointers(session="missing", start=0, count=1, width=width)
    assert error.value.code == "invalid_arguments"
    assert error.value.execution_outcome == "not_executed"


def test_registered_mcp_rejects_invalid_width_before_dispatch() -> None:
    for width in (0, 7, 8, 4.0, True):
        result = invoke(
            "mgba_live_dump_pointers",
            {"session": "missing", "start": 0, "count": 1, "width": width},
        )
        assert result.isError
        assert result.structuredContent is not None
        assert result.structuredContent["error"]["code"] == "invalid_arguments"
