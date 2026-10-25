from __future__ import annotations

import argparse
import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION_FILE = ROOT / "hermes_nerve" / "_version.py"
MANIFEST = ROOT / "plugin.yaml"


def source_version() -> str:
    tree = ast.parse(VERSION_FILE.read_text(encoding="utf-8"), filename=str(VERSION_FILE))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "__version__" for target in node.targets):
            value = ast.literal_eval(node.value)
            if not isinstance(value, str) or not value:
                raise ValueError("__version__ must be a non-empty string")
            return value
    raise ValueError("hermes_nerve._version.__version__ is missing")


def main() -> int:
    parser = argparse.ArgumentParser(description="Check or synchronize plugin.yaml from the single Python version source.")
    parser.add_argument("--write", action="store_true", help="update plugin.yaml instead of failing on drift")
    parser.add_argument("--check", action="store_true", help="check metadata only (the default)")
    args = parser.parse_args()
    version = source_version()
    text = MANIFEST.read_text(encoding="utf-8")
    match = re.search(r"(?m)^version:.*$", text)
    if not match:
        print("plugin.yaml must contain exactly one top-level version field", file=sys.stderr)
        return 2
    updated = text[:match.start()] + f'version: "{version}"' + text[match.end():]
    if updated != text:
        if not args.write:
            print(f"version drift: _version.py={version}; plugin.yaml differs (run scripts/sync_release_identity.py --write)", file=sys.stderr)
            return 1
        MANIFEST.write_text(updated, encoding="utf-8")
        print(f"plugin.yaml synchronized to {version}")
    else:
        print(f"version identity consistent: {version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
