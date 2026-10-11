#!/usr/bin/env bash
# Unified quality gate: locked sync, lint, format check, typecheck, tests, build, package check.
# All uv invocations strip ambient UV_INDEX (may be a plaintext HTTP mirror) via env -u;
# the --locked flag keeps uv pinned to uv.lock so later commands cannot re-resolve packages.
set -euo pipefail
cd "$(dirname "$0")/.."

LOG_DIR="${VERIFY_LOG_DIR:-}"
DIST_DIR="$(mktemp -d /tmp/cc-models-verify-dist.XXXXXX)"
cleanup() { rm -rf "$DIST_DIR"; }
trap cleanup EXIT

log() { if [ -n "$LOG_DIR" ]; then mkdir -p "$LOG_DIR"; tee -a "$LOG_DIR/$1.log"; else cat; fi; }

echo "== verify: locked sync ==" | log sync
env -u UV_INDEX uv sync --locked 2>&1 | log sync

echo "== verify: ruff check ==" | log lint
env -u UV_INDEX uv run --locked ruff check . 2>&1 | log lint

echo "== verify: ruff format --check ==" | log lint
env -u UV_INDEX uv run --locked ruff format --check . 2>&1 | log lint

echo "== verify: pyright (basic) ==" | log pyright
env -u UV_INDEX uv run --locked pyright 2>&1 | log pyright

echo "== verify: pytest ==" | log pytest
env -u UV_INDEX uv run --locked pytest -q 2>&1 | log pytest

echo "== verify: build (output to $DIST_DIR) ==" | log build
env -u UV_INDEX uv build --out-dir "$DIST_DIR" 2>&1 | log build

echo "== verify: package check ==" | log check_dist
env -u UV_INDEX uv run --locked python scripts/check_dist.py "$DIST_DIR" 2>&1 | log check_dist

echo "verify: ALL GATES PASSED"
