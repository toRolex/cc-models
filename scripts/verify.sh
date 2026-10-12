#!/usr/bin/env bash
# Unified quality gate: locked sync, lint, format, typecheck, build into a
# cleaned dist/, package check, tests (installed-CLI tests install the wheel
# built here). All uv invocations strip ambient UV_INDEX (may be a plaintext
# HTTP mirror) via env -u; the --locked flag keeps uv pinned to uv.lock.
set -euo pipefail
cd "$(dirname "$0")/.."

LOG_DIR="${VERIFY_LOG_DIR:-}"

log() { if [ -n "$LOG_DIR" ]; then mkdir -p "$LOG_DIR"; tee -a "$LOG_DIR/$1.log"; else cat; fi; }

echo "== verify: locked sync ==" | log sync
env -u UV_INDEX uv sync --locked 2>&1 | log sync

echo "== verify: ruff check ==" | log lint
env -u UV_INDEX uv run --locked ruff check . 2>&1 | log lint

echo "== verify: ruff format --check ==" | log lint
env -u UV_INDEX uv run --locked ruff format --check . 2>&1 | log lint

echo "== verify: pyright (basic) ==" | log pyright
env -u UV_INDEX uv run --locked pyright 2>&1 | log pyright

echo "== verify: build (clean dist) ==" | log build
rm -rf dist
env -u UV_INDEX uv build --out-dir dist 2>&1 | log build

echo "== verify: package check (dist) ==" | log check_dist
env -u UV_INDEX uv run --locked python scripts/check_dist.py dist 2>&1 | log check_dist

echo "== verify: regenerate formula + checksums from built artifacts ==" | log gen_formula
env -u UV_INDEX uv run --locked python scripts/gen_formula.py 2>&1 | log gen_formula

echo "== verify: pytest (installs the built wheel into a fresh venv) ==" | log pytest
env -u UV_INDEX uv run --locked pytest -q 2>&1 | log pytest

echo "verify: ALL GATES PASSED"
