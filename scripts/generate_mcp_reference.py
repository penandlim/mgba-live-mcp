#!/usr/bin/env python3
"""Generate Markdown reference docs from the server's MCP tool definitions."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

from mcp.types import Tool

from mgba_live_mcp import server as mcp_server
from mgba_live_mcp.errors import ERROR_CODES
from mgba_live_mcp.inspections import MAX_ITEMS, MAX_READ_BYTES, MAX_RESPONSE_BYTES

DEFAULT_OUTPUT = Path("docs/mcp-reference.md")


def _format_schema(schema: dict[str, Any] | None) -> str:
    if schema is None:
        return "_Not declared._"

    rendered = json.dumps(schema, indent=2, sort_keys=True)
    return f"```json\n{rendered}\n```"


def _format_required_fields(input_schema: dict[str, Any] | None) -> str:
    if input_schema is None:
        return "_None._"

    for keyword in ("anyOf", "oneOf"):
        branches = input_schema.get(keyword)
        if isinstance(branches, list) and branches:
            return "_Conditional; see schema._"
    required = input_schema.get("required")
    if isinstance(required, list) and required:
        items = [f"`{item}`" for item in required]
        return ", ".join(items)
    return "_None._"


def _render_tool_section(tool: Tool) -> str:
    data = tool.model_dump(exclude_none=True)
    name = str(data["name"])
    description = str(data.get("description") or "_No description provided._")
    input_schema = data.get("inputSchema")
    output_schema = data.get("outputSchema")
    runtime_rule = mcp_server.TOOL_RUNTIME_ARGUMENT_RULES.get(name)
    annotations = data.get("annotations")
    runtime_rule_lines = []
    if runtime_rule is not None:
        runtime_rule_lines = [f"- Runtime argument rule: {runtime_rule}"]

    return "\n".join(
        [
            f"## `{name}`",
            "",
            description,
            "",
            f"- Required input fields: {_format_required_fields(input_schema)}",
            *runtime_rule_lines,
            "",
            "### Input Schema",
            "",
            _format_schema(input_schema),
            "",
            "### Output Schema",
            "",
            _format_schema(output_schema),
            "",
            "### Behavior Annotations",
            "",
            _format_schema(annotations),
            "",
        ]
    )


def _render_markdown(tools: list[Tool]) -> str:
    sections = [_render_tool_section(tool) for tool in tools]
    tool_names = ", ".join(f"`{tool.name}`" for tool in tools)

    error_rows = [f"| `{code}` | {meaning} |" for code, meaning in ERROR_CODES.items()]
    return (
        "\n".join(
            [
                "# MCP Tool Reference",
                "",
                "This file is auto-generated from the server's `tools/list` metadata.",
                "Do not edit manually. Regenerate with:",
                "",
                "```bash",
                "make mcp-docs",
                "```",
                "",
                f"- Tool count: {len(tools)}",
                f"- Tools: {tool_names}",
                "",
                "## Result and error contracts",
                "",
                "Success `structuredContent` matches the tool's output schema; the first text",
                "block is the same object encoded as compact JSON. Images appear only in",
                "image blocks, never in that JSON. Existing heterogeneous fields are retained:",
                "`status(all=true)` uses `value`, Lua uses `data`/`lua`, and command `frame`",
                "is distinct from `screenshot.frame`. A nullable frame reports absence of a",
                "bridge counter, not an inferred or substituted screenshot frame.",
                "Lua `return nil` is explicitly encoded as `data.result: null` (or startup",
                "`lua: null`); `return {}` retains the bridge's existing empty-array encoding.",
                "",
                "Failures have `isError=true` and matching JSON text/structured content:",
                "`{error: {code, message, phase, execution_outcome, ...context}}`.",
                "Context includes `tool` (MCP) or `command` (CLI), `session_id`, `pid`,",
                "bridge `request_id`, and `mcp_request_id` when known. A blocking prior",
                "request is identified separately by `pending_request_id`, not confused",
                "with a refused request's execution outcome. CLI failures print this",
                "envelope to stderr and exit nonzero. Error envelopes are not successes",
                "and are not validated against the success output schema.",
                "Tool argument objects reject unknown fields with `invalid_arguments`.",
                "Malformed MCP request envelopes, such as argument arrays, are rejected",
                "by the SDK with JSON-RPC `-32602` before tool dispatch.",
                "",
                "`execution_outcome` is domain-owned: `not_executed` requires proof of",
                "nonexecution, `completed` preserves known command completion even when",
                "capture/result handling fails, and `unknown` means execution or settling",
                "cannot be established. `command_completed` and `command_request_id` retain",
                "primary mutation evidence separately from a failed capture/poll `request_id`.",
                "Snapshot/settle failures retain `cause_code`, `cause_phase`, and",
                "`cause_execution_outcome`. Known bridge failures remain primary if their",
                "completion crosses the deadline; `completion_error` records that failure.",
                "Never replay a mutation just to recover its result. Phases identify the",
                "actual failing stage, including acquisition, startup, dispatch, command,",
                "settle, snapshot, result, or native inspection/TERM/KILL.",
                "",
                "A timeout must be a finite positive number, never a boolean. One monotonic",
                "budget covers validation, worker queueing, acquisition, startup, execution,",
                "settling, capture, and result assembly. Nested caps never renew it;",
                "wall-clock jumps do not affect it. CLI `start --ready-timeout` covers all",
                "startup phases; CLI attach/status also accept `--timeout`.",
                "",
                "Timeout is not cancellation. Only an atomically proven pending owned",
                "request can be withdrawn using the bridge's `rename-v1` claim protocol.",
                "Running/ambiguous work, legacy bridges, and interrupted composites stay",
                "fenced. Cancelling an MCP caller does not release a running worker's lease.",
                "There is no automatic replay. Use independently bounded stop/recovery.",
                "",
                "Deadlines are cooperative: safety cleanup can finish after expiry, with",
                "each journal-state lock wait bounded to 0.5 seconds; filesystem/native",
                "syscalls cannot be preempted. Stop has no operation `timeout`: its separate",
                "nonnegative `grace` allows TERM, plus at most one second for escalation",
                "and exit confirmation. Ownership lock acquisition is nonblocking.",
                "",
                "Annotations are hints, not security enforcement. Read-only/idempotent",
                "hints describe requested domain effects, excluding bridge bookkeeping;",
                "live emulation continues, so repeated reads need not return identical data.",
                "Status performs maintenance, attach changes the active marker, Lua is",
                "unrestricted, and screenshot export may overwrite files.",
                "",
                "Built-in memory inspections reject requests above "
                f"{MAX_READ_BYTES} source bytes or {MAX_ITEMS} sparse addresses/pointers/entities",
                f"before reading. A conservative {MAX_RESPONSE_BYTES}-byte native JSON budget",
                "also rejects output expansion before traversal (not after reading/serialization).",
                "This bounds compact success envelopes, excluding CLI formatting/MCP wrappers.",
                "Metadata/header reserve is `max(2048, 256 + encoded request/session IDs)`.",
                "Payload costs: 17 per sparse address,",
                "4 per raw byte, 2 per hex byte, 60 per pointer, or 48 per entity plus 4 per byte.",
                "Delta costs `26 * ceil(length / 2) + 2 * length`, even for an unchanged baseline.",
                "Thus pointer/delta maxima are 512 records/2048 bytes; byte/hex ranges allow 4096.",
                "Addresses must fit the active platform; failed reads never fabricate bytes.",
                "`read_range` returns byte arrays by default; `hex` and `delta` are opt-in.",
                "Delta baselines contain exactly `start` and hex `data` for the requested region.",
                "Split larger reads explicitly: `frame` is a bridge callback counter.",
                "Chunks need not share a frame or game state. Baselines are not retained.",
                "",
                "### Stable error codes",
                "",
                "| Code | Meaning |",
                "| --- | --- |",
                *error_rows,
                "",
                *sections,
            ]
        ).rstrip()
        + "\n"
    )


async def _load_tools() -> list[Tool]:
    return await mcp_server.list_tools()


def _write_or_check(output_path: Path, content: str, *, check: bool) -> int:
    existing = output_path.read_text() if output_path.exists() else ""
    if check:
        if existing != content:
            print(f"Outdated MCP reference: {output_path}")
            print("Run `make mcp-docs` to regenerate.")
            return 1
        print(f"MCP reference is up to date: {output_path}")
        return 0

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(content)
    print(f"Wrote MCP reference: {output_path}")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="Path to generated markdown output.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Validate output is up to date without modifying files.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    tools = asyncio.run(_load_tools())
    content = _render_markdown(tools)
    return _write_or_check(args.output, content, check=bool(args.check))


if __name__ == "__main__":
    raise SystemExit(main())
