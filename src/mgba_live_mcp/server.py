"""MCP server for persistent live mGBA control with live screenshot exports."""

from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import ImageContent, TextContent, Tool

from .live_controller import LiveControllerClient

server = Server("mgba-live-mcp")
_controller = LiveControllerClient()


def _text_content(payload: Any) -> TextContent:
    return TextContent(type="text", text=json.dumps(payload, separators=(",", ":")))


def _text_payload(content: TextContent | ImageContent) -> dict[str, Any]:
    if getattr(content, "type", None) != "text":
        raise RuntimeError("Expected text payload in tool response.")

    text_value = getattr(content, "text", None)
    if not isinstance(text_value, str):
        raise RuntimeError("Text payload is missing JSON content.")

    try:
        payload = json.loads(text_value)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Failed to parse JSON tool payload.") from exc

    if not isinstance(payload, dict):
        raise RuntimeError("Tool payload JSON must be an object.")
    return payload


def _parse_args_list(value: list[str] | None) -> list[str]:
    if not value:
        return []
    return [str(v) for v in value]


def _image_bytes_from_screenshot(result: dict[str, Any]) -> tuple[str, bytes] | None:
    path = result.get("path")
    if isinstance(path, str) and path:
        try:
            return "png", Path(path).read_bytes()
        except OSError:
            return None
    return None


def _image_content(result: dict[str, Any]) -> ImageContent | None:
    parsed = _image_bytes_from_screenshot(result)
    if parsed is None:
        return None

    _, raw = parsed
    encoded = base64.b64encode(raw).decode()

    return ImageContent(type="image", data=encoded, mimeType="image/png")


def _extract_session_id(payload: Any) -> str | None:
    if isinstance(payload, dict):
        session_id = payload.get("session_id")
        if isinstance(session_id, str) and session_id:
            return session_id
        for key in ("sessions", "value"):
            nested = payload.get(key)
            nested_session = _extract_session_id(nested)
            if nested_session:
                return nested_session
        return None
    if isinstance(payload, list):
        for item in payload:
            nested_session = _extract_session_id(item)
            if nested_session:
                return nested_session
    return None


def _session_arg_value(command_args: list[str]) -> str | None:
    for index, value in enumerate(command_args):
        if value == "--session" and index + 1 < len(command_args):
            return str(command_args[index + 1])
    return None


async def _resolve_snapshot_session(
    command_args: list[str],
    command_payload: Any,
    *,
    timeout: float,
) -> str | None:
    session_from_args = _session_arg_value(command_args)
    if session_from_args:
        return session_from_args

    payload_session_id = _extract_session_id(command_payload)
    if payload_session_id:
        return payload_session_id

    # Some CLI responses (for example run-lua) do not include session id.
    # Fall back to status for active-session resolution before screenshot capture.
    try:
        status_result = await _controller.run("status", [], timeout=max(timeout, 20.0))
    except Exception:
        return None

    return _extract_session_id(status_result.payload)


def _extract_run_lua_result(command_payload: dict[str, Any]) -> Any:
    if not isinstance(command_payload, dict):
        return None
    data = command_payload.get("data")
    if not isinstance(data, dict):
        return None
    if "result" in data:
        return data.get("result")
    return data


def _extract_run_lua_macro_key(command_payload: dict[str, Any]) -> str | None:
    result = _extract_run_lua_result(command_payload)
    if not isinstance(result, dict):
        return None
    macro_key = result.get("macro_key")
    if isinstance(macro_key, str) and macro_key:
        return macro_key
    return None


def _extract_response_frame(command_payload: Any) -> int | None:
    if not isinstance(command_payload, dict):
        return None
    frame = command_payload.get("frame")
    if isinstance(frame, bool):
        return None
    if isinstance(frame, (int, float)):
        return int(frame)
    return None


def _extract_input_tap_duration(command_payload: Any) -> int | None:
    if not isinstance(command_payload, dict):
        return None
    data = command_payload.get("data")
    if not isinstance(data, dict):
        return None
    duration = data.get("duration")
    if isinstance(duration, bool):
        return None
    if isinstance(duration, (int, float)):
        value = int(duration)
        if value >= 1:
            return value
    return None


def _lua_quote(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("'", "\\'")
    return f"'{escaped}'"


async def _wait_for_macro_completion(
    *,
    session_id: str,
    macro_key: str,
    timeout: float,
    poll_seconds: float = 0.05,
) -> dict[str, Any]:
    settle_timeout = max(float(timeout), 0.0)
    poll_interval = max(float(poll_seconds), 0.01)
    loop = asyncio.get_running_loop()
    started_at = loop.time()
    deadline = started_at + settle_timeout
    polls = 0
    wait_code = (
        f"local macro = _G[{_lua_quote(macro_key)}]; "
        "if macro == nil then return true end "
        "local active = macro.active; "
        "if active == nil then return true end "
        "return active == false"
    )
    poll_command_timeout = max(1.0, min(5.0, settle_timeout if settle_timeout > 0 else 1.0))

    while True:
        polls += 1
        poll_result = await _controller.run(
            "run-lua",
            ["--code", wait_code, "--session", str(session_id)],
            timeout=poll_command_timeout,
        )
        result_value = _extract_run_lua_result(poll_result.payload)
        if result_value is True:
            return {
                "mode": "macro_key",
                "macro_key": macro_key,
                "completed": True,
                "polls": polls,
                "waited_seconds": round(loop.time() - started_at, 3),
            }

        if settle_timeout <= 0 or loop.time() >= deadline:
            return {
                "mode": "macro_key",
                "macro_key": macro_key,
                "completed": False,
                "polls": polls,
                "waited_seconds": round(loop.time() - started_at, 3),
            }

        await asyncio.sleep(poll_interval)


async def _wait_for_target_frame(
    *,
    session_id: str,
    target_frame: int,
    timeout: float,
    poll_seconds: float = 0.05,
) -> int:
    settle_timeout = max(float(timeout), 0.0)
    poll_interval = max(float(poll_seconds), 0.01)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + settle_timeout
    poll_command_timeout = max(1.0, min(5.0, settle_timeout if settle_timeout > 0 else 1.0))

    while True:
        poll_result = await _controller.run(
            "run-lua",
            ["--code", "return true", "--session", str(session_id)],
            timeout=poll_command_timeout,
        )
        current_frame = _extract_response_frame(poll_result.payload)
        if current_frame is None:
            raise RuntimeError("run-lua poll did not return a frame.")

        if current_frame >= target_frame:
            return current_frame

        if settle_timeout <= 0 or loop.time() >= deadline:
            raise TimeoutError(
                f"Timed out waiting for frame >= {target_frame}; last_frame={current_frame}"
            )

        await asyncio.sleep(poll_interval)


async def _run_with_snapshot(
    live_command: str,
    command_args: list[str],
    *,
    timeout: float,
    session_id: str | None = None,
    include_snapshot: bool = True,
    ensure_post_lua_settle: bool = False,
    require_snapshot_session: bool = False,
    require_screenshot: bool = False,
    input_tap_wait_frames: int | None = None,
) -> list[TextContent | ImageContent]:
    command_result = await _controller.run(live_command, command_args, timeout=timeout)
    payload: dict[str, Any]
    if isinstance(command_result.payload, dict):
        payload = dict(command_result.payload)
    else:
        payload = {"value": command_result.payload}
    image_contents: list[ImageContent] = []

    if not include_snapshot:
        return [_text_content(payload)]

    resolved_session = session_id or await _resolve_snapshot_session(
        command_args,
        command_result.payload,
        timeout=timeout,
    )
    if not resolved_session:
        if require_snapshot_session:
            requested_session = session_id or _session_arg_value(command_args) or "unknown"
            raise RuntimeError(
                f"Unable to resolve session_id for screenshot capture after '{live_command}' "
                f"(requested_session={requested_session})."
            )
        return [_text_content(payload)]

    if input_tap_wait_frames is not None:
        tap_frame = _extract_response_frame(command_result.payload)
        tap_duration = _extract_input_tap_duration(command_result.payload)
        if tap_frame is None or tap_duration is None:
            raise RuntimeError(
                f"Input-tap response missing frame/duration for session '{resolved_session}'."
            )
        target_frame = tap_frame + tap_duration + int(input_tap_wait_frames)
        try:
            await _wait_for_target_frame(
                session_id=str(resolved_session),
                target_frame=target_frame,
                timeout=max(timeout, 20.0),
            )
        except Exception as exc:
            raise RuntimeError(
                f"Post-tap wait failed for session '{resolved_session}' "
                f"(target_frame={target_frame}). Original error: {exc}"
            ) from exc

    if ensure_post_lua_settle:
        macro_key = _extract_run_lua_macro_key(command_result.payload)
        if macro_key:
            try:
                await _wait_for_macro_completion(
                    session_id=str(resolved_session),
                    macro_key=macro_key,
                    timeout=max(timeout, 20.0),
                )
            except Exception:
                pass
        else:
            # For run-lua calls, issue a no-op Lua command before screenshot capture
            # so the image reflects state after the Lua command has fully completed.
            try:
                await _controller.run(
                    "run-lua",
                    ["--code", "return true", "--session", str(resolved_session)],
                    timeout=max(timeout, 20.0),
                )
            except Exception:
                pass

    shot_args = ["--session", str(resolved_session)]
    try:
        shot_result = await _controller.run("screenshot", shot_args, timeout=max(timeout, 20.0))
        screenshot_payload = shot_result.payload
        payload["screenshot"] = screenshot_payload
        shot_image = (
            _image_content(screenshot_payload) if isinstance(screenshot_payload, dict) else None
        )
        if shot_image is not None:
            image_contents.append(shot_image)
    except Exception as exc:
        if require_screenshot:
            raise RuntimeError(
                f"Screenshot capture failed for session '{resolved_session}'. Original error: {exc}"
            ) from exc

    return [_text_content(payload), *image_contents]


def _build_session_arg(args: dict[str, Any]) -> list[str]:
    result = []
    if session := args.get("session"):
        result.extend(["--session", str(session)])
    return result


def _build_start_command_args(args: dict[str, Any]) -> list[str]:
    if "rom" not in args:
        raise ValueError("rom is required")

    cmd_args: list[str] = ["--rom", str(args["rom"])]
    if savestate := args.get("savestate"):
        cmd_args += ["--savestate", str(savestate)]
    if "fps_target" in args:
        fps_target = args["fps_target"]
        if fps_target is not None:
            cmd_args += ["--fps-target", str(float(fps_target))]
    if args.get("fast"):
        cmd_args.append("--fast")
    if session_id := args.get("session_id"):
        cmd_args.extend(["--session-id", str(session_id)])
    if mgba_path := args.get("mgba_path"):
        cmd_args += ["--mgba-path", str(mgba_path)]
    return cmd_args


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="mgba_live_start",
            description="Start a persistent live mGBA session.",
            inputSchema={
                "type": "object",
                "properties": {
                    "rom": {"type": "string", "description": "Path to ROM (.gba/.gb/.gbc)."},
                    "savestate": {"type": "string", "description": "Optional savestate path."},
                    "fps_target": {
                        "type": "number",
                        "description": "Explicit fpsTarget. Defaults to 120 when omitted.",
                    },
                    "fast": {"type": "boolean", "description": "Shortcut for fps_target=600."},
                    "session_id": {
                        "type": "string",
                        "description": "Optional explicit session id.",
                    },
                    "mgba_path": {"type": "string", "description": "Optional mgba-qt path."},
                    "timeout": {
                        "type": "number",
                        "description": "Command timeout in seconds.",
                        "default": 20.0,
                    },
                },
                "required": ["rom"],
            },
        ),
        Tool(
            name="mgba_live_start_with_lua",
            description=(
                "Start a live session, run Lua immediately, then return the post-Lua screenshot."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "rom": {"type": "string", "description": "Path to ROM (.gba/.gb/.gbc)."},
                    "savestate": {"type": "string", "description": "Optional savestate path."},
                    "fps_target": {
                        "type": "number",
                        "description": "Explicit fpsTarget. Defaults to 120 when omitted.",
                    },
                    "fast": {"type": "boolean", "description": "Shortcut for fps_target=600."},
                    "session_id": {
                        "type": "string",
                        "description": "Optional explicit session id.",
                    },
                    "mgba_path": {"type": "string", "description": "Optional mgba-qt path."},
                    "file": {"type": "string", "description": "Lua file path."},
                    "code": {"type": "string", "description": "Inline Lua code."},
                    "timeout": {
                        "type": "number",
                        "description": "Command timeout in seconds.",
                        "default": 20.0,
                    },
                },
                "required": ["rom"],
            },
        ),
        Tool(
            name="mgba_live_attach",
            description="Attach to an existing managed live session.",
            inputSchema={
                "type": "object",
                "properties": {
                    "session": {"type": "string", "description": "Session id."},
                    "pid": {"type": "integer", "description": "PID of a managed session."},
                    "timeout": {
                        "type": "number",
                        "description": "Command timeout in seconds.",
                        "default": 20.0,
                    },
                },
            },
        ),
        Tool(
            name="mgba_live_status",
            description="Show status for one session or all managed sessions.",
            inputSchema={
                "type": "object",
                "properties": {
                    "session": {"type": "string", "description": "Optional session id."},
                    "all": {"type": "boolean", "description": "Whether to include all sessions."},
                    "timeout": {
                        "type": "number",
                        "description": "Command timeout in seconds.",
                        "default": 20.0,
                    },
                },
            },
        ),
        Tool(
            name="mgba_live_stop",
            description="Stop one managed session.",
            inputSchema={
                "type": "object",
                "properties": {
                    "session": {"type": "string", "description": "Session id to stop."},
                    "grace": {"type": "number", "description": "Kill grace period in seconds."},
                    "timeout": {
                        "type": "number",
                        "description": "Command timeout in seconds.",
                        "default": 20.0,
                    },
                },
            },
        ),
        Tool(
            name="mgba_live_run_lua",
            description="Execute Lua in a running live session.",
            inputSchema={
                "type": "object",
                "properties": {
                    "session": {"type": "string", "description": "Optional session id."},
                    "file": {"type": "string", "description": "Lua file path."},
                    "code": {"type": "string", "description": "Inline Lua code."},
                    "timeout": {
                        "type": "number",
                        "description": "Command timeout in seconds.",
                        "default": 20.0,
                    },
                },
                "required": [],
            },
        ),
        Tool(
            name="mgba_live_input_tap",
            description=(
                "Tap a key for N frames, optionally wait additional frames "
                "after release, then return a screenshot."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "session": {"type": "string"},
                    "key": {
                        "type": "string",
                        "description": "A/B/START/SELECT/UP/DOWN/LEFT/RIGHT/L/R.",
                    },
                    "frames": {"type": "integer", "default": 1},
                    "wait_frames": {
                        "type": "integer",
                        "default": 0,
                        "minimum": 0,
                        "description": (
                            "Additional frames to wait after tap release before screenshot capture."
                        ),
                    },
                    "timeout": {
                        "type": "number",
                        "description": "Command timeout in seconds.",
                        "default": 20.0,
                    },
                },
                "required": ["key"],
            },
        ),
        Tool(
            name="mgba_live_input_set",
            description=(
                "Set currently held keys for live session. Use "
                "mgba_live_status after input for visual verification."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "session": {"type": "string"},
                    "keys": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Keys to hold.",
                    },
                    "timeout": {
                        "type": "number",
                        "description": "Command timeout in seconds.",
                        "default": 20.0,
                    },
                },
                "required": ["keys"],
            },
        ),
        Tool(
            name="mgba_live_input_clear",
            description=(
                "Clear held keys from live session. Use mgba_live_status "
                "after input for visual verification."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "session": {"type": "string"},
                    "keys": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional keys to clear; omit to clear all.",
                    },
                    "timeout": {
                        "type": "number",
                        "description": "Command timeout in seconds.",
                        "default": 20.0,
                    },
                },
            },
        ),
        Tool(
            name="mgba_live_export_screenshot",
            description="Export a screenshot from a live session.",
            inputSchema={
                "type": "object",
                "properties": {
                    "session": {"type": "string", "description": "Optional session id."},
                    "timeout": {
                        "type": "number",
                        "description": "Command timeout in seconds.",
                        "default": 20.0,
                    },
                    "out": {"type": "string", "description": "Optional persisted PNG output path."},
                },
                "required": [],
            },
        ),
        Tool(
            name="mgba_live_read_memory",
            description="Read memory addresses from live session.",
            inputSchema={
                "type": "object",
                "properties": {
                    "session": {"type": "string"},
                    "addresses": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "Addresses to read.",
                    },
                    "timeout": {
                        "type": "number",
                        "description": "Command timeout in seconds.",
                        "default": 20.0,
                    },
                },
                "required": ["addresses"],
            },
        ),
        Tool(
            name="mgba_live_read_range",
            description="Read contiguous memory range from live session.",
            inputSchema={
                "type": "object",
                "properties": {
                    "session": {"type": "string"},
                    "start": {"type": "integer", "description": "Range start address."},
                    "length": {"type": "integer", "description": "Byte length."},
                    "timeout": {
                        "type": "number",
                        "description": "Command timeout in seconds.",
                        "default": 20.0,
                    },
                },
                "required": ["start", "length"],
            },
        ),
        Tool(
            name="mgba_live_dump_pointers",
            description="Dump pointer table entries from live session.",
            inputSchema={
                "type": "object",
                "properties": {
                    "session": {"type": "string"},
                    "start": {"type": "integer", "description": "Pointer table start address."},
                    "count": {"type": "integer", "description": "Entries to read."},
                    "width": {"type": "integer", "default": 4},
                    "timeout": {
                        "type": "number",
                        "description": "Command timeout in seconds.",
                        "default": 20.0,
                    },
                },
                "required": ["start", "count"],
            },
        ),
        Tool(
            name="mgba_live_dump_oam",
            description="Dump OAM entries from live session.",
            inputSchema={
                "type": "object",
                "properties": {
                    "session": {"type": "string"},
                    "count": {"type": "integer", "default": 40},
                    "timeout": {
                        "type": "number",
                        "description": "Command timeout in seconds.",
                        "default": 20.0,
                    },
                },
            },
        ),
        Tool(
            name="mgba_live_dump_entities",
            description="Dump structured entity bytes from live session.",
            inputSchema={
                "type": "object",
                "properties": {
                    "session": {"type": "string"},
                    "base": {"type": "integer", "default": 49664},
                    "size": {"type": "integer", "default": 24},
                    "count": {"type": "integer", "default": 10},
                    "timeout": {
                        "type": "number",
                        "description": "Command timeout in seconds.",
                        "default": 20.0,
                    },
                },
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent | ImageContent]:
    args = arguments or {}
    timeout = float(args.get("timeout", 20.0))

    if name == "mgba_live_start":
        if args.get("script") is not None:
            raise ValueError(
                "mgba_live_start no longer accepts script. "
                "Use mgba_live_start_with_lua with file or code."
            )
        cmd_args = _build_start_command_args(args)
        return await _run_with_snapshot(
            "start",
            cmd_args,
            timeout=timeout,
            include_snapshot=False,
        )
    if name == "mgba_live_start_with_lua":
        file_arg = args.get("file")
        code_arg = args.get("code")
        has_file = bool(file_arg)
        has_code = bool(code_arg)
        if has_file == has_code:
            raise ValueError("Exactly one of file or code is required.")

        start_args = _build_start_command_args(args)
        start_result = await _controller.run("start", start_args, timeout=timeout)
        start_payload = start_result.payload
        session_id = start_payload.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            raise RuntimeError("Start command did not return session_id.")

        lua_args: list[str] = []
        if has_file:
            lua_args.extend(["--file", str(file_arg)])
        else:
            lua_args.extend(["--code", str(code_arg)])
        lua_args.extend(["--session", session_id])
        try:
            lua_contents = await _run_with_snapshot(
                "run-lua",
                lua_args,
                timeout=timeout,
                session_id=session_id,
                ensure_post_lua_settle=True,
            )
        except Exception as exc:
            raise RuntimeError(
                f"Lua execution failed after starting session '{session_id}'. "
                "Session is still running; use mgba_live_attach or mgba_live_status to inspect. "
                f"Original error: {exc}"
            ) from exc

        lua_payload = _text_payload(lua_contents[0])
        combined_payload: dict[str, Any] = {"session_id": session_id}
        if isinstance(start_payload, dict) and "pid" in start_payload:
            combined_payload["pid"] = start_payload["pid"]

        lua_result = _extract_run_lua_result(lua_payload)
        combined_payload["lua"] = lua_result if lua_result is not None else lua_payload
        if "screenshot" in lua_payload:
            combined_payload["screenshot"] = lua_payload["screenshot"]

        image_contents = [
            content for content in lua_contents if getattr(content, "type", None) == "image"
        ]
        return [_text_content(combined_payload), *image_contents]
    if name == "mgba_live_attach":
        cmd_args = _build_session_arg(args)
        if pid := args.get("pid"):
            cmd_args.extend(["--pid", str(int(pid))])
        return await _run_with_snapshot("attach", cmd_args, timeout=timeout)

    if name == "mgba_live_status":
        cmd_args = []
        if args.get("all"):
            cmd_args.append("--all")
        cmd_args.extend(_build_session_arg(args))
        return await _run_with_snapshot(
            "status",
            cmd_args,
            timeout=timeout,
        )
    if name == "mgba_live_stop":
        cmd_args = _build_session_arg(args)
        if grace := args.get("grace"):
            cmd_args.extend(["--grace", str(float(grace))])
        return await _run_with_snapshot("stop", cmd_args, timeout=timeout, include_snapshot=False)

    if name == "mgba_live_run_lua":
        cmd_args = []
        if file := args.get("file"):
            cmd_args.extend(["--file", str(file)])
        if code := args.get("code"):
            cmd_args.extend(["--code", str(code)])
        if not cmd_args:
            raise ValueError("One of file or code is required")
        cmd_args.extend(_build_session_arg(args))
        return await _run_with_snapshot(
            "run-lua",
            cmd_args,
            timeout=timeout,
            ensure_post_lua_settle=True,
        )

    if name == "mgba_live_input_tap":
        if "key" not in args:
            raise ValueError("key is required")
        wait_frames_raw = args.get("wait_frames", 0)
        if wait_frames_raw is None:
            wait_frames = 0
        elif isinstance(wait_frames_raw, bool):
            raise ValueError("wait_frames must be a non-negative integer")
        elif isinstance(wait_frames_raw, int):
            wait_frames = wait_frames_raw
        elif isinstance(wait_frames_raw, float) and wait_frames_raw.is_integer():
            wait_frames = int(wait_frames_raw)
        else:
            raise ValueError("wait_frames must be a non-negative integer")
        if wait_frames < 0:
            raise ValueError("wait_frames must be >= 0")
        cmd_args = ["--key", str(args["key"])]
        if frames := args.get("frames"):
            cmd_args.extend(["--frames", str(int(frames))])
        cmd_args.extend(_build_session_arg(args))
        return await _run_with_snapshot(
            "input-tap",
            cmd_args,
            timeout=timeout,
            require_snapshot_session=True,
            require_screenshot=True,
            input_tap_wait_frames=wait_frames,
        )

    if name == "mgba_live_input_set":
        cmd_args = ["--keys", *_parse_args_list(args.get("keys"))]
        cmd_args.extend(_build_session_arg(args))
        return await _run_with_snapshot(
            "input-set",
            cmd_args,
            timeout=timeout,
            include_snapshot=False,
        )

    if name == "mgba_live_input_clear":
        cmd_args = []
        if keys := args.get("keys"):
            cmd_args.extend(["--keys", *_parse_args_list(keys)])
        cmd_args.extend(_build_session_arg(args))
        return await _run_with_snapshot(
            "input-clear",
            cmd_args,
            timeout=timeout,
            include_snapshot=False,
        )

    if name == "mgba_live_export_screenshot":
        cmd_args = []
        if out := args.get("out"):
            cmd_args.extend(["--out", str(out)])
        cmd_args.extend(_build_session_arg(args))
        command_result = await _controller.run("screenshot", cmd_args, timeout=timeout)
        payload: dict[str, Any]
        if isinstance(command_result.payload, dict):
            payload = dict(command_result.payload)
        else:
            payload = {"value": command_result.payload}
        contents = [_text_content(payload)]
        shot_image = (
            _image_content(command_result.payload)
            if isinstance(command_result.payload, dict)
            else None
        )
        if shot_image is not None:
            contents.append(shot_image)
        return contents

    if name == "mgba_live_read_memory":
        cmd_args = ["--addresses", *_parse_args_list(args.get("addresses"))]
        cmd_args.extend(_build_session_arg(args))
        return await _run_with_snapshot("read-memory", cmd_args, timeout=timeout)

    if name == "mgba_live_read_range":
        cmd_args = ["--start", str(args["start"]), "--length", str(int(args["length"]))]
        cmd_args.extend(_build_session_arg(args))
        return await _run_with_snapshot("read-range", cmd_args, timeout=timeout)

    if name == "mgba_live_dump_pointers":
        cmd_args = ["--start", str(args["start"]), "--count", str(int(args["count"]))]
        if width := args.get("width"):
            cmd_args.extend(["--width", str(int(width))])
        cmd_args.extend(_build_session_arg(args))
        return await _run_with_snapshot("dump-pointers", cmd_args, timeout=timeout)

    if name == "mgba_live_dump_oam":
        cmd_args = []
        if count := args.get("count"):
            cmd_args.extend(["--count", str(int(count))])
        cmd_args.extend(_build_session_arg(args))
        return await _run_with_snapshot("dump-oam", cmd_args, timeout=timeout)

    if name == "mgba_live_dump_entities":
        cmd_args = []
        if base := args.get("base"):
            cmd_args.extend(["--base", str(base)])
        if size := args.get("size"):
            cmd_args.extend(["--size", str(size)])
        if count := args.get("count"):
            cmd_args.extend(["--count", str(count)])
        cmd_args.extend(_build_session_arg(args))
        return await _run_with_snapshot("dump-entities", cmd_args, timeout=timeout)

    return [TextContent(type="text", text=f"Unknown tool: {name}")]


def main() -> None:
    import asyncio

    async def run() -> None:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options())

    asyncio.run(run())


if __name__ == "__main__":
    main()
