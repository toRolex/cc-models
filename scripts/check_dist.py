#!/usr/bin/env python3
"""Verify built distributions (sdist + wheel) for release safety.

Stdlib only. Checks:
- wheel/sdist filename version matches pyproject version, no local segment
- sdist contents stay within the declared whitelist
- no local/machine metadata (VCS dirs, caches, venvs, direct_url.json)
- wheel contains the necessary cc_models modules

Usage: check_dist.py DIST_DIR
"""

from __future__ import annotations

import sys
import tarfile
import tomllib
import zipfile
from pathlib import Path

# Paths allowed inside the sdist (mirrors tool.hatch.build.targets.sdist.include).
SDIST_WHITELIST = (
    "src/",
    "tests/",
    "scripts/",
    "README.md",
    "LICENSE",
    "pyproject.toml",
    "uv.lock",
    ".gitignore",
    "PKG-INFO",  # hatchling standard sdist metadata
)

# Any archive path containing one of these substrings is local metadata.
FORBIDDEN_SUBSTRINGS = (
    ".jj/",
    ".git/",
    ".github/",
    ".agents/",
    ".pi/",
    ".venv/",
    "venv/",
    "__pycache__/",
    ".pytest_cache/",
    ".ruff_cache/",
    "direct_url.json",
    ".DS_Store",
)

NECESSARY_WHEEL_MODULES = (
    "cc_models/__init__.py",
    "cc_models/__main__.py",
    "cc_models/app.py",
    "cc_models/catalog.py",
    "cc_models/cli.py",
    "cc_models/domain.py",
    "cc_models/session.py",
    "cc_models/settings.py",
    "cc_models/store.py",
)


def _fail(errors: list[str], message: str) -> None:
    errors.append(message)


def check_version(name: str, expected_version: str, errors: list[str]) -> None:
    # Expect "cc_models-<version>.tar.gz" or "cc_models-<version>-py3-none-any.whl".
    body = name.removeprefix("cc_models-").removesuffix(".tar.gz")
    body = body.removesuffix("-py3-none-any.whl")
    if body != expected_version:
        _fail(
            errors,
            f"{name}: version segment {body!r} != expected {expected_version!r} "
            "(local version segments like +abc are rejected)",
        )


def check_paths(archive: str, names: list[str], errors: list[str]) -> None:
    for n in names:
        if n.startswith("/"):
            _fail(errors, f"{archive}: absolute path entry {n!r}")
        if ".." in n.split("/"):
            _fail(errors, f"{archive}: path traversal in entry {n!r}")
        for bad in FORBIDDEN_SUBSTRINGS:
            if bad in n:
                _fail(errors, f"{archive}: forbidden local metadata path {n!r} (contains {bad!r})")


def check_sdist_whitelist(
    archive: str, names: list[str], expected_version: str, errors: list[str]
) -> None:
    root = f"cc_models-{expected_version}"
    for n in names:
        if not n.startswith(root + "/"):
            _fail(errors, f"{archive}: sdist entry {n!r} outside expected root {root!r}")
            continue
        rest = n[len(root) + 1 :]
        if rest == "":
            continue
        # Only directory rules (ending with /) allow prefix matches; plain files must match exactly.
        if not any(rest == w or (w.endswith("/") and rest.startswith(w)) for w in SDIST_WHITELIST):
            _fail(errors, f"{archive}: sdist entry {n!r} outside whitelist {SDIST_WHITELIST}")


def check_wheel_scope(
    archive: str, names: list[str], expected_version: str, errors: list[str]
) -> None:
    allowed = ("cc_models/", f"cc_models-{expected_version}.dist-info/")
    for n in names:
        if not n.startswith(allowed):
            _fail(errors, f"{archive}: wheel entry {n!r} outside {allowed}")


def check_wheel_modules(archive: str, names: set[str], errors: list[str]) -> None:
    for module in NECESSARY_WHEEL_MODULES:
        if module not in names:
            _fail(errors, f"{archive}: missing necessary module {module!r}")


def check_dist_dir(dist_dir: Path, project_root: Path) -> list[str]:
    errors: list[str] = []
    pyproject = tomllib.loads((project_root / "pyproject.toml").read_text(encoding="utf-8"))
    expected_version = pyproject["project"]["version"]

    sdists = sorted(dist_dir.glob("*.tar.gz"))
    wheels = sorted(dist_dir.glob("*.whl"))
    if not sdists:
        _fail(errors, f"no sdist (*.tar.gz) found in {dist_dir}")
    if not wheels:
        _fail(errors, f"no wheel (*.whl) found in {dist_dir}")

    for sdist in sdists:
        check_version(sdist.name, expected_version, errors)
        with tarfile.open(sdist, "r:gz") as tf:
            names = tf.getnames()
        check_paths(sdist.name, names, errors)
        check_sdist_whitelist(sdist.name, names, expected_version, errors)

    for wheel in wheels:
        check_version(wheel.name, expected_version, errors)
        with zipfile.ZipFile(wheel) as zf:
            names = zf.namelist()
        check_paths(wheel.name, names, errors)
        check_wheel_scope(wheel.name, names, expected_version, errors)
        check_wheel_modules(wheel.name, set(names), errors)

    return errors


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: check_dist.py DIST_DIR", file=sys.stderr)
        return 2
    dist_dir = Path(argv[0])
    project_root = Path(__file__).resolve().parent.parent
    errors = check_dist_dir(dist_dir, project_root)
    if errors:
        for e in errors:
            print(f"FAIL: {e}", file=sys.stderr)
        print(f"check_dist: {len(errors)} error(s)", file=sys.stderr)
        return 1
    print("check_dist: OK (versions, paths, whitelist, no local metadata, necessary modules)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
