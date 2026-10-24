"""Load Nerve's packaged plaintext policy tables.

The security policy's protected-path and blocked-command DATA lives in
auditable plaintext tables under ``hermes_nerve/policy/`` so a reviewer can
audit every protective entry without reading code. The loader guarantees the
classification engine never runs against a silently weakened table: a
missing, unreadable, empty, or duplicate-entry table raises
:class:`TableLoadError` at import time, so the plugin fails loudly instead
of degrading a protection.

Tables resolve relative to this file's installed location, NOT via an
absolute package import: Hermes loads installed plugins under a
loader-chosen top-level module name (never ``hermes_nerve``), so the only
portable address for plugin data is the loader module's own path. Plugins
install as real directories (the validator probes a directory too), which
this resolution requires.
"""
from __future__ import annotations

from pathlib import Path
from typing import Tuple

_POLICY_DIR = Path(__file__).resolve().parent / "policy"


class TableLoadError(RuntimeError):
    """A packaged policy table is missing, malformed, or empty."""


def parse_table(text: str, name: str = "<policy table>") -> Tuple[str, ...]:
    """Parse table text into entries: one per line, ``#`` comments, stripped.

    Order is preserved for audit; duplicate entries are rejected so an
    accidental double-listing cannot hide behind set semantics.
    """
    entries: list[str] = []
    seen: set[str] = set()
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line in seen:
            raise TableLoadError(f"policy table {name!r} lists {line!r} more than once")
        seen.add(line)
        entries.append(line)
    if not entries:
        raise TableLoadError(f"policy table {name!r} is empty")
    return tuple(entries)


def load_table(name: str) -> Tuple[str, ...]:
    """Read a packaged table from the ``policy/`` directory beside this module."""
    try:
        text = (_POLICY_DIR / name).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise TableLoadError(f"policy table {name!r} could not be loaded: {exc}") from exc
    return parse_table(text, name)
