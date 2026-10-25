#!/usr/bin/env python3
from __future__ import annotations

import argparse
import shutil
from pathlib import Path


def copy_plugin(source: Path, hermes_home: Path) -> Path:
    destination = hermes_home / "plugins" / "hermes-context-bus"
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(
        source,
        destination,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache"),
    )
    return destination


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Install HermesContextBus as a Hermes user plugin."
    )
    parser.add_argument(
        "--hermes-home",
        type=Path,
        default=Path.home() / ".hermes",
        help="Hermes home (default: ~/.hermes)",
    )
    parser.add_argument(
        "--source",
        type=Path,
        default=Path(__file__).resolve().parents[1],
    )
    args = parser.parse_args()

    source = args.source.resolve()
    hermes_home = args.hermes_home.expanduser().resolve()
    if not (source / "plugin.yaml").is_file() or not (source / "__init__.py").is_file():
        raise SystemExit(f"invalid plugin source: {source}")

    destination = copy_plugin(source, hermes_home)
    print(f"Installed plugin: {destination}")
    print("")
    print("Default backend: auto (MegaMCP when authorized, otherwise local SQLite/WAL).")
    print("No MCP configuration is required for the local shared forum.")
    print("Optional MegaMCP reference: config/hermes-config-snippet.yaml")
    print("")
    print("Then run: hermes plugins doctor ~/.hermes/plugins/hermes-context-bus --ci")
    print("Restart the Hermes gateway only after Doctor passes.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())