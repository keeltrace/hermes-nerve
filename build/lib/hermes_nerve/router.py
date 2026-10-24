"""Local adaptive relevance router for the Nerve nervous system.

The router never calls a provider. It converts structured Hermes events into a
small significance assessment so remote Jev is used when a new judgment could
plausibly alter the trajectory, not after a fixed number of events.

v0.2.1 adds stable semantic fingerprints for decision leases and repeated
failure episodes. Runtime/version counters are intentionally excluded from the
fingerprint: a lease is invalidated by a material state change, not merely by
another event arriving.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

CRITICAL_TYPES = {
    "COMPLETION_CANDIDATE",
    "CONSEQUENTIAL_ACTION",
    "IRREVERSIBLE_ACTION",
    "HUMAN_ESCALATION",
    "REPEATED_FAILURE",
    "STRATEGY_CHANGE",
    "RECOVERY",
    "HIGH_MATERIALITY_DECISION",
}

DECISION_TYPES = {
    "DECISION",
    "STRATEGY",
    "ROUTING",
    "RECOVERY",
    "COMPLETION",
    "COMPLETION_CANDIDATE",
    "COMMIT",
    "PRIORITIZATION",
    "HUMAN_ESCALATION",
    "HIGH_MATERIALITY_DECISION",
}

READ_ONLY_HINTS = {
    "read", "get", "list", "search", "find", "status", "show", "inspect", "fetch",
    "grep", "rg", "cat", "pwd", "head", "tail", "diff",
}

MUTATING_HINTS = {
    "write", "edit", "update", "delete", "remove", "create", "commit", "push", "merge",
    "deploy", "restart", "stop", "send", "post", "publish", "install", "purchase", "apply",
}

_LONG_HEX_RE = re.compile(r"\b[0-9a-f]{12,}\b", re.IGNORECASE)
_UUID_RE = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b", re.IGNORECASE)
_NUMBER_RE = re.compile(r"\b\d{4,}\b")
_DURATION_RE = re.compile(r"\b\d+(?:\.\d+)?\s*(?:ms|s|sec|secs|seconds|minutes|min)\b", re.IGNORECASE)
_WHITESPACE_RE = re.compile(r"\s+")


@dataclass(frozen=True)
class RouteAssessment:
    worth_calling: bool
    score: float
    reasons: tuple[str, ...]
    critical: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "worth_calling": self.worth_calling,
            "score": round(self.score, 4),
            "reasons": list(self.reasons),
            "critical": self.critical,
        }


def canonical_fingerprint(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _normalize_text(value: str) -> str:
    text = str(value or "").strip()
    text = _UUID_RE.sub("<uuid>", text)
    text = _LONG_HEX_RE.sub("<hex>", text)
    text = _DURATION_RE.sub("<duration>", text)
    text = _NUMBER_RE.sub("<n>", text)
    return _WHITESPACE_RE.sub(" ", text)


def _normalize_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _normalize_value(v) for k, v in sorted(value.items(), key=lambda item: str(item[0]))}
    if isinstance(value, (list, tuple)):
        return [_normalize_value(v) for v in value]
    if isinstance(value, str):
        # Arguments should remain discriminating while ignoring whitespace churn.
        return _WHITESPACE_RE.sub(" ", value.strip())
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)


def tool_action_fingerprint(tool_name: str, args: dict[str, Any] | None = None) -> str:
    """Stable identity for a proposed tool action.

    Used by the local loop breaker before execution. It intentionally excludes
    tool-call IDs, timestamps, and turn/event counters.
    """
    return canonical_fingerprint({
        "tool_name": str(tool_name or "").strip().lower(),
        "args": _normalize_value(args or {}),
    })


def failure_fingerprint(
    *,
    tool_name: str,
    args: dict[str, Any] | None,
    status: str = "",
    result: str = "",
    error_message: str = "",
    exit_code: Any = None,
) -> str:
    """Stable identity for one repeated-failure episode.

    The error signature normalizes volatile IDs/large counters/durations so the
    same underlying failure is not treated as novel merely because a timestamp
    or timing value changed.
    """
    signature_source = str(error_message or result or "")[:4000]
    return canonical_fingerprint({
        "action": tool_action_fingerprint(tool_name, args),
        "status": str(status or "failure").strip().lower(),
        "exit_code": str(exit_code) if exit_code is not None else "",
        "error_signature": _normalize_text(signature_source.lower()),
    })


def decision_state_fingerprint(event: dict[str, Any]) -> str:
    """Semantic lease fingerprint shared by router and nervous runtime.

    ``state_version``/``decision_version`` are correlation and stale-response
    guards, not semantic evidence, so they are deliberately excluded.
    """
    event_type = str(event.get("type") or "OBSERVATION").strip().upper()
    if event_type in {"FAILURE", "REPEATED_FAILURE"}:
        event_type = "FAILURE_EPISODE"
    return canonical_fingerprint({
        "type": event_type,
        "goal": event.get("goal"),
        "strategy": event.get("strategy"),
        "hypothesis": event.get("hypothesis"),
        "choices": event.get("choices"),
        "hermes_decision": event.get("hermes_decision"),
        "contradiction": bool(event.get("contradiction")),
        "completion_candidate": bool(event.get("completion_candidate")),
        "tool_name": str(event.get("tool_name") or ""),
        "status": str(event.get("status") or ""),
        "failure_fingerprint": str(event.get("failure_fingerprint") or ""),
        "action_fingerprint": str(event.get("action_fingerprint") or ""),
        # Buckets preserve meaningful changes without invalidating on tiny float jitter.
        "materiality_bucket": round(_bounded_float(event.get("materiality"), 0.0), 1),
        "uncertainty_bucket": round(_bounded_float(event.get("uncertainty"), 0.0), 1),
        "consequence_bucket": round(_bounded_float(event.get("consequence"), 0.0), 1),
        "risk_bucket": round(_bounded_float(event.get("risk"), 0.0), 1),
    })


def _bounded_float(value: Any, default: float = 0.0) -> float:
    try:
        return min(1.0, max(0.0, float(value)))
    except (TypeError, ValueError):
        return default


def infer_tool_risk(tool_name: str, status: str = "") -> tuple[float, list[str]]:
    name = (tool_name or "").lower()
    reasons: list[str] = []
    if status.lower() in {"error", "failed", "blocked", "failure"}:
        reasons.append("tool-failure")
        return 0.72, reasons
    if any(token in name for token in MUTATING_HINTS):
        reasons.append("mutating-tool")
        return 0.68, reasons
    if any(token in name for token in READ_ONLY_HINTS):
        reasons.append("read-only-tool")
        return 0.10, reasons
    reasons.append("unknown-tool-risk")
    return 0.36, reasons


def assess_event(
    event: dict[str, Any],
    *,
    last_remote_fingerprint: str = "",
    call_threshold: float = 0.58,
) -> RouteAssessment:
    """Estimate whether another Jev opinion could materially matter now.

    This is intentionally state/significance based. Event counts are not part
    of the primary trigger; callers may still enforce a separate safety budget.
    Exact repeated failures are a special case: once one equivalent state was
    assessed remotely, retries are handled by local loop-breaking policy until
    materially new evidence appears.
    """
    event_type = str(event.get("type") or "OBSERVATION").strip().upper()
    critical = event_type in CRITICAL_TYPES
    reasons: list[str] = []
    score = 0.0

    materiality = _bounded_float(event.get("materiality"), 0.0)
    uncertainty = _bounded_float(event.get("uncertainty"), 0.0)
    novelty = _bounded_float(event.get("novelty"), 0.0)
    consequence = _bounded_float(event.get("consequence"), 0.0)
    risk = _bounded_float(event.get("risk"), 0.0)

    if event_type in DECISION_TYPES:
        score += 0.34
        reasons.append("accountable-decision")
    if critical:
        score += 0.32
        reasons.append("critical-event")
    if event_type in {"FAILURE", "REPEATED_FAILURE"}:
        score += 0.34
        reasons.append("failure")
    if bool(event.get("contradiction")):
        score += 0.34
        reasons.append("contradictory-evidence")
    if bool(event.get("strategy_changed")):
        score += 0.30
        reasons.append("strategy-changed")
    if bool(event.get("completion_candidate")) or event_type in {"COMPLETION", "COMPLETION_CANDIDATE"}:
        score += 0.36
        reasons.append("completion-pressure")
    if bool(event.get("repeated_failure")):
        score += 0.28
        reasons.append("repeated-failure")

    score += 0.22 * materiality
    score += 0.18 * uncertainty
    score += 0.15 * novelty
    score += 0.22 * consequence
    score += 0.18 * risk

    if materiality >= 0.65:
        reasons.append("high-materiality")
    if uncertainty >= 0.60:
        reasons.append("high-uncertainty")
    if novelty >= 0.65:
        reasons.append("high-novelty")
    if consequence >= 0.65:
        reasons.append("high-consequence")
    if risk >= 0.65:
        reasons.append("high-risk")

    if event_type == "TOOL_RESULT":
        tool_risk, tool_reasons = infer_tool_risk(str(event.get("tool_name") or ""), str(event.get("status") or ""))
        score += 0.20 * tool_risk
        reasons.extend(tool_reasons)
        if tool_risk <= 0.12 and not bool(event.get("contradiction")):
            score -= 0.22
            reasons.append("stable-introspection")

    choices = event.get("choices")
    if isinstance(choices, list) and len({str(x) for x in choices}) >= 2:
        score += 0.22
        reasons.append("bounded-alternatives")

    fingerprint = decision_state_fingerprint(event)
    same_state = bool(last_remote_fingerprint and fingerprint == last_remote_fingerprint)
    repeat_count = int(event.get("failure_repeat_count") or 0) if event_type in {"FAILURE", "REPEATED_FAILURE"} else 0
    exact_repeated_failure = bool(same_state and repeat_count >= 2 and event.get("failure_fingerprint"))
    if same_state and (not critical or exact_repeated_failure):
        score -= 0.65 if exact_repeated_failure else 0.42
        reasons.append("semantic-hysteresis")
        if exact_repeated_failure:
            reasons.append("repeated-failure-dedup")
            critical = False

    if bool(event.get("suppress_remote")):
        score = 0.0
        critical = False
        reasons.append(str(event.get("suppress_reason") or "local-suppression"))

    score = min(1.0, max(0.0, score))
    return RouteAssessment(
        worth_calling=critical or score >= min(1.0, max(0.0, float(call_threshold))),
        score=score,
        reasons=tuple(dict.fromkeys(reasons)),
        critical=critical,
    )
