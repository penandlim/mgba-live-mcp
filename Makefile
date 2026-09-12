.PHONY: dev lint format typecheck test check mcp-docs mcp-docs-check precommit-install precommit-run test-rom verify-test-rom native-build native-smoke

dev:
	uv sync --group dev

test-rom:
	uv run python -m mgba_live_mcp.test_rom fetch

verify-test-rom:
	uv run python -m mgba_live_mcp.test_rom verify

MGBA_PATH ?= .native/mgba/build/qt/mgba-qt
NATIVE_PROVENANCE ?= .native/mgba/provenance.json
NATIVE_ARTIFACTS ?= .native/smoke-$(shell date -u +%Y%m%dT%H%M%SZ)

native-build:
	uv run python scripts/provision_native.py

native-smoke:
	uv run --group native python scripts/native_smoke.py --mgba "$(MGBA_PATH)" --build-provenance "$(NATIVE_PROVENANCE)" --artifacts "$(NATIVE_ARTIFACTS)"

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

precommit-install:
	uv run pre-commit install

precommit-run:
	uv run pre-commit run --all-files
