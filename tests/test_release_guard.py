"""Behavioral tests for scripts/check_release_state.sh via a mock gh binary.

The mock gh stands in for `gh api .../releases/tags/<tag> --jq .draft` and
covers the four branches the release workflow depends on: absent (404),
draft with and without --clobber, public release, and a metadata query
failure that must not read as "absent".
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
GUARD = ROOT / "scripts" / "check_release_state.sh"

MOCK_GH = """\
#!/usr/bin/env bash
case "${GH_MOCK_CASE:?set GH_MOCK_CASE}" in
  absent)
    echo "gh: Not Found (HTTP 404)" >&2
    exit 1
    ;;
  draft)
    echo "true"
    ;;
  public)
    echo "false"
    ;;
  api-error)
    echo "gh: API error (HTTP 502)" >&2
    exit 1
    ;;
  *)
    echo "gh: unexpected mock case $GH_MOCK_CASE" >&2
    exit 1
    ;;
esac
"""


@pytest.fixture()
def mock_env(tmp_path: Path) -> dict[str, str]:
    gh = tmp_path / "gh"
    gh.write_text(MOCK_GH)
    gh.chmod(gh.stat().st_mode | stat.S_IXUSR)
    return {
        **os.environ,
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "GITHUB_REPOSITORY": "toRolex/cc-models",
        "GH_MOCK_CASE": "absent",
    }


def run_guard(env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(GUARD), "v0.1.0", *args],
        capture_output=True,
        text=True,
        env=env,
        check=False,  # the guard's nonzero exits are the behavior under test
    )


def test_absent_release_reports_none(mock_env: dict[str, str]) -> None:
    result = run_guard(mock_env)
    assert result.returncode == 0
    assert result.stdout.strip() == "none"


def test_draft_release_requires_clobber(mock_env: dict[str, str]) -> None:
    mock_env["GH_MOCK_CASE"] = "draft"
    result = run_guard(mock_env)
    assert result.returncode == 1
    assert "draft" in result.stderr
    assert "--clobber" in result.stderr


def test_draft_release_with_clobber_reports_draft(mock_env: dict[str, str]) -> None:
    mock_env["GH_MOCK_CASE"] = "draft"
    result = run_guard(mock_env, "--clobber")
    assert result.returncode == 0
    assert result.stdout.strip() == "draft"


def test_public_release_is_rejected_even_with_clobber(
    mock_env: dict[str, str],
) -> None:
    mock_env["GH_MOCK_CASE"] = "public"
    assert run_guard(mock_env).returncode == 1
    result = run_guard(mock_env, "--clobber")
    assert result.returncode == 1
    assert "public" in result.stderr


def test_metadata_query_failure_fails_loudly(mock_env: dict[str, str]) -> None:
    mock_env["GH_MOCK_CASE"] = "api-error"
    result = run_guard(mock_env)
    assert result.returncode == 1
    assert "502" in result.stderr


def test_rejects_unknown_argument(mock_env: dict[str, str]) -> None:
    assert run_guard(mock_env, "--wat").returncode == 2
    assert run_guard(mock_env, "").returncode == 2
