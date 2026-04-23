.PHONY: dev lint format typecheck test check mcp-docs mcp-docs-check precommit-install precommit-run test-rom verify-test-rom

dev:
	uv sync --group dev

test-rom:
	uv run python -m mgba_live_mcp.test_rom fetch

verify-test-rom:
	uv run python -m mgba_live_mcp.test_rom verify

lint:
	uv run ruff format --check .
	uv run ruff check .

format:
	uv run ruff format .
	uv run ruff check --fix .

typecheck:
	uv run ty check src/ scripts/ tests/

test: test-rom
	uv run pytest

check: lint typecheck test mcp-docs-check

mcp-docs:
	uv run python scripts/generate_mcp_reference.py

mcp-docs-check:
	uv run python scripts/generate_mcp_reference.py --check

precommit-install:
	uv run pre-commit install

precommit-run:
	uv run pre-commit run --all-files
