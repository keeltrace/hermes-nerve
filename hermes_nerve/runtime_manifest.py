"""Shared runtime-packaging manifest for Nerve installers and tools.

Every packaging path — Control Panel staged install, the runtime payload
installer, wheel data files, and identity checks — must agree on which files
constitute the shippable plugin. This module is that single source of truth.

It also provides the package-aware plugin-manifest locator: a source checkout
keeps ``plugin.yaml`` beside the package, while a wheel installs it under
``share/hermes-nerve``. Code that must read the manifest (CLI advanced
catalog, provenance identity, installers) resolves it through
:func:`find_manifest` instead of hard-coding a repository-relative path.
"""
from __future__ import annotations

import sys
from importlib import import_module
from pathlib import Path

# Root-level files shipped with every staged installation.
RUNTIME_ROOT_FILES = ("plugin.yaml", "__init__.py", "CONTRIBUTORS.md", "CHANGELOG.md")

# Package-relative asset globs included beyond Python modules. policy/*.txt is
# REQUIRED: hermes_nerve.policy_tables loads these tables at import time and
# deliberately raises TableLoadError when they are missing — a staged tree
# without them must never pass admission.
RUNTIME_ASSET_PATTERNS = (
    "web/*.html", "web/*.css", "web/*.js",
    "university/content/*.json",
    "observatory/assets/*.css", "observatory/assets/*.js",
    "research/*.md",
    "policy/*.txt",
)

# Manifest lookup candidates, most-specific first. Covers: source checkout
# (package dir sibling), checkout root, wheel-style ``share`` data files
# (package-relative and sys.prefix), and the venv data root.
_MANIFEST_CANDIDATES_FROM_PACKAGE = (
    Path("plugin.yaml"),
    Path("..") / "plugin.yaml",
    Path("share") / "hermes-nerve" / "plugin.yaml",
    Path("..") / "share" / "hermes-nerve" / "plugin.yaml",
)


def collect_runtime_files(package_source: Path) -> set[Path]:
    """Return the shippable file set (Python modules + declared assets).

    Private state is never shipped: dot-directories/dotfiles, ``__pycache__``,
    and any ``.bak`` path component are excluded.
    """
    package_source = Path(package_source)
    runtime: set[Path] = set(package_source.rglob("*.py"))
    for pattern in RUNTIME_ASSET_PATTERNS:
        runtime.update(package_source.glob(pattern))
    return {
        path for path in runtime
        if not any(part.startswith(".") or part == "__pycache__" or ".bak" in part
                   for part in path.relative_to(package_source).parts)
    }


def find_manifest(start: Path | None = None) -> Path:
    """Locate ``plugin.yaml`` for the installed/checked-out package.

    Raises ``FileNotFoundError`` naming every candidate when the manifest is
    absent — callers fail loudly instead of silently degrading.
    """
    if start is None:
        start = Path(import_module("hermes_nerve").__file__ or ".").resolve().parent
    start = Path(start).resolve()
    candidates = [start / rel for rel in _MANIFEST_CANDIDATES_FROM_PACKAGE]
    candidates.append(Path(sys.prefix) / "share" / "hermes-nerve" / "plugin.yaml")
    for candidate in candidates:
        try:
            if candidate.is_file():
                return candidate.resolve()
        except OSError:
            continue
    raise FileNotFoundError(
        "Nerve plugin manifest not found; looked in: "
        + ", ".join(str(c) for c in candidates))


__all__ = [
    "RUNTIME_ROOT_FILES",
    "RUNTIME_ASSET_PATTERNS",
    "collect_runtime_files",
    "find_manifest",
]
