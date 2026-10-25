from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any


def _text(value: Any) -> str:
    return str(value if value is not None else "").strip()


def _ordered(items: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return sorted(
        items,
        key=lambda item: (
            float(item.get("created_at") or 0),
            _text(item.get("entry_id") or item.get("message_id")),
        ),
    )


def render_context_markdown(
    board_entries: Sequence[Mapping[str, Any]],
    inboxes: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    generated_at: str | None = None,
) -> str:
    stamp = generated_at or datetime.now(timezone.utc).isoformat()
    lines = [
        "# Hermes Shared Context",
        "",
        f"Generated: {stamp}",
        "",
        "> Derived view only. MegaMCP blackboard/inboxes are authoritative.",
        "",
        "## Shared board",
        "",
    ]

    entries = _ordered(board_entries)
    if not entries:
        lines.append("_No shared board entries._")
    else:
        for entry in entries:
            title = _text(entry.get("title")) or "Untitled"
            author = _text(entry.get("author")) or "unknown"
            body = _text(entry.get("body"))
            lines.extend([f"### {title}", f"Author: {author}", "", body, ""])

    lines.extend(["## Agent inboxes", ""])
    for agent_id in sorted(inboxes):
        messages = _ordered(inboxes[agent_id])
        lines.extend([f"### {agent_id}", ""])
        if not messages:
            lines.extend(["_No messages._", ""])
            continue
        for message in messages:
            sender = _text(message.get("from_id")) or "unknown"
            body = _text(message.get("body"))
            thread = _text(message.get("thread_id"))
            suffix = f" | thread {thread}" if thread else ""
            lines.append(f"- from {sender}{suffix}: {body}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"