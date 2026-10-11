"""Install the real wheel with uv pip into a fresh venv and drive the installed CLI.

This proves the packaged console entry point, not a handcrafted shim. The
wheel is built by scripts/verify.sh before pytest; a bare `pytest` run builds
it on demand with `uv build`. No skip: a clean checkout must pass end to end.
"""

from __future__ import annotations

import os
import subprocess
import tomllib
import venv
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _version() -> str:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return pyproject["project"]["version"]


@pytest.fixture(scope="module")
def installed_cli(tmp_path_factory: pytest.TempPathFactory) -> Path:
    version = _version()
    dist = ROOT / "dist"
    wheel = dist / f"cc_models-{version}-py3-none-any.whl"
    if not wheel.exists():
        subprocess.run(
            ["uv", "build", "--out-dir", str(dist)],
            cwd=ROOT,
            check=True,
            capture_output=True,
        )
    assert wheel.exists(), f"uv build did not produce {wheel.name}"

    venv_dir = tmp_path_factory.mktemp("cli-venv")
    # symlinks=True: uv-managed Pythons resolve libpython via a rpath relative
    # to the binary, so a copied interpreter aborts on startup.
    venv.create(venv_dir, with_pip=False, symlinks=True)
    py = venv_dir / "bin" / "python"

    # Real install path: uv resolves and installs the wheel's declared
    # dependencies (textual) into the venv, exactly like an end user would.
    env = {k: v for k, v in os.environ.items() if k != "UV_INDEX"}
    subprocess.run(
        ["uv", "pip", "install", "--python", str(py), str(wheel)],
        cwd=ROOT,
        check=True,
        capture_output=True,
        env=env,
    )

    cli = venv_dir / "bin" / "cc-models"
    assert cli.exists(), "installed wheel did not create the cc-models console script"
    return cli


def _run(cli: Path, args: list[str], home: Path) -> subprocess.CompletedProcess[str]:
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(home),
        "TMPDIR": str(home),
    }
    return subprocess.run(
        [str(cli), *args], check=True, capture_output=True, text=True, env=env, cwd=str(home)
    )


def test_installed_version(installed_cli: Path, tmp_path: Path) -> None:
    result = _run(installed_cli, ["--version"], tmp_path)
    assert result.stdout.strip() == _version()


def test_installed_help_lists_subcommands(installed_cli: Path, tmp_path: Path) -> None:
    result = _run(installed_cli, ["--help"], tmp_path)
    assert "list" in result.stdout
    assert "apply" in result.stdout


def test_installed_demo_list_shows_catalog(installed_cli: Path, tmp_path: Path) -> None:
    """Literal catalog evidence from the packaged CLI, offline, isolated HOME."""
    result = _run(installed_cli, ["--demo", "list"], tmp_path)
    assert "--demo" in result.stdout
    assert "claude-opus-4-6" in result.stdout
    assert "custom-proxy-model" in result.stdout
    assert "Opus 演示" in result.stdout
