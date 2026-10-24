"""Copy only the plugin runtime, never a developer's whole checkout."""
from __future__ import annotations

from pathlib import Path
import shutil
import sys

ROOT_FILES = ("plugin.yaml", "__init__.py", "pyproject.toml", "README.md", "LICENSE",
              "CONTRIBUTORS.md", "CHANGELOG.md")
PACKAGES = ("hermes_nerve", "hermes_jev")
# Keep in sync with tool.setuptools.package-data in pyproject.toml.
ASSET_GLOBS = (
    "hermes_nerve/web/*.html", "hermes_nerve/web/*.css", "hermes_nerve/web/*.js",
    "hermes_nerve/research/*.md",
    "hermes_nerve/policy/*.txt",
)


def runtime_files(root: Path) -> list[Path]:
    selected = {root / name for name in ROOT_FILES}
    for package in PACKAGES:
        selected.update(path for path in (root / package).rglob("*.py")
                        if "__pycache__" not in path.relative_to(root).parts)
    for pattern in ASSET_GLOBS:
        selected.update(root.glob(pattern))
    # Match the GUI payload contract: ignored checkout-private Python and asset
    # files are not runtime merely because they match a language/asset suffix.
    selected = {path for path in selected
                if not any(part.startswith(".") or part == "__pycache__" or ".bak" in part
                           for part in path.relative_to(root).parts)}
    required = ROOT_FILES + ("hermes_nerve/__init__.py", "hermes_nerve/provenance.py", "hermes_jev/__init__.py")
    for name in required:
        if not (root / name).is_file():
            raise ValueError(f"Missing required runtime file: {name}")
    for source in sorted(selected):
        current = root
        for part in source.relative_to(root).parts:
            current = current / part
            if current.is_symlink():
                raise ValueError(f"Runtime payload symlinks are not accepted: {source.relative_to(root)}")
        if not source.is_file():
            raise ValueError(f"Runtime payload is not a regular file: {source.relative_to(root)}")
        if source.suffix == ".py":
            # Syntax validation only; never execute source to preflight it.
            compile(source.read_bytes(), str(source), "exec")
    return sorted(selected)


def install(root: Path, destination: Path) -> int:
    paths = runtime_files(root)
    for source in paths:
        target = destination / source.relative_to(root)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    return len(paths)


if __name__ == "__main__":
    if sys.argv[1] == "--check":
        count = len(runtime_files(Path(sys.argv[2])))
        print(f"Runtime payload preflight passed: {count} files")
    else:
        count = install(Path(sys.argv[1]), Path(sys.argv[2]))
        print(f"Installed {count} runtime payload files (checkout-private files excluded)")
