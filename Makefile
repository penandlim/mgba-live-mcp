.PHONY: dev lint format typecheck test check

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

check: lint typecheck test
