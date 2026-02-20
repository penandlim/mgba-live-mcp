.PHONY: dev lint format typecheck test check mcp-docs mcp-docs-check

dev:
	uv sync --group dev

lint:
	uv run ruff format --check .
	uv run ruff check .

format:
	uv run ruff format .
	uv run ruff check --fix .

typecheck:
	uv run ty check src/ scripts/ tests/

test:
	uv run pytest

check: lint typecheck test mcp-docs-check

mcp-docs:
	uv run python scripts/generate_mcp_reference.py

mcp-docs-check:
	uv run python scripts/generate_mcp_reference.py --check
