"""Negative/positive tests for scripts/check_dist.py (stdlib artifact verification)."""

from __future__ import annotations

import importlib.util
import io
import tarfile
import tomllib
import zipfile
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "check_dist.py"


def _load_check_dist():
    spec = importlib.util.spec_from_file_location("check_dist", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


check_dist = _load_check_dist()

with (_SCRIPT.parent.parent / "pyproject.toml").open("rb") as f:
    VERSION = tomllib.load(f)["project"]["version"]

MODULES = check_dist.NECESSARY_WHEEL_MODULES


def _make_good_wheel(path: Path) -> None:
    with zipfile.ZipFile(path, "w") as zf:
        for m in MODULES:
            zf.writestr(m, "# module\n")
        zf.writestr(f"cc_models-{VERSION}.dist-info/METADATA", "Name: cc-models\n")


def _make_good_sdist(path: Path) -> None:
    with tarfile.open(path, "w:gz") as tf:
        for name in ("src/cc_models/__init__.py", "tests/test_x.py", "README.md", "pyproject.toml"):
            _add(tf, f"cc_models-{VERSION}/{name}")


def _add(tar: tarfile.TarFile, name: str) -> None:
    info = tarfile.TarInfo(name)
    info.size = 0
    tar.addfile(info, io.BytesIO(b""))


def _dist(tmp_path: Path) -> Path:
    # Neutral directory name so no error-substring assertion can match the path itself.
    dist = tmp_path / "dist"
    dist.mkdir(exist_ok=True)
    return dist


def _good_pair(dist: Path) -> None:
    _make_good_wheel(dist / f"cc_models-{VERSION}-py3-none-any.whl")
    _make_good_sdist(dist / f"cc_models-{VERSION}.tar.gz")


def _sdist_with(dist: Path, extra: list[str]) -> None:
    with tarfile.open(dist / f"cc_models-{VERSION}.tar.gz", "w:gz") as tf:
        for name in ("src/cc_models/__init__.py", "tests/test_x.py", "README.md", "pyproject.toml"):
            _add(tf, f"cc_models-{VERSION}/{name}")
        for name in extra:
            _add(tf, name)


def _wheel_with(dist: Path, extra: list[str]) -> None:
    with zipfile.ZipFile(dist / f"cc_models-{VERSION}-py3-none-any.whl", "w") as zf:
        for m in MODULES:
            zf.writestr(m, "# module\n")
        zf.writestr(f"cc_models-{VERSION}.dist-info/METADATA", "Name: cc-models\n")
        for name in extra:
            zf.writestr(name, "")


def _check(dist: Path) -> list[str]:
    return check_dist.check_dist_dir(dist, Path(__file__).resolve().parent.parent)


def test_good_artifacts_pass(tmp_path: Path) -> None:
    dist = _dist(tmp_path)
    _good_pair(dist)
    assert _check(dist) == []


def test_local_metadata_rejected(tmp_path: Path) -> None:
    dist = _dist(tmp_path)
    _wheel_with(dist, [".jj/checkout/meta"])
    _make_good_sdist(dist / f"cc_models-{VERSION}.tar.gz")
    assert any("forbidden local metadata" in e for e in _check(dist))


def test_sdist_prefix_tricks_rejected(tmp_path: Path) -> None:
    dist = _dist(tmp_path)
    _sdist_with(
        dist,
        [
            f"cc_models-{VERSION}/README.md.secret",
            f"cc_models-{VERSION}/pyproject.toml.backup",
            f"cc_models-{VERSION}/uv.lock.credentials",
        ],
    )
    _make_good_wheel(dist / f"cc_models-{VERSION}-py3-none-any.whl")
    errors = _check(dist)
    for trick in ("README.md.secret", "pyproject.toml.backup", "uv.lock.credentials"):
        assert any("outside whitelist" in e and trick in e for e in errors)


def test_sdist_traversal_rejected(tmp_path: Path) -> None:
    dist = _dist(tmp_path)
    _sdist_with(dist, [f"cc_models-{VERSION}/src/../../credentials.json"])
    _make_good_wheel(dist / f"cc_models-{VERSION}-py3-none-any.whl")
    assert any("traversal" in e for e in _check(dist))


def test_sdist_absolute_path_rejected(tmp_path: Path) -> None:
    dist = _dist(tmp_path)
    _sdist_with(dist, ["/etc/credentials.json"])
    _make_good_wheel(dist / f"cc_models-{VERSION}-py3-none-any.whl")
    assert any("absolute" in e for e in _check(dist))


def test_sdist_wrong_root_rejected(tmp_path: Path) -> None:
    dist = _dist(tmp_path)
    _sdist_with(dist, ["other-entry/src/cc_models/__init__.py"])
    _make_good_wheel(dist / f"cc_models-{VERSION}-py3-none-any.whl")
    assert any("outside" in e for e in _check(dist))


def test_wheel_traversal_and_env_rejected(tmp_path: Path) -> None:
    dist = _dist(tmp_path)
    _wheel_with(dist, ["../../credentials.json", ".env"])
    _make_good_sdist(dist / f"cc_models-{VERSION}.tar.gz")
    errors = _check(dist)
    assert any("traversal" in e for e in errors)
    assert any("outside" in e and ".env" in e for e in errors)


def test_wheel_extraroot_rejected(tmp_path: Path) -> None:
    dist = _dist(tmp_path)
    _wheel_with(dist, ["extraroot/credentials.json"])
    _make_good_sdist(dist / f"cc_models-{VERSION}.tar.gz")
    assert any("outside" in e and "extraroot" in e for e in _check(dist))


def test_sdist_entry_outside_whitelist_rejected(tmp_path: Path) -> None:
    dist = _dist(tmp_path)
    _sdist_with(dist, [f"cc_models-{VERSION}/.venv/lib.py"])
    _make_good_wheel(dist / f"cc_models-{VERSION}-py3-none-any.whl")
    assert any("outside whitelist" in e for e in _check(dist))


def test_local_version_segment_rejected(tmp_path: Path) -> None:
    dist = _dist(tmp_path)
    _make_good_wheel(dist / f"cc_models-{VERSION}+local.build-py3-none-any.whl")
    _make_good_sdist(dist / f"cc_models-{VERSION}.tar.gz")
    assert any("version segment" in e for e in _check(dist))


def test_missing_necessary_module_rejected(tmp_path: Path) -> None:
    dist = _dist(tmp_path)
    with zipfile.ZipFile(dist / f"cc_models-{VERSION}-py3-none-any.whl", "w") as zf:
        for m in MODULES[1:]:  # omit cc_models/__init__.py
            zf.writestr(m, "")
        zf.writestr(f"cc_models-{VERSION}.dist-info/METADATA", "Name: cc-models\n")
    _make_good_sdist(dist / f"cc_models-{VERSION}.tar.gz")
    assert any("missing necessary module" in e for e in _check(dist))


def test_missing_archives_rejected(tmp_path: Path) -> None:
    errors = _check(_dist(tmp_path))
    assert any("no sdist" in e for e in errors) and any("no wheel" in e for e in errors)


def test_cli_exit_codes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert check_dist.main([str(tmp_path)]) == 1
    assert check_dist.main([]) == 2
    capsys.readouterr()
