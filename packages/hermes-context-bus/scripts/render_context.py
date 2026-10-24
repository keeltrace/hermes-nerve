#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from hermes_context_bus.projection import render_context_markdown


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("runtime/shared-context.md"),
    )
    args = parser.parse_args()

    payload = json.loads(args.input.read_text(encoding="utf-8"))
    board = payload.get("board", {})
    entries = board.get("entries", [])
    inboxes = payload.get("inboxes", {})
    rendered = render_context_markdown(entries, inboxes)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    tmp = args.output.with_suffix(args.output.suffix + ".tmp")
    tmp.write_text(rendered, encoding="utf-8")
    tmp.replace(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())