"""Privacy-minimized evidence ledger for anchors, shadow plans, and rehydration.

The ledger is intentionally not a second transcript. By default it stores redacted
content only for evidence that may need rehydration, plus hashes/provenance. Tool
observation can be disabled through plugin config.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .privacy import canonical_hash, redact
from .paths import hermes_home
from .jsonl import append_jsonl, read_jsonl

_configured_enabled = True
_configured_detail = "sanitized"
_current_session_id = ""


def configure(*, enabled: Any = True, detail: Any = "sanitized") -> None:
    global _configured_enabled, _configured_detail
    _configured_enabled = bool(enabled)
    value = str(detail or "sanitized").strip().lower()
    _configured_detail = value if value in {"hash", "sanitized"} else "sanitized"


def enabled() -> bool:
    return _configured_enabled


def detail() -> str:
    return _configured_detail


def set_session(session_id: str | None) -> None:
    global _current_session_id
    _current_session_id = str(session_id or "")


def ledger_path() -> Path:
    explicit = os.getenv("HERMES_NERVE_CONTEXT_LEDGER")
    if explicit:
        return Path(explicit).expanduser()
    return hermes_home() / "nerve" / "context-ledger.jsonl"


def _write(record: dict[str, Any]) -> dict[str, Any]:
    if not enabled():
        return record
    append_jsonl(ledger_path(), record)
    return record


def _safe_content(content: str) -> str | None:
    if detail() != "sanitized":
        return None
    value = redact(str(content))
    return value if isinstance(value, str) else str(value)


def record_evidence(
    *,
    evidence_id: str,
    content: str,
    kind: str,
    recoverable: bool,
    metadata: dict[str, Any] | None = None,
    action: str = "OBSERVED",
    source: str = "context",
    session_id: str | None = None,
) -> dict[str, Any]:
    safe_metadata = redact(metadata or {})
    record: dict[str, Any] = {
        "schema": "hermes-nerve-evidence/v1",
        "event": "evidence",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "session_id": _current_session_id if session_id is None else str(session_id),
        "evidence_id": str(evidence_id),
        "kind": str(kind),
        "recoverable": bool(recoverable),
        "action": str(action),
        "source": str(source),
        "chars": len(str(content)),
        "content_sha256": canonical_hash(str(content)),
        "metadata": safe_metadata,
    }
    safe = _safe_content(str(content))
    if safe is not None:
        record["content"] = safe
    return _write(record)


def record_shadow_plan(*, contract: str, goal: str, decisions: list[dict[str, Any]], stats: dict[str, Any]) -> dict[str, Any]:
    record = {
        "schema": "hermes-nerve-shadow/v1",
        "event": "shadow_plan",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "session_id": _current_session_id,
        "contract": str(contract),
        "goal_sha256": canonical_hash(str(goal)),
        "decisions": redact(decisions),
        "stats": redact(stats),
    }
    return _write(record)


def _iter_records(path: Path | None = None) -> list[dict[str, Any]]:
    return read_jsonl(path or ledger_path())


def rehydrate(evidence_id: str) -> dict[str, Any]:
    wanted = str(evidence_id or "").strip()
    if not wanted:
        raise ValueError("evidence_id must be non-empty")
    for record in reversed(_iter_records()):
        if record.get("event") == "evidence" and record.get("evidence_id") == wanted:
            if "content" not in record:
                raise ValueError("evidence exists but ledger detail=hash does not retain rehydratable content")
            _write(
                {
                    "schema": "hermes-nerve-evidence-event/v1",
                    "event": "rehydrated",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "session_id": _current_session_id,
                    "evidence_id": wanted,
                }
            )
            return {
                "evidence_id": wanted,
                "content": record["content"],
                "kind": record.get("kind"),
                "recoverable": bool(record.get("recoverable")),
                "metadata": record.get("metadata") or {},
                "content_sha256": record.get("content_sha256"),
                "source": record.get("source"),
            }
    raise ValueError(f"evidence_id not found in ledger: {wanted}")


def stats(path: Path | None = None) -> dict[str, Any]:
    selected_path = path or ledger_path()
    rows = _iter_records(selected_path)
    evidence = [row for row in rows if row.get("event") == "evidence"]
    rehydrated = [row for row in rows if row.get("event") == "rehydrated"]
    shadows = [row for row in rows if row.get("event") == "shadow_plan"]
    return {
        "enabled": enabled(),
        "detail": detail(),
        "path": str(selected_path),
        "evidence_events": len(evidence),
        "shadow_plans": len(shadows),
        "rehydrations": len(rehydrated),
        "unique_evidence": len({str(row.get("evidence_id")) for row in evidence if row.get("evidence_id")}),
    }


def tool_is_recoverable(tool_name: str, args: dict[str, Any] | None = None) -> bool:
    """Conservative deterministic recoverability classifier for tool observations."""
    name = str(tool_name or "").strip().lower()
    if name.startswith(("read", "search", "list", "get", "web_", "browser_")):
        return True
    if name in {"tool_search", "tool_describe", "remote_worker_status", "remote_worker_result"}:
        return True
    if name in {"terminal", "execute_code"}:
        command = str((args or {}).get("command") or (args or {}).get("code") or "").strip().lower()
        readonly_prefixes = (
            "pwd", "ls", "find ", "grep ", "rg ", "cat ", "head ", "tail ", "sed -n",
            "git status", "git diff", "git log", "git show", "git branch", "git rev-parse",
        )
        return command.startswith(readonly_prefixes)
    return False


def observe_tool_call(*, tool_name: str, args: dict[str, Any], result: str, task_id: str = "", duration_ms: int = 0, **kwargs: Any) -> None:
    """Best-effort post_tool_call observer. It never calls Jev and never changes tool output."""
    if not enabled() or str(tool_name).startswith(("nerve_", "jev_")):
        return None
    payload = str(result or "")
    if not payload:
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    evidence_id = f"tool-{canonical_hash([tool_name, task_id, stamp, payload])[:16]}"
    record_evidence(
        evidence_id=evidence_id,
        content=payload,
        kind="tool_result",
        recoverable=tool_is_recoverable(tool_name, args),
        metadata={
            "tool_name": tool_name,
            "args": redact(args or {}),
            "task_id": task_id,
            "duration_ms": duration_ms,
            "tool_call_id": kwargs.get("tool_call_id"),
            "status": kwargs.get("status"),
        },
        action="OBSERVED",
        source="post_tool_call",
        # Hermes passes session_id to every post_tool_call hook, "" when the call has
        # no session. Only a host that omits the field falls back to the process-wide
        # value, which only the context engine sets.
        session_id=kwargs.get("session_id"),
    )
    return None


def report(path: Path | None = None) -> dict[str, Any]:
    """Aggregate shadow/evidence telemetry for threshold tuning."""
    selected_path = path or ledger_path()
    rows = _iter_records(selected_path)
    shadow_rows = [row for row in rows if row.get("event") == "shadow_plan"]
    action_counts: dict[str, int] = {}
    proposed_saved = 0
    for row in shadow_rows:
        stats_value = row.get("stats") if isinstance(row.get("stats"), dict) else {}
        try:
            proposed_saved += int(stats_value.get("proposed_reduced_chars") or 0)
        except (TypeError, ValueError):
            pass
        for decision in row.get("decisions") or []:
            if not isinstance(decision, dict):
                continue
            action = str(decision.get("proposed_action") or decision.get("action") or "UNKNOWN")
            action_counts[action] = action_counts.get(action, 0) + 1
    evidence_rows = [row for row in rows if row.get("event") == "evidence"]
    rehydrations = [row for row in rows if row.get("event") == "rehydrated"]
    compacted_ids = {
        str(row.get("evidence_id"))
        for row in evidence_rows
        if str(row.get("action") or "").upper() in {"ANCHOR", "DROP"}
    }
    rehydrated_ids = {str(row.get("evidence_id")) for row in rehydrations}
    demanded_after_compaction = len(compacted_ids & rehydrated_ids)
    return {
        **stats(selected_path),
        "shadow_action_counts": action_counts,
        "shadow_proposed_saved_chars": proposed_saved,
        "compacted_unique_evidence": len(compacted_ids),
        "rehydrated_compacted_evidence": demanded_after_compaction,
        "recovery_demand_rate": round(demanded_after_compaction / len(compacted_ids), 6) if compacted_ids else 0.0,
        "note": "Recovery demand is a tuning signal, not automatically a false-forget failure.",
    }
