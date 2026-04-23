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

DEFAULT_OUTPUT = Path("docs/mcp-reference.md")
RUNTIME_ARGUMENT_RULES = {
    "mgba_live_start_with_lua": "Provide exactly one of `file` or `code`.",
    "mgba_live_start_with_lua_and_view": "Provide exactly one of `file` or `code`.",
    "mgba_live_attach": "Provide `session` or `pid`.",
    "mgba_live_status": "Provide `session`, or set `all=true`.",
    "mgba_live_run_lua": "Provide exactly one of `file` or `code`.",
    "mgba_live_run_lua_and_view": "Provide exactly one of `file` or `code`.",
}


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
    runtime_rule = RUNTIME_ARGUMENT_RULES.get(name)
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
        ]
    )


def _render_markdown(tools: list[Tool]) -> str:
    sections = [_render_tool_section(tool) for tool in tools]
    tool_names = ", ".join(f"`{tool.name}`" for tool in tools)

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
