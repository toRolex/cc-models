#!/usr/bin/env python3
"""Generate the Homebrew formula and checksums from actual build artifacts.

Single owner for version/hash generation. Uses the locked dependency graph
(uv.lock, universal resolution) plus `packaging` for PEP 508 marker/extra
evaluation; the PyPI JSON API is only used for canonical sdist urls/hashes.

Inputs:
- pyproject.toml: project version and description
- uv.lock: frozen dependency graph (names, versions, markers, extras)
- dist/: cc_models sdist and wheel built by `uv build`, matching pyproject version

Outputs:
- Formula/cc-models.rb: virtualenv formula, url = release sdist, one Resource
  per locked runtime dependency (sdist urls/digests from the PyPI JSON API)
- dist/checksums.txt: sha256 of every artifact the release publishes

Usage: gen_formula.py [--dist-dir DIST_DIR] [--check]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import tomllib
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from packaging.markers import Marker

PYPI_JSON = "https://pypi.org/pypi/{name}/{version}/json"
FORMULA_PATH = Path("Formula/cc-models.rb")

# Marker evaluation environment for the Homebrew target (python@3.13 on
# macOS). platform_machine is left to packaging defaults (rarely referenced).
BREW_ENV = {
    "os_name": "posix",
    "sys_platform": "darwin",
    "platform_system": "Darwin",
    "platform_python_implementation": "CPython",
    "implementation_name": "cpython",
    "python_version": "3.13",
    "python_full_version": "3.13.1",
}


@dataclass(frozen=True)
class DistArtifact:
    filename: str
    path: Path
    sha256: str


@dataclass(frozen=True)
class FormulaResource:
    name: str
    url: str
    sha256: str


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def collect_dist_artifacts(dist_dir: Path, expected_version: str) -> list[DistArtifact]:
    """Hash exactly one sdist and one wheel, both at expected_version.

    Stale or unexpected cc_models artifacts (wrong/extra version) fail loudly
    instead of silently feeding an old digest into the formula.
    """
    sdist_name = f"cc_models-{expected_version}.tar.gz"
    wheel_name = f"cc_models-{expected_version}-py3-none-any.whl"
    artifacts: list[DistArtifact] = []
    for path in sorted(dist_dir.iterdir()):
        if path.name in (sdist_name, wheel_name):
            artifacts.append(DistArtifact(path.name, path, sha256_file(path)))
        elif re.fullmatch(r"cc_models-.*\.(tar\.gz|whl)", path.name):
            raise SystemExit(
                f"gen_formula: unexpected artifact {path.name!r} in {dist_dir} "
                f"(expected {sdist_name} and {wheel_name}); clean the dist dir and rebuild"
            )
    if len(artifacts) != 2:
        raise SystemExit(
            f"gen_formula: {dist_dir} must contain exactly {sdist_name} and {wheel_name}; "
            "run `uv build --out-dir dist` first"
        )
    return artifacts


def load_locked_packages(uv_lock: Path) -> dict[str, dict]:
    lock = tomllib.loads(uv_lock.read_text(encoding="utf-8"))
    return {pkg["name"]: pkg for pkg in lock["package"]}


def _marker_active(marker: Marker | None, active_extras: frozenset[str]) -> bool:
    if marker is None:
        return True
    for extra in {"", *active_extras}:
        if marker.evaluate({**BREW_ENV, "extra": extra}):
            return True
    return False


def runtime_closure(pyproject: Path, locked: dict[str, dict]) -> list[str]:
    """Transitive runtime dependency names from the universal uv.lock graph.

    uv.lock edges carry `marker` (PEP 508 string) and `extra` (child extras
    that activate the edge); `[package.optional-dependencies]` holds the
    extra-gated dep lists. Markers are evaluated with `packaging` against the
    Homebrew python@3.13 environment. Dev-only packages never enter: the walk
    starts only at the root package's runtime dependencies.
    """
    py = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    root_name = py["project"]["name"]
    root_pkg = locked.get(root_name) or locked.get(root_name.replace("-", "_"))
    if root_pkg is None:
        raise SystemExit(
            f"gen_formula: root package {root_name} not found in uv.lock (run `uv lock`)"
        )
    todo: list[tuple[str, frozenset[str]]] = []
    active: dict[str, frozenset[str]] = {}

    def push(edges: list[dict], parent_extras: frozenset[str]) -> None:
        for edge in edges:
            marker = Marker(edge["marker"]) if "marker" in edge else None
            if not _marker_active(marker, parent_extras):
                continue
            todo.append((edge["name"], frozenset(edge.get("extra", ()))))

    push(root_pkg["dependencies"], frozenset())
    while todo:
        name, extras = todo.pop()
        merged = extras | active.get(name, frozenset())
        if name in active and merged == active[name]:
            continue
        active[name] = merged
        pkg = locked.get(name)
        if pkg is None:
            raise SystemExit(f"gen_formula: {name} not found in uv.lock (run `uv lock`)")
        edges: list[dict] = list(pkg.get("dependencies", ()))
        for extra, deps in pkg.get("optional-dependencies", {}).items():
            if extra in merged:
                edges.extend(deps)
        push(edges, merged)
    return sorted(active)


def sdist_for(name: str, version: str) -> tuple[str, str]:
    """Canonical files.pythonhosted.org sdist url + sha256 from the PyPI JSON API."""
    with urllib.request.urlopen(PYPI_JSON.format(name=name, version=version)) as resp:
        data = json.load(resp)
    for url_info in data["urls"]:
        if url_info["packagetype"] == "sdist":
            return url_info["url"], url_info["digests"]["sha256"]
    raise SystemExit(f"gen_formula: no sdist published for {name} {version}")


def formula_text(
    project: dict,
    sdist_url: str,
    sdist_sha256: str,
    resources: list[FormulaResource],
) -> str:
    resource_blocks = "".join(
        f'  resource "{r.name}" do\n    url "{r.url}"\n    sha256 "{r.sha256}"\n  end\n\n'
        for r in resources
    )
    desc = project["description"]
    return (
        "class CcModels < Formula\n"
        "  include Language::Python::Virtualenv\n\n"
        f'  desc "{desc}"\n'
        '  homepage "https://github.com/toRolex/cc-models"\n'
        f'  url "{sdist_url}"\n'
        f'  sha256 "{sdist_sha256}"\n\n'
        '  depends_on "python@3.13"\n\n'
        f"{resource_blocks}"
        "  def install\n"
        "    virtualenv_install_with_resources\n"
        "  end\n\n"
        "  test do\n"
        '    assert_match version.to_s, shell_output("#{bin}/cc-models --version")\n'
        "  end\n"
        "end\n"
    )


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist-dir", default="dist", type=Path)
    parser.add_argument("--check", action="store_true", help="fail if outputs would change")
    args = parser.parse_args(argv)

    root = Path(__file__).resolve().parent.parent
    pyproject_path = root / "pyproject.toml"
    project = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))["project"]
    version = project["version"]

    artifacts = collect_dist_artifacts(args.dist_dir, version)
    sdist = next((a for a in artifacts if a.filename.endswith(".tar.gz")), None)
    if sdist is None:
        raise SystemExit("gen_formula: no sdist in dist; the formula url needs a release sdist")

    locked = load_locked_packages(root / "uv.lock")
    names = runtime_closure(pyproject_path, locked)
    resources = []
    for name in names:
        pkg = locked[name]
        url, sha256 = sdist_for(name, pkg["version"])
        resources.append(FormulaResource(name, url, sha256))

    # The formula url must reference the release tag asset, not a local path.
    sdist_url = (
        f"https://github.com/toRolex/cc-models/releases/download/v{version}/{sdist.filename}"
    )
    text = formula_text(project, sdist_url, sdist.sha256, resources)

    checksums = "".join(f"{a.sha256}  {a.filename}\n" for a in artifacts)
    if args.check:
        ok = FORMULA_PATH.exists() and FORMULA_PATH.read_text(encoding="utf-8") == text
        print("gen_formula: formula up to date" if ok else "gen_formula: formula stale")
        return 0 if ok else 1

    FORMULA_PATH.parent.mkdir(exist_ok=True)
    FORMULA_PATH.write_text(text, encoding="utf-8")
    (args.dist_dir / "checksums.txt").write_text(checksums, encoding="utf-8")
    print(
        f"gen_formula: wrote {FORMULA_PATH} and {args.dist_dir / 'checksums.txt'} "
        f"(version {version}, {len(resources)} resources)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
