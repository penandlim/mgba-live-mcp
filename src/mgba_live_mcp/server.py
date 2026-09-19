"""MCP server for persistent live mGBA control."""

from __future__ import annotations

import asyncio
import base64
import json
from functools import cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError
from jsonschema.protocols import Validator
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult, ContentBlock, ImageContent, TextContent, Tool, ToolAnnotations
from pydantic import JsonValue, TypeAdapter

from . import __version__
from . import result_types as results
from .deadlines import check_deadline, current_deadline, operation_timeout, suspend_deadline
from .errors import DomainError, error_payload
from .live_controller import LiveControllerClient
from .screenshots import ScreenshotResult, validate_png
from .session_manager import SessionManager

server = Server("mgba-live-mcp", version=__version__)
_controller = LiveControllerClient()

_LUA_SOURCE_FILE_ARG = "file"
_LUA_SOURCE_CODE_ARG = "code"
_LUA_SOURCE_RUNTIME_RULE = (
    f"Provide exactly one of `{_LUA_SOURCE_FILE_ARG}` or `{_LUA_SOURCE_CODE_ARG}`."
)
_ATTACH_RUNTIME_RULE = "Provide `session` or `pid`."
_STATUS_RUNTIME_RULE = "Provide `session`, or set `all=true`."

TOOL_RUNTIME_ARGUMENT_RULES = {
    "mgba_live_start_with_lua": _LUA_SOURCE_RUNTIME_RULE,
    "mgba_live_start_with_lua_and_view": _LUA_SOURCE_RUNTIME_RULE,
    "mgba_live_attach": _ATTACH_RUNTIME_RULE,
    "mgba_live_status": _STATUS_RUNTIME_RULE,
    "mgba_live_run_lua": _LUA_SOURCE_RUNTIME_RULE,
    "mgba_live_run_lua_and_view": _LUA_SOURCE_RUNTIME_RULE,
}


def _text_content(payload: Any) -> TextContent:
    return TextContent(type="text", text=json.dumps(payload, separators=(",", ":")))


def _plain_argument_rule(rule: str) -> str:
    return rule.replace("`", "")


def _lua_source_properties() -> dict[str, dict[str, str]]:
    rule = _plain_argument_rule(_LUA_SOURCE_RUNTIME_RULE)
    return {
        _LUA_SOURCE_FILE_ARG: {
            "type": "string",
            "description": f"Lua file path. {rule}",
        },
        _LUA_SOURCE_CODE_ARG: {
            "type": "string",
            "description": f"Inline Lua code. {rule}",
        },
    }


def _image_bytes_from_screenshot(result: dict[str, Any]) -> bytes:
    if isinstance(result, ScreenshotResult):
        return result.png

    stage = "validation"
    try:
        if "png_base64" in result:
            encoded = result["png_base64"]
            if not isinstance(encoded, str) or not encoded:
                raise ValueError("Required screenshot content is unavailable.")
            raw = base64.b64decode(encoded, validate=True)
        else:
            stage = "capture"
            path = result.get("path")
            if not isinstance(path, str) or not path:
                raise ValueError("Required screenshot path is unavailable.")
            raw = Path(path).read_bytes()
        stage = "validation"
        validate_png(raw)
        return raw
    except (ValueError, OSError) as exc:
        raise DomainError(
            "snapshot_failed",
            str(exc),
            phase="snapshot",
            execution_outcome="completed",
            stage=stage,
            **{
                key: result[key]
                for key in ("session_id", "request_id", "frame", "path")
                if key in result
            },
        ) from exc


def _image_content(result: dict[str, Any]) -> ImageContent:
    raw = _image_bytes_from_screenshot(result)
    encoded = base64.b64encode(raw).decode()
    return ImageContent(type="image", data=encoded, mimeType="image/png")


def _require_session(arguments: dict[str, Any]) -> str:
    session = arguments.get("session")
    if session is None:
        raise DomainError(
            "session_required",
            "session is required.",
            phase="validation",
            execution_outcome="not_executed",
        )
    return SessionManager.validate_session_id(session)


def _maybe_session(arguments: dict[str, Any]) -> str | None:
    session = arguments.get("session")
    return None if session is None else SessionManager.validate_session_id(session)


def _all_sessions_requested(arguments: dict[str, Any]) -> bool:
    all_value = arguments.get("all")
    if all_value is None:
        return False
    if not isinstance(all_value, bool):
        raise ValueError("all must be a boolean.")
    return all_value


def _parse_keys(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("keys must be an array of strings.")
    return [str(item) for item in value]


def _parse_required_keys(arguments: dict[str, Any]) -> list[str]:
    if "keys" not in arguments:
        raise ValueError("keys is required.")
    return _parse_keys(arguments.get("keys"))


def _parse_optional_pid(arguments: dict[str, Any]) -> int | None:
    pid = arguments.get("pid")
    if pid is None:
        return None
    if isinstance(pid, bool) or not isinstance(pid, int):
        raise ValueError("pid must be an integer.")
    return pid


def _parse_grace(arguments: dict[str, Any]) -> float:
    grace = arguments.get("grace")
    if grace is None:
        return 1.0
    if isinstance(grace, bool) or not isinstance(grace, (int, float)):
        raise ValueError("grace must be a number.")
    return float(grace)


def _parse_wait_frames(arguments: dict[str, Any]) -> int:

    wait_frames_raw = arguments.get("wait_frames", 0)
    if wait_frames_raw is None:
        return 0
    if isinstance(wait_frames_raw, bool):
        raise ValueError("wait_frames must be a non-negative integer")
    if isinstance(wait_frames_raw, int):
        wait_frames = wait_frames_raw
    elif isinstance(wait_frames_raw, float) and wait_frames_raw.is_integer():
        wait_frames = int(wait_frames_raw)
    else:
        raise ValueError("wait_frames must be a non-negative integer")
    if wait_frames < 0:
        raise ValueError("wait_frames must be >= 0")
    return wait_frames


def _lua_source_kwargs(arguments: dict[str, Any]) -> dict[str, Any]:
    file_arg = arguments.get(_LUA_SOURCE_FILE_ARG)
    code_arg = arguments.get(_LUA_SOURCE_CODE_ARG)
    has_file = bool(file_arg)
    has_code = bool(code_arg)
    if has_file == has_code:
        raise ValueError(
            f"Exactly one of {_LUA_SOURCE_FILE_ARG} or {_LUA_SOURCE_CODE_ARG} is required."
        )
    if has_file:
        return {_LUA_SOURCE_FILE_ARG: str(file_arg)}
    return {_LUA_SOURCE_CODE_ARG: str(code_arg)}


def _build_start_kwargs(arguments: dict[str, Any]) -> dict[str, Any]:
    rom = arguments.get("rom")
    if not isinstance(rom, str) or not rom:
        raise ValueError("rom is required")

    kwargs: dict[str, Any] = {"rom": rom}
    savestate = arguments.get("savestate")
    if savestate is not None:
        if not isinstance(savestate, str) or not savestate:
            raise ValueError("savestate must be a non-empty string")
        kwargs["savestate"] = savestate
    fps_target = arguments.get("fps_target")
    if fps_target is not None:
        if isinstance(fps_target, bool) or not isinstance(fps_target, (int, float)):
            raise ValueError("fps_target must be a number")
        kwargs["fps_target"] = float(fps_target)
    fast = arguments.get("fast")
    if fast is not None:
        if not isinstance(fast, bool):
            raise ValueError("fast must be a boolean")
        if fast:
            kwargs["fast"] = True
    session_id = arguments.get("session_id")
    if session_id is not None:
        kwargs["session_id"] = SessionManager.validate_session_id(session_id)
    mgba_path = arguments.get("mgba_path")
    if mgba_path is not None:
        if not isinstance(mgba_path, str) or not mgba_path:
            raise ValueError("mgba_path must be a non-empty string")
        kwargs["mgba_path"] = mgba_path
    return kwargs


def _tool(
    result_type: Any,
    *,
    read_only: bool = False,
    destructive: bool = True,
    idempotent: bool = False,
    open_world: bool = False,
    **definition: Any,
) -> Tool:
    definition["inputSchema"]["additionalProperties"] = False
    timeout = definition["inputSchema"]["properties"].get("timeout")
    if timeout is not None:
        timeout.update(
            exclusiveMinimum=0,
            description=(
                "Finite positive budget in seconds for the entire operation, including validation, "
                "worker queueing, execution, settling, capture, and result assembly as applicable. "
                "Expiry does not prove nonexecution; inspect execution_outcome before recovery."
            ),
        )
    return Tool(
        **definition,
        outputSchema={"type": "object", **TypeAdapter(result_type).json_schema()},
        annotations=ToolAnnotations(
            readOnlyHint=read_only,
            destructiveHint=destructive,
            idempotentHint=idempotent,
            openWorldHint=open_world,
        ),
    )


@cache
def _tools() -> dict[str, Tool]:
    catalog = [
        _tool(
            results.Started,
            open_world=True,
            name="mgba_live_start",
            description="Validate local inputs and transactionally start a session.",
            inputSchema={
                "type": "object",
                "properties": {
                    "rom": {"type": "string", "description": "Path to ROM (.gba/.gb/.gbc)."},
                    "savestate": {"type": "string", "description": "Optional savestate path."},
                    "fps_target": {"type": "number", "description": "Explicit fpsTarget."},
                    "fast": {"type": "boolean", "description": "Shortcut for fps_target=600."},
                    "session_id": {
                        "type": "string",
                        "description": "Optional explicit session id.",
                    },
                    "mgba_path": {"type": "string", "description": "Optional mGBA binary path."},
                    "timeout": {"type": "number", "default": 20.0},
                },
                "required": ["rom"],
            },
        ),
        _tool(
            results.StartupLua,
            open_world=True,
            name="mgba_live_start_with_lua",
            description="Start and run unrestricted Lua. Metadata only; not safely retryable.",
            inputSchema={
                "type": "object",
                "properties": {
                    "rom": {"type": "string", "description": "Path to ROM (.gba/.gb/.gbc)."},
                    "savestate": {"type": "string", "description": "Optional savestate path."},
                    "fps_target": {"type": "number", "description": "Explicit fpsTarget."},
                    "fast": {"type": "boolean", "description": "Shortcut for fps_target=600."},
                    "session_id": {
                        "type": "string",
                        "description": "Optional explicit session id.",
                    },
                    "mgba_path": {"type": "string", "description": "Optional mGBA binary path."},
                    **_lua_source_properties(),
                    "timeout": {"type": "number", "default": 20.0},
                },
                "required": ["rom"],
            },
        ),
        _tool(
            results.StartupView,
            open_world=True,
            name="mgba_live_start_with_lua_and_view",
            description="Start, run unrestricted Lua, settle, capture. Not safely retryable.",
            inputSchema={
                "type": "object",
                "properties": {
                    "rom": {"type": "string", "description": "Path to ROM (.gba/.gb/.gbc)."},
                    "savestate": {"type": "string", "description": "Optional savestate path."},
                    "fps_target": {"type": "number", "description": "Explicit fpsTarget."},
                    "fast": {"type": "boolean", "description": "Shortcut for fps_target=600."},
                    "session_id": {
                        "type": "string",
                        "description": "Optional explicit session id.",
                    },
                    "mgba_path": {"type": "string", "description": "Optional mGBA binary path."},
                    **_lua_source_properties(),
                    "timeout": {"type": "number", "default": 20.0},
                },
                "required": ["rom"],
            },
        ),
        _tool(
            results.Attached,
            destructive=False,
            idempotent=True,
            name="mgba_live_attach",
            description="Attach to a managed session; updates the CLI active-session marker.",
            inputSchema={
                "type": "object",
                "properties": {
                    "session": {
                        "type": "string",
                        "description": f"Session id. {_plain_argument_rule(_ATTACH_RUNTIME_RULE)}",
                    },
                    "pid": {
                        "type": "integer",
                        "description": "PID of a managed session. "
                        f"{_plain_argument_rule(_ATTACH_RUNTIME_RULE)}",
                    },
                    "timeout": {"type": "number", "default": 20.0},
                },
            },
        ),
        _tool(
            results.Status | results.StatusList,
            idempotent=True,
            name="mgba_live_status",
            description="Show metadata; archives dead sessions and refreshes the active marker.",
            inputSchema={
                "type": "object",
                "properties": {
                    "session": {
                        "type": "string",
                        "description": "Session id for one session. "
                        f"{_plain_argument_rule(_STATUS_RUNTIME_RULE)}",
                    },
                    "all": {
                        "type": "boolean",
                        "description": "If true, list all sessions. Otherwise pass session.",
                    },
                    "timeout": {"type": "number", "default": 20.0},
                },
            },
        ),
        _tool(
            results.View,
            read_only=True,
            destructive=False,
            idempotent=True,
            name="mgba_live_get_view",
            description="Capture a screenshot using a temporary file; no emulator mutation.",
            inputSchema={
                "type": "object",
                "properties": {
                    "session": {"type": "string", "description": "Session id."},
                    "timeout": {"type": "number", "default": 20.0},
                },
                "required": ["session"],
            },
        ),
        _tool(
            results.Stopped,
            idempotent=True,
            name="mgba_live_stop",
            description=(
                "Stop a managed group independently of operation deadlines; retire its generation. "
                "Repeated stop confirms exit."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "session": {"type": "string", "description": "Session id to stop."},
                    "grace": {
                        "type": "number",
                        "minimum": 0,
                        "description": (
                            "Independent SIGTERM grace period in seconds before SIGKILL; "
                            "not a total operation timeout."
                        ),
                    },
                },
                "required": ["session"],
            },
        ),
        _tool(
            results.Command[JsonValue],
            open_world=True,
            name="mgba_live_run_lua",
            description="Unrestricted Lua; may change emulator/files/processes. No safe retry.",
            inputSchema={
                "type": "object",
                "properties": {
                    "session": {"type": "string", "description": "Session id."},
                    **_lua_source_properties(),
                    "timeout": {"type": "number", "default": 20.0},
                },
                "required": ["session"],
            },
        ),
        _tool(
            results.CommandView[JsonValue],
            open_world=True,
            name="mgba_live_run_lua_and_view",
            description="Unrestricted Lua, settle, capture; changes may persist. No safe retry.",
            inputSchema={
                "type": "object",
                "properties": {
                    "session": {"type": "string", "description": "Session id."},
                    **_lua_source_properties(),
                    "timeout": {"type": "number", "default": 20.0},
                },
                "required": ["session"],
            },
        ),
        _tool(
            results.Command[results.Tap],
            name="mgba_live_input_tap",
            description="Tap a key for N frames. Metadata only.",
            inputSchema={
                "type": "object",
                "properties": {
                    "session": {"type": "string"},
                    "key": {
                        "type": "string",
                        "description": "A/B/START/SELECT/UP/DOWN/LEFT/RIGHT/L/R.",
                    },
                    "frames": {"type": "integer", "default": 1},
                    "timeout": {"type": "number", "default": 20.0},
                },
                "required": ["session", "key"],
            },
        ),
        _tool(
            results.CommandView[results.Tap],
            name="mgba_live_input_tap_and_view",
            description="Tap a key, optionally wait additional frames, then return one screenshot.",
            inputSchema={
                "type": "object",
                "properties": {
                    "session": {"type": "string"},
                    "key": {
                        "type": "string",
                        "description": "A/B/START/SELECT/UP/DOWN/LEFT/RIGHT/L/R.",
                    },
                    "frames": {"type": "integer", "default": 1},
                    "wait_frames": {"type": "integer", "default": 0, "minimum": 0},
                    "timeout": {"type": "number", "default": 20.0},
                },
                "required": ["session", "key"],
            },
        ),
        _tool(
            results.Command[results.Keys],
            idempotent=True,
            name="mgba_live_input_set",
            description="Replace held keys and cancel scheduled releases; the game keeps running.",
            inputSchema={
                "type": "object",
                "properties": {
                    "session": {"type": "string"},
                    "keys": {"type": "array", "items": {"type": "string"}},
                    "timeout": {"type": "number", "default": 20.0},
                },
                "required": ["session", "keys"],
            },
        ),
        _tool(
            results.Command[results.Keys | results.Cleared],
            idempotent=True,
            name="mgba_live_input_clear",
            description="Clear held keys from a live session.",
            inputSchema={
                "type": "object",
                "properties": {
                    "session": {"type": "string"},
                    "keys": {"type": "array", "items": {"type": "string"}},
                    "timeout": {"type": "number", "default": 20.0},
                },
                "required": ["session"],
            },
        ),
        _tool(
            results.Exported,
            open_world=True,
            name="mgba_live_export_screenshot",
            description="Save a screenshot; may overwrite the requested file.",
            inputSchema={
                "type": "object",
                "properties": {
                    "session": {"type": "string", "description": "Session id."},
                    "out": {"type": "string", "description": "Optional persisted PNG output path."},
                    "timeout": {"type": "number", "default": 20.0},
                },
                "required": ["session"],
            },
        ),
        _tool(
            results.Memory,
            read_only=True,
            destructive=False,
            idempotent=True,
            name="mgba_live_read_memory",
            description="Read memory addresses from a live session.",
            inputSchema={
                "type": "object",
                "properties": {
                    "session": {"type": "string"},
                    "addresses": {"type": "array", "items": {"type": "integer"}},
                    "timeout": {"type": "number", "default": 20.0},
                },
                "required": ["session", "addresses"],
            },
        ),
        _tool(
            results.MemoryRange,
            read_only=True,
            destructive=False,
            idempotent=True,
            name="mgba_live_read_range",
            description="Read a contiguous memory range from a live session.",
            inputSchema={
                "type": "object",
                "properties": {
                    "session": {"type": "string"},
                    "start": {"type": "integer"},
                    "length": {"type": "integer"},
                    "timeout": {"type": "number", "default": 20.0},
                },
                "required": ["session", "start", "length"],
            },
        ),
        _tool(
            results.Pointers,
            read_only=True,
            destructive=False,
            idempotent=True,
            name="mgba_live_dump_pointers",
            description="Dump pointer table entries from a live session.",
            inputSchema={
                "type": "object",
                "properties": {
                    "session": {"type": "string"},
                    "start": {"type": "integer"},
                    "count": {"type": "integer"},
                    "width": {"type": "integer", "minimum": 1, "maximum": 6, "default": 4},
                    "timeout": {"type": "number", "default": 20.0},
                },
                "required": ["session", "start", "count"],
            },
        ),
        _tool(
            results.Oam,
            read_only=True,
            destructive=False,
            idempotent=True,
            name="mgba_live_dump_oam",
            description="Dump OAM entries from a live session.",
            inputSchema={
                "type": "object",
                "properties": {
                    "session": {"type": "string"},
                    "count": {"type": "integer", "default": 40},
                    "timeout": {"type": "number", "default": 20.0},
                },
                "required": ["session"],
            },
        ),
        _tool(
            results.Entities,
            read_only=True,
            destructive=False,
            idempotent=True,
            name="mgba_live_dump_entities",
            description="Dump structured entity bytes from a live session.",
            inputSchema={
                "type": "object",
                "properties": {
                    "session": {"type": "string"},
                    "base": {"type": "integer", "default": 49664},
                    "size": {"type": "integer", "default": 24},
                    "count": {"type": "integer", "default": 10},
                    "timeout": {"type": "number", "default": 20.0},
                },
                "required": ["session"],
            },
        ),
    ]
    return {tool.name: tool for tool in catalog}


@server.list_tools()
async def list_tools() -> list[Tool]:
    return list(_tools().values())


@cache
def _validators(name: str) -> tuple[Validator, Validator]:
    tool = _tools()[name]
    return Draft202012Validator(tool.inputSchema), Draft202012Validator(tool.outputSchema)


@server.call_tool(validate_input=False)
async def call_tool(name: str, arguments: dict[str, Any]) -> CallToolResult:
    args = arguments
    context: dict[str, Any] = {"tool": name}
    if isinstance(args, dict):
        context["session_id"] = args.get("session") or args.get("session_id")
        context["pid"] = args.get("pid")
    try:
        context["mcp_request_id"] = server.request_context.request_id
    except LookupError:
        pass
    try:
        tool = _tools().get(name)
        if tool is None:
            raise DomainError(
                "unknown_tool",
                f"Unknown tool: {name}",
                phase="dispatch",
                execution_outcome="not_executed",
            )
        budget = (
            suspend_deadline()
            if name == "mgba_live_stop"
            else operation_timeout(
                args.get("timeout", 20.0) if isinstance(args, dict) else 20.0,
                session_id=context.get("session_id"),
            )
        )
        with budget:
            check_deadline("validation")
            input_validator, output_validator = _validators(name)
            try:
                input_validator.validate(args)
            except ValidationError as exc:
                raise DomainError(
                    "invalid_arguments",
                    exc.message,
                    phase="validation",
                    execution_outcome="not_executed",
                ) from exc
            check_deadline("dispatch")
            payload = await _dispatch_tool(name, args)
            deadline = current_deadline()
            if deadline is not None:
                deadline.execution_outcome = "completed"
            check_deadline("result")
            if not isinstance(payload, dict):
                raise DomainError(
                    "invalid_result",
                    "Expected an object result.",
                    phase="result",
                    execution_outcome="completed",
                )
            visual = name.endswith("_and_view") or name in {
                "mgba_live_get_view",
                "mgba_live_export_screenshot",
            }
            image = _image_content(payload) if visual else None
            check_deadline("result")
            public = {key: value for key, value in payload.items() if key != "png_base64"}
            try:
                output_validator.validate(public)
            except ValidationError as exc:
                raise DomainError(
                    "invalid_result",
                    exc.message,
                    phase="result",
                    execution_outcome="completed",
                ) from exc
            content: list[ContentBlock] = [_text_content(public)]
            if image is not None:
                content.append(image)
            return CallToolResult(content=content, structuredContent=public)
    except Exception as exc:
        failure = error_payload(exc, **context)
        return CallToolResult(
            content=[_text_content(failure)], structuredContent=failure, isError=True
        )


async def _dispatch_tool(
    name: str, arguments: dict[str, Any]
) -> dict[str, Any] | list[dict[str, Any]]:
    args = arguments or {}
    timeout = args.get("timeout", 20.0)

    if name == "mgba_live_start":
        payload = await _controller.start(timeout=timeout, **_build_start_kwargs(args))
        return payload

    if name == "mgba_live_start_with_lua":
        payload = await _controller.start_with_lua(
            timeout=timeout,
            **_build_start_kwargs(args),
            **_lua_source_kwargs(args),
        )
        return payload

    if name == "mgba_live_start_with_lua_and_view":
        payload = await _controller.start_with_lua_and_view(
            timeout=timeout,
            **_build_start_kwargs(args),
            **_lua_source_kwargs(args),
        )
        return payload

    if name == "mgba_live_attach":
        session = _maybe_session(args)
        pid = _parse_optional_pid(args)
        if session is None and pid is None:
            raise DomainError(
                "session_required",
                "provide session or pid.",
                phase="validation",
                execution_outcome="not_executed",
            )
        payload = await _controller.attach(session=session, pid=pid, timeout=timeout)
        return payload

    if name == "mgba_live_status":
        if _all_sessions_requested(args):
            payload = await _controller.status(
                session=_maybe_session(args), all=True, timeout=timeout
            )
            return {"value": payload}
        payload = await _controller.status(session=_require_session(args), timeout=timeout)
        return payload

    if name == "mgba_live_get_view":
        payload = await _controller.get_view(session=_require_session(args), timeout=timeout)
        public_payload = {
            "session_id": payload.get("session_id"),
            "screenshot": {"frame": payload.get("frame")},
            "png_base64": payload.get("png_base64"),
        }
        if isinstance(payload, ScreenshotResult):
            return ScreenshotResult(public_payload, payload.png)
        return public_payload

    if name == "mgba_live_stop":
        payload = await _controller.stop(
            session=_require_session(args),
            grace=_parse_grace(args),
        )
        return payload

    if name == "mgba_live_run_lua":
        payload = await _controller.run_lua(
            session=_require_session(args),
            timeout=timeout,
            **_lua_source_kwargs(args),
        )
        return payload

    if name == "mgba_live_run_lua_and_view":
        payload = await _controller.run_lua_and_view(
            session=_require_session(args),
            timeout=timeout,
            **_lua_source_kwargs(args),
        )
        return payload

    if name == "mgba_live_input_tap":
        if "key" not in args:
            raise ValueError("key is required")
        payload = await _controller.input_tap(
            session=_require_session(args),
            key=str(args["key"]),
            frames=int(args.get("frames", 1)),
            timeout=timeout,
        )
        return payload

    if name == "mgba_live_input_tap_and_view":
        if "key" not in args:
            raise ValueError("key is required")
        payload = await _controller.input_tap_and_view(
            session=_require_session(args),
            key=str(args["key"]),
            frames=int(args.get("frames", 1)),
            wait_frames=_parse_wait_frames(args),
            timeout=timeout,
        )
        return payload

    if name == "mgba_live_input_set":
        payload = await _controller.input_set(
            session=_require_session(args),
            keys=_parse_required_keys(args),
            timeout=timeout,
        )
        return payload

    if name == "mgba_live_input_clear":
        keys = None
        if "keys" in args:
            keys = _parse_keys(args.get("keys"))
        payload = await _controller.input_clear(
            session=_require_session(args),
            keys=keys,
            timeout=timeout,
        )
        return payload

    if name == "mgba_live_export_screenshot":
        payload = await _controller.export_screenshot(
            session=_require_session(args),
            out=str(args["out"]) if args.get("out") else None,
            timeout=timeout,
        )
        return payload

    if name == "mgba_live_read_memory":
        payload = await _controller.read_memory(
            session=_require_session(args),
            addresses=list(args.get("addresses", [])),
            timeout=timeout,
        )
        return payload

    if name == "mgba_live_read_range":
        payload = await _controller.read_range(
            session=_require_session(args),
            start=args["start"],
            length=int(args["length"]),
            timeout=timeout,
        )
        return payload

    if name == "mgba_live_dump_pointers":
        payload = await _controller.dump_pointers(
            session=_require_session(args),
            start=args["start"],
            count=int(args["count"]),
            width=args.get("width", 4),
            timeout=timeout,
        )
        return payload

    if name == "mgba_live_dump_oam":
        payload = await _controller.dump_oam(
            session=_require_session(args),
            count=int(args.get("count", 40)),
            timeout=timeout,
        )
        return payload

    if name == "mgba_live_dump_entities":
        payload = await _controller.dump_entities(
            session=_require_session(args),
            base=args.get("base", 49664),
            size=int(args.get("size", 24)),
            count=int(args.get("count", 10)),
            timeout=timeout,
        )
        return payload

    raise DomainError(
        "unknown_tool",
        f"Unknown tool: {name}",
        phase="dispatch",
        execution_outcome="not_executed",
    )


def main() -> None:
    async def run() -> None:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options())

    asyncio.run(run())


if __name__ == "__main__":
    main()
