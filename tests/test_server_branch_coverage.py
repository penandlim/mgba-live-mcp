from __future__ import annotations

import asyncio
import base64
from typing import Any, cast

import pytest
from mcp.types import ImageContent, TextContent

import mgba_live_mcp.server as server


def test_text_payload_validation_errors() -> None:
    image = ImageContent(type="image", data="AA==", mimeType="image/png")
    with pytest.raises(RuntimeError, match="Expected text payload"):
        server._text_payload(image)

    bad_text: Any = type("BadText", (), {"type": "text", "text": None})()
    with pytest.raises(RuntimeError, match="missing JSON"):
        server._text_payload(cast(TextContent, bad_text))

    invalid_json = TextContent(type="text", text="{")
    with pytest.raises(RuntimeError, match="parse JSON"):
        server._text_payload(invalid_json)

    non_object = TextContent(type="text", text='["x"]')
    with pytest.raises(RuntimeError, match="must be an object"):
        server._text_payload(non_object)


def test_image_helper_edges(tmp_path) -> None:
    assert server._image_bytes_from_screenshot({"png_base64": "***"}) is None
    assert server._image_bytes_from_screenshot({"path": str(tmp_path / "missing.png")}) is None

    png_path = tmp_path / "ok.png"
    png_path.write_bytes(b"png")
    kind, raw = server._image_bytes_from_screenshot({"path": str(png_path)}) or ("", b"")
    assert kind == "png"
    assert raw == b"png"

    contents = server._contents_from_payload(
        {"session_id": "s1", "png_base64": "AA=="}, include_image=True
    )
    assert len(contents) == 2


def test_argument_validation_helpers() -> None:
    with pytest.raises(ValueError, match="session_required"):
        server._require_session({})

    assert server._parse_wait_frames({}) == 0
    assert server._parse_wait_frames({"wait_frames": 2.0}) == 2
    with pytest.raises(ValueError, match="non-negative integer"):
        server._parse_wait_frames({"wait_frames": True})
    with pytest.raises(ValueError, match="non-negative integer"):
        server._parse_wait_frames({"wait_frames": 1.5})
    with pytest.raises(ValueError, match=">= 0"):
        server._parse_wait_frames({"wait_frames": -1})

    with pytest.raises(ValueError, match="Exactly one of file or code"):
        server._lua_source_kwargs({})
    with pytest.raises(ValueError, match="Exactly one of file or code"):
        server._lua_source_kwargs({"file": "a.lua", "code": "return true"})
    assert server._lua_source_kwargs({"code": "return true"}) == {"code": "return true"}

    with pytest.raises(ValueError, match="rom is required"):
        server._build_start_kwargs({})
    assert server._build_start_kwargs({"rom": "x.gba", "fast": True}) == {
        "rom": "x.gba",
        "fast": True,
    }


@pytest.mark.anyio
async def test_call_tool_validation_branches() -> None:
    with pytest.raises(ValueError, match="mgba_live_start_with_lua"):
        await server.call_tool("mgba_live_start", {"rom": "x.gba", "script": "boot.lua"})

    with pytest.raises(ValueError, match="session_required"):
        await server.call_tool("mgba_live_attach", {})

    with pytest.raises(ValueError, match="session_required"):
        await server.call_tool("mgba_live_read_memory", {"addresses": [1]})

    with pytest.raises(ValueError, match="key is required"):
        await server.call_tool("mgba_live_input_tap", {"session": "s1"})


def test_unknown_tool_response() -> None:
    result = asyncio.run(server.call_tool("unknown", {}))
    assert len(result) == 1
    assert getattr(result[0], "type", None) == "text"


def test_export_visual_payload_strips_base64() -> None:
    payload = {"session_id": "s1", "png_base64": base64.b64encode(b"png").decode()}
    public = server._public_visual_payload(payload)
    assert public == {"session_id": "s1"}
