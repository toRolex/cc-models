"""Tests for scripts/gen_formula.py (formula generation and guards)."""

from __future__ import annotations

import hashlib
import importlib.util
import sys
import tomllib
from pathlib import Path

import pytest


def _load_gen_formula():
    path = Path(__file__).resolve().parent.parent / "scripts" / "gen_formula.py"
    spec = importlib.util.spec_from_file_location("gen_formula", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


gf = _load_gen_formula()

ROOT = Path(__file__).resolve().parent.parent


def _pyproject(tmp_path: Path, name: str = "cc_models") -> Path:
    path = tmp_path / "pyproject.toml"
    path.write_text(f'[project]\nname = "{name}"\nversion = "1.0"\n', encoding="utf-8")
    return path


def test_runtime_closure_matches_real_lock() -> None:
    locked = gf.load_locked_packages(ROOT / "uv.lock")
    names = gf.runtime_closure(ROOT / "pyproject.toml", locked)
    assert "textual" in names
    # dev-only tools must never leak into the formula resources
    assert "pytest" not in names
    assert "ruff" not in names
    assert "pyright" not in names
    assert "packaging" not in names
    # textual's linkify extra pulls linkify-it-py through markdown-it-py
    assert "linkify-it-py" in names
    assert "mdurl" in names


def test_runtime_closure_markers_and_extras(tmp_path: Path) -> None:
    pyproject = _pyproject(tmp_path)
    locked = {
        "cc_models": {
            "name": "cc_models",
            "version": "1.0",
            "dependencies": [
                {"name": "base"},
                {"name": "winonly", "marker": "sys_platform == 'win32'"},
                {"name": "mdpkg", "extra": ["linkify"]},
                {"name": "oldpy", "marker": "python_version < '3.13'"},
            ],
        },
        "base": {"name": "base", "version": "1.0", "dependencies": []},
        "winonly": {"name": "winonly", "version": "1.0", "dependencies": []},
        "oldpy": {"name": "oldpy", "version": "1.0", "dependencies": []},
        "mdpkg": {
            "name": "mdpkg",
            "version": "1.0",
            "dependencies": [{"name": "mdurl"}],
            "optional-dependencies": {"linkify": [{"name": "linkify"}]},
        },
        "mdurl": {"name": "mdurl", "version": "1.0", "dependencies": []},
        "linkify": {"name": "linkify", "version": "1.0", "dependencies": []},
    }
    names = gf.runtime_closure(pyproject, locked)
    assert names == ["base", "linkify", "mdpkg", "mdurl"]
    assert "winonly" not in names
    assert "oldpy" not in names


def test_formula_text_escapes_ruby_interpolation() -> None:
    resources = [gf.FormulaResource("demo", "https://example.com/demo.tar.gz", "ab" * 32)]
    text = gf.formula_text(
        {"description": "测试工具"},
        "https://github.com/toRolex/cc-models/releases/download/v0.1.0/cc_models-0.1.0.tar.gz",
        "cd" * 32,
        resources,
    )
    assert 'shell_output("#{bin}/cc-models --version")' in text
    assert "<built-in function bin>" not in text
    assert text.count('depends_on "python@3.13"') == 1
    assert 'resource "demo" do' in text


def test_collect_dist_artifacts_rejects_stale_version(tmp_path: Path) -> None:
    stale = tmp_path / "cc_models-0.0.9.tar.gz"
    stale.write_bytes(b"old build")
    with pytest.raises(SystemExit, match="unexpected artifact"):
        gf.collect_dist_artifacts(tmp_path, "0.1.0")


def test_collect_dist_artifacts_requires_both_artifacts(tmp_path: Path) -> None:
    (tmp_path / "cc_models-0.1.0.tar.gz").write_bytes(b"sdist only")
    with pytest.raises(SystemExit, match="must contain exactly"):
        gf.collect_dist_artifacts(tmp_path, "0.1.0")


def test_collect_dist_artifacts_ignores_non_package_files(tmp_path: Path) -> None:
    (tmp_path / "cc_models-0.1.0.tar.gz").write_bytes(b"sdist")
    (tmp_path / "cc_models-0.1.0-py3-none-any.whl").write_bytes(b"wheel")
    (tmp_path / "checksums.txt").write_text("x\n", encoding="utf-8")
    artifacts = gf.collect_dist_artifacts(tmp_path, "0.1.0")
    assert {a.filename for a in artifacts} == {
        "cc_models-0.1.0.tar.gz",
        "cc_models-0.1.0-py3-none-any.whl",
    }


def test_generated_formula_and_checksums_are_consistent(tmp_path: Path, monkeypatch) -> None:
    """End to end: formula digest must equal the real sdist digest in checksums.txt."""
    dist = tmp_path / "dist"
    dist.mkdir()
    version = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"][
        "version"
    ]
    sdist = dist / f"cc_models-{version}.tar.gz"
    wheel = dist / f"cc_models-{version}-py3-none-any.whl"
    sdist.write_bytes(b"fake sdist bytes")
    wheel.write_bytes(b"fake wheel bytes")

    formula_path = tmp_path / "Formula" / "cc-models.rb"
    monkeypatch.setattr(gf, "FORMULA_PATH", formula_path)
    monkeypatch.setattr(
        gf,
        "sdist_for",
        lambda name, ver: (f"https://example.com/{name}-{ver}.tar.gz", "ee" * 32),
    )

    assert gf.main(["--dist-dir", str(dist)]) == 0

    text = formula_path.read_text(encoding="utf-8")
    locked = gf.load_locked_packages(ROOT / "uv.lock")
    for name in gf.runtime_closure(ROOT / "pyproject.toml", locked):
        assert f'resource "{name}" do' in text
        assert f'url "https://example.com/{name}-{locked[name]["version"]}.tar.gz"' in text

    # checksums.txt digests match the actual artifact files, byte for byte
    lines: dict[str, str] = {}
    for line in (dist / "checksums.txt").read_text(encoding="utf-8").splitlines():
        digest, filename = line.split("  ", 1)
        lines[filename] = digest
    assert set(lines) == {sdist.name, wheel.name}
    for filename, digest in lines.items():
        assert digest == hashlib.sha256((dist / filename).read_bytes()).hexdigest()

    # the formula's root sha256 is the digest of the identical sdist used for install
    sdist_digest = hashlib.sha256(sdist.read_bytes()).hexdigest()
    assert f'sha256 "{sdist_digest}"' in text


def test_committed_formula_matches_current_version_and_lock() -> None:
    """Structural guard on the committed formula.

    sdist bytes are not reproducible across builds, so the digest-vs-artifact
    check lives in test_generated_formula_and_checksums_are_consistent and the
    release workflow regenerates the formula from the exact uploaded artifacts.
    """
    formula = ROOT / "Formula/cc-models.rb"
    assert formula.exists(), "Formula/cc-models.rb missing; run scripts/gen_formula.py"
    version = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"][
        "version"
    ]
    text = formula.read_text(encoding="utf-8")
    assert f"releases/download/v{version}/cc_models-{version}.tar.gz" in text
    locked = gf.load_locked_packages(ROOT / "uv.lock")
    for name in gf.runtime_closure(ROOT / "pyproject.toml", locked):
        assert f'resource "{name}" do' in text
