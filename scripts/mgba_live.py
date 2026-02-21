#!/usr/bin/env python3
"""Compatibility wrapper for the packaged live CLI.

This file is intentionally thin to preserve existing dev commands such as:
`uv run python scripts/mgba_live.py --help`.
"""

from mgba_live_mcp.live_cli import main

if __name__ == "__main__":
    main()
