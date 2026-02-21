"""Lightweight wrapper around the live mGBA control CLI used by the MCP server."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from dataclasses import dataclass
from typing import Any


@dataclass
class LiveCommandResult:
    """Structured result of a live controller command."""

    returncode: int
    payload: dict[str, Any]
    stderr: str


class LiveControllerClient:
    """Run `mgba_live_mcp.live_cli` commands via subprocess and parse JSON output."""

    def __init__(self, module_name: str = "mgba_live_mcp.live_cli") -> None:
        self.module_name = module_name

    async def run(
        self,
        command: str,
        args: list[str],
        *,
        timeout: float = 20.0,
    ) -> LiveCommandResult:
        """Run one CLI command and return parsed JSON payload."""

        proc_args = [sys.executable, "-m", self.module_name, command] + args
        proc = await asyncio.create_subprocess_exec(
            *proc_args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ},
        )

        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except TimeoutError as exc:
            proc.kill()
            stdout, stderr = await proc.communicate()
            command_line = " ".join(proc_args)
            raise RuntimeError(f"Command timed out after {timeout}s: {command_line}") from exc

        stdout_s = stdout.decode(errors="replace")
        stderr_s = stderr.decode(errors="replace")
        if proc.returncode != 0:
            command_line = " ".join(proc_args)
            raise RuntimeError(
                f"Command failed (exit {proc.returncode}): {command_line}\n{stdout_s}\n{stderr_s}"
            )

        text = stdout_s.strip()
        if not text:
            if stderr_s.strip():
                raise RuntimeError(stderr_s.strip())
            raise RuntimeError("No output from command")

        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Invalid JSON from command: {text[:512]}") from exc

        return LiveCommandResult(
            returncode=proc.returncode,
            payload=payload if isinstance(payload, dict) else {"value": payload},
            stderr=stderr_s,
        )
