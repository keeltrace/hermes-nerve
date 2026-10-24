"""Jev-assisted context-value control for explicit evidence units.

v0.1.5.5 treats Jev as a semantic context governor, not a summarizer. Jev
estimates fuzzy properties (future need, exactness need, supersession, conflict)
and deterministic local policy decides whether evidence is kept exact, pinned,
anchored, or dropped. Kept evidence is never model-rewritten.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Callable

from . import ledger, lifecycle
from .engine import DecisionEngine
from .privacy import canonical_hash
from .provenance import execution_provenance

_ALLOWED_KINDS = {
    "user_text",
    "assistant_text",
    "tool_call",
    "tool_result",
    "observation",
    "artifact",
    "other",
}
_TEXT_KINDS = {"user_text", "assistant_text"}
_ACTIONS = {"KEEP_EXACT", "PIN", "ANCHOR", "DROP"}
_BATCH_SIZE = 4  # 4 noul questions per candidate = Jev's 16-question ceiling.
MAX_ITEMS = 48
_MAX_ITEMS = MAX_ITEMS  # Backward-compatible private alias; engine code uses MAX_ITEMS.

_configured_preview_chars = 1200
_configured_anchor_chars = 220
_configured_preserve_tail = 4
_configured_mode = "shadow"
_configured_drop_max_needed = 0.20
_configured_drop_max_exact = 0.20
_configured_drop_min_superseded = 0.75
_configured_anchor_max_needed = 0.55
_configured_anchor_max_exact = 0.45
_configured_conflict_pin_min = 0.70


def _bounded_int(value: Any, default: int, low: int, high: int) -> int:
    try:
        parsed = int(value) if value is not None else default
    except (TypeError, ValueError):
        parsed = default
    return min(high, max(low, parsed))


def _bounded_float(value: Any, default: float, low: float = 0.0, high: float = 1.0) -> float:
    try:
        parsed = float(value) if value is not None else default
    except (TypeError, ValueError):
        parsed = default
    if not math.isfinite(parsed):
        parsed = default
    return min(high, max(low, parsed))


def configure(
    *,
    preview_chars: Any = None,
    stub_chars: Any = None,
    anchor_chars: Any = None,
    preserve_tail: Any = None,
    min_confidence: Any = None,  # accepted for 0.1.5.x compatibility; action-specific policy supersedes it
    mode: Any = None,
    drop_max_needed: Any = None,
    drop_max_exact: Any = None,
    drop_min_superseded: Any = None,
    anchor_max_needed: Any = None,
    anchor_max_exact: Any = None,
    conflict_pin_min: Any = None,
) -> None:
    global _configured_preview_chars, _configured_anchor_chars, _configured_preserve_tail, _configured_mode
    global _configured_drop_max_needed, _configured_drop_max_exact, _configured_drop_min_superseded
    global _configured_anchor_max_needed, _configured_anchor_max_exact, _configured_conflict_pin_min

    _configured_preview_chars = _bounded_int(preview_chars, 1200, 160, 4000)
    selected_anchor = anchor_chars if anchor_chars is not None else stub_chars
    _configured_anchor_chars = _bounded_int(selected_anchor, 220, 40, 1200)
    _configured_preserve_tail = _bounded_int(preserve_tail, 4, 0, 12)
    selected_mode = str(mode or "apply").strip().lower()
    _configured_mode = selected_mode if selected_mode in {"apply", "shadow"} else "apply"
    _configured_drop_max_needed = _bounded_float(drop_max_needed, 0.20)
    _configured_drop_max_exact = _bounded_float(drop_max_exact, 0.20)
    _configured_drop_min_superseded = _bounded_float(drop_min_superseded, 0.75)
    _configured_anchor_max_needed = _bounded_float(anchor_max_needed, 0.55)
    _configured_anchor_max_exact = _bounded_float(anchor_max_exact, 0.45)
    _configured_conflict_pin_min = _bounded_float(conflict_pin_min, 0.70)


@dataclass(frozen=True)
class EvidenceItem:
    id: str
    kind: str
    content: str
    recoverable: bool
    pinned: bool
    metadata: dict[str, Any]


@dataclass(frozen=True)
class CurationDecision:
    id: str
    action: str
    proposed_action: str
    basis: str
    lease: str
    semantic: dict[str, float]
    recoverable: bool

    def as_dict(self) -> dict[str, Any]:
        certainty = {
            key: round(abs(float(value) - 0.5) * 2.0, 6)
            for key, value in self.semantic.items()
        }
        return {
            "id": self.id,
            "action": self.action,
            "proposed_action": self.proposed_action,
            "basis": self.basis,
            "lease": self.lease,
            "recoverable": self.recoverable,
            "semantic": {key: round(float(value), 6) for key, value in self.semantic.items()},
            "semantic_certainty": certainty,
        }


def _normalize_items(raw_items: Any) -> list[EvidenceItem]:
    if not isinstance(raw_items, list) or not raw_items:
        raise ValueError("items must be a non-empty array")
    if len(raw_items) > _MAX_ITEMS:
        raise ValueError(f"items may contain at most {_MAX_ITEMS} evidence units")

    seen: set[str] = set()
    out: list[EvidenceItem] = []
    for index, raw in enumerate(raw_items):
        if not isinstance(raw, dict):
            raise ValueError(f"items[{index}] must be an object")
        item_id = str(raw.get("id") or "").strip()
        if not item_id:
            raise ValueError(f"items[{index}].id must be non-empty")
        if item_id in seen:
            raise ValueError(f"duplicate evidence id: {item_id}")
        seen.add(item_id)

        kind = str(raw.get("kind") or "other").strip().lower()
        if kind not in _ALLOWED_KINDS:
            raise ValueError(f"items[{index}].kind must be one of {', '.join(sorted(_ALLOWED_KINDS))}")
        content = raw.get("content")
        if not isinstance(content, str):
            raise ValueError(f"items[{index}].content must be a string")
        metadata = raw.get("metadata") or {}
        if not isinstance(metadata, dict):
            raise ValueError(f"items[{index}].metadata must be an object when provided")
        out.append(
            EvidenceItem(
                id=item_id,
                kind=kind,
                content=content,
                recoverable=bool(raw.get("recoverable", False)),
                pinned=bool(raw.get("pinned", False)),
                metadata=metadata,
            )
        )
    return out


def _preview(item: EvidenceItem, preview_chars: int) -> dict[str, Any]:
    content = item.content
    if len(content) <= preview_chars:
        shown = content
        truncated = False
    else:
        head = max(1, int(preview_chars * 0.72))
        tail = max(1, preview_chars - head)
        shown = content[:head] + "\n[…preview gap…]\n" + content[-tail:]
        truncated = True
    return {
        "id": item.id,
        "kind": item.kind,
        "recoverable": item.recoverable,
        "chars": len(content),
        "preview_truncated": truncated,
        "content_preview": shown,
        "metadata": item.metadata,
    }


def _lease_for(item: EvidenceItem) -> str:
    explicit = str(item.metadata.get("lease") or "").strip().lower()
    if explicit:
        return explicit
    if item.kind in _TEXT_KINDS:
        return "task_lifetime"
    lowered = item.content.lower()
    failure_markers = ("error", "exception", "traceback", "assertion", "failed", "failure", "401", "403", "500")
    if not item.recoverable and any(marker in lowered for marker in failure_markers):
        return "until_verification_pass"
    return "task"


def _questions(item: EvidenceItem, slot: int) -> dict[str, dict[str, Any]]:
    ref = f"evidence id={item.id!r}, kind={item.kind!r}"
    return {
        f"needed_{slot}": {
            "type": "noul",
            "instructions": (
                f"For {ref}, estimate whether completing the stated ongoing goal is likely to require information "
                "from this evidence again. True means likely useful later; false means low expected future value."
            ),
            "criteria": {"true": "Likely needed again", "false": "Unlikely to be needed again"},
        },
        f"exact_{slot}": {
            "type": "noul",
            "instructions": (
                f"For {ref}, estimate whether its exact original contents (not merely provenance or the fact it existed) "
                "are likely to be needed to complete the goal correctly."
            ),
            "criteria": {"true": "Exact bytes/details matter", "false": "An anchor/provenance is sufficient"},
        },
        f"superseded_{slot}": {
            "type": "noul",
            "instructions": (
                f"For {ref}, estimate whether later state/evidence has made this observation stale, redundant, or superseded."
            ),
            "criteria": {"true": "Likely stale/superseded", "false": "Still current/nonredundant"},
        },
        f"conflict_{slot}": {
            "type": "noul",
            "instructions": (
                f"For {ref}, estimate whether this evidence participates in an unresolved contradiction with other current evidence. "
                "True means preserve it until the contradiction is reconciled."
            ),
            "criteria": {"true": "Unresolved contradiction", "false": "No material unresolved contradiction"},
        },
    }


def _noul(answer: Any, default: float = 0.5) -> float:
    if not isinstance(answer, dict):
        return default
    try:
        value = float(answer.get("noul"))
    except (TypeError, ValueError):
        return default
    if not math.isfinite(value):
        return default
    return max(0.0, min(1.0, value))


def _anchor(item: EvidenceItem, anchor_chars: int) -> str:
    prefix = item.content[:anchor_chars].replace("\x00", "")
    tool_name = str(item.metadata.get("tool_name") or "")
    pointer = str(item.metadata.get("recovery_pointer") or f"nerve_context_rehydrate:{item.id}")
    header = (
        f"[NERVE_CONTEXT_ANCHOR id={item.id} kind={item.kind} chars={len(item.content)} "
        f"sha256={canonical_hash(item.content)[:16]} recoverable={str(item.recoverable).lower()}"
        + (f" tool={tool_name}" if tool_name else "")
        + f" recovery={pointer}]"
    )
    if not prefix:
        return header
    omitted = max(0, len(item.content) - len(prefix))
    return f"{header}\n{prefix}\n[… {omitted} original chars omitted; use nerve_context_rehydrate with evidence_id={item.id!r} …]"


def _policy(overrides: dict[str, Any] | None = None) -> dict[str, float]:
    raw = overrides or {}
    if not isinstance(raw, dict):
        raise ValueError("policy must be an object when provided")
    return {
        "drop_max_needed": _bounded_float(raw.get("drop_max_needed"), _configured_drop_max_needed),
        "drop_max_exact": _bounded_float(raw.get("drop_max_exact"), _configured_drop_max_exact),
        "drop_min_superseded": _bounded_float(raw.get("drop_min_superseded"), _configured_drop_min_superseded),
        "anchor_max_needed": _bounded_float(raw.get("anchor_max_needed"), _configured_anchor_max_needed),
        "anchor_max_exact": _bounded_float(raw.get("anchor_max_exact"), _configured_anchor_max_exact),
        "conflict_pin_min": _bounded_float(raw.get("conflict_pin_min"), _configured_conflict_pin_min),
    }


def _propose(item: EvidenceItem, semantic: dict[str, float], lease: str, policy: dict[str, float]) -> tuple[str, str]:
    needed = semantic["needed_again"]
    exact = semantic["exact_required"]
    superseded = semantic["superseded"]
    conflict = semantic["conflict"]

    if lease == "until_verification_pass" and not lifecycle.verification_passed():
        return "PIN", "lifecycle:until_verification_pass"
    if conflict >= policy["conflict_pin_min"]:
        return "PIN", "safety:unresolved_conflict"
    if not item.recoverable:
        return "KEEP_EXACT", "safety:unrecoverable"
    if (
        needed <= policy["drop_max_needed"]
        and exact <= policy["drop_max_exact"]
        and superseded >= policy["drop_min_superseded"]
        and conflict < 0.5
    ):
        return "DROP", "policy:low_value_superseded_recoverable"
    if needed <= policy["anchor_max_needed"] and exact <= policy["anchor_max_exact"] and conflict < policy["conflict_pin_min"]:
        return "ANCHOR", "policy:low_exactness_recoverable"
    return "KEEP_EXACT", "policy:working_set"


def curate_context(
    *,
    goal: str,
    items: Any,
    preserve_tail: Any = None,
    min_confidence: Any = None,  # legacy compatibility only
    preview_chars: Any = None,
    stub_chars: Any = None,
    anchor_chars: Any = None,
    mode: Any = None,
    policy: dict[str, Any] | None = None,
    contract: str = "context-curation/v2",
    engine_factory: Callable[[], DecisionEngine] = DecisionEngine,
) -> dict[str, Any]:
    """Return an ordered context-value plan and (optionally) apply it to a copy.

    ``mode=shadow`` never removes or anchors evidence; it records the proposed
    action while returning the original items. ``mode=apply`` applies deterministic
    ANCHOR/DROP operations after Jev semantic assessment.
    """
    goal = str(goal or "").strip()
    if not goal:
        raise ValueError("goal must be non-empty")
    normalized = _normalize_items(items)
    tail = _bounded_int(preserve_tail, _configured_preserve_tail, 0, 12)
    preview_limit = _bounded_int(preview_chars, _configured_preview_chars, 160, 4000)
    selected_anchor = anchor_chars if anchor_chars is not None else stub_chars
    anchor_limit = _bounded_int(selected_anchor, _configured_anchor_chars, 40, 1200)
    selected_mode = str(mode or _configured_mode).strip().lower()
    if selected_mode not in {"apply", "shadow"}:
        raise ValueError("mode must be 'apply' or 'shadow'")
    local_policy = _policy(policy)
    protected_tail_start = max(0, len(normalized) - tail)

    decisions: dict[str, CurationDecision] = {}
    candidates: list[EvidenceItem] = []
    protected_for_state: list[dict[str, Any]] = []

    for index, item in enumerate(normalized):
        lease = _lease_for(item)
        if item.pinned:
            proposed, basis = "PIN", "policy:pinned"
        elif item.kind in _TEXT_KINDS:
            proposed, basis = "KEEP_EXACT", "policy:verbatim_text"
        elif index >= protected_tail_start:
            proposed, basis = "PIN", "policy:recent_tail"
        else:
            candidates.append(item)
            continue
        action = proposed if selected_mode == "apply" else "KEEP_EXACT"
        decisions[item.id] = CurationDecision(item.id, action, proposed, basis, lease, {}, item.recoverable)
        protected_for_state.append(_preview(item, min(preview_limit, 600)))

    request_meta: list[dict[str, Any]] = []
    total_usage: dict[str, Any] = {"input_tokens": 0, "output_tokens": 0, "cost": 0.0}
    total_latency = 0.0

    for offset in range(0, len(candidates), _BATCH_SIZE):
        batch = candidates[offset : offset + _BATCH_SIZE]
        state = {
            "goal": goal,
            "latest_verification": lifecycle.latest_verification(),
            "protected_context": protected_for_state[-8:],
            "candidate_evidence": [_preview(item, preview_limit) for item in batch],
            "decision_boundary": {
                "jev_estimates": ["needed_again", "exact_required", "superseded", "conflict"],
                "local_code_decides": ["KEEP_EXACT", "PIN", "ANCHOR", "DROP"],
                "recoverability_is_deterministic": True,
                "nonrecoverable_evidence_is_never_dropped": True,
            },
        }
        questions: dict[str, dict[str, Any]] = {}
        for slot, item in enumerate(batch):
            questions.update(_questions(item, slot))
        result = engine_factory().assess(
            state=state,
            questions=questions,
            contract=f"{contract}/semantic-batch-{offset // _BATCH_SIZE + 1}",
        )
        total_latency += float(result.get("latency_ms") or 0.0)
        usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
        for key in ("input_tokens", "output_tokens"):
            try:
                total_usage[key] += int(usage.get(key) or 0)
            except (TypeError, ValueError):
                pass
        try:
            total_usage["cost"] += float(usage.get("cost") or 0.0)
        except (TypeError, ValueError):
            pass
        request_meta.append(
            {
                "request_id": result.get("request_id") or "",
                "model": result.get("model") or "",
                "provider": result.get("provider") or "",
                "latency_ms": result.get("latency_ms") or 0.0,
                "usage": usage,
                "execution": result.get("execution") or execution_provenance(live_provider_call=True),
            }
        )

        answers = result.get("answers") if isinstance(result.get("answers"), dict) else {}
        for slot, item in enumerate(batch):
            semantic = {
                "needed_again": _noul(answers.get(f"needed_{slot}")),
                "exact_required": _noul(answers.get(f"exact_{slot}")),
                "superseded": _noul(answers.get(f"superseded_{slot}")),
                "conflict": _noul(answers.get(f"conflict_{slot}")),
            }
            lease = _lease_for(item)
            proposed, basis = _propose(item, semantic, lease, local_policy)
            if proposed == "ANCHOR" and len(_anchor(item, anchor_limit)) >= len(item.content):
                proposed, basis = "KEEP_EXACT", "policy:anchor_would_not_reduce"
            action = proposed if selected_mode == "apply" else "KEEP_EXACT"
            decisions[item.id] = CurationDecision(item.id, action, proposed, basis, lease, semantic, item.recoverable)

    curated_items: list[dict[str, Any]] = []
    output_chars = 0
    applied_counts = {action: 0 for action in _ACTIONS}
    proposed_counts = {action: 0 for action in _ACTIONS}
    proposed_output_chars = 0

    for item in normalized:
        decision = decisions[item.id]
        applied_counts[decision.action] += 1
        proposed_counts[decision.proposed_action] += 1

        proposed_content = item.content
        if decision.proposed_action == "ANCHOR":
            proposed_content = _anchor(item, anchor_limit)
        elif decision.proposed_action == "DROP":
            proposed_content = ""
        proposed_output_chars += len(proposed_content)

        # Shadow mode records proposals in the shadow-plan ledger only.
        # Counting hypothetical ANCHOR/DROP actions as real compaction corrupts
        # recovery-demand telemetry and conflates observation with apply mode.
        if selected_mode == "apply" and decision.action in {"ANCHOR", "DROP"}:
            ledger.record_evidence(
                evidence_id=item.id,
                content=item.content,
                kind=item.kind,
                recoverable=item.recoverable,
                metadata={**item.metadata, "lease": decision.lease},
                action=decision.action,
                source="curation:apply",
            )

        if decision.action == "DROP":
            continue
        content = item.content
        if decision.action == "ANCHOR":
            content = _anchor(item, anchor_limit)
        output_chars += len(content)
        curated_items.append(
            {
                "id": item.id,
                "kind": item.kind,
                "content": content,
                "recoverable": item.recoverable,
                "pinned": item.pinned,
                "metadata": item.metadata,
                "retention": decision.as_dict(),
            }
        )

    input_chars = sum(len(item.content) for item in normalized)
    applied_reduced_chars = max(0, input_chars - output_chars)
    proposed_reduced_chars = max(0, input_chars - proposed_output_chars)
    stats = {
        "input_items": len(normalized),
        "output_items": len(curated_items),
        "mode": selected_mode,
        "applied": {key.lower(): value for key, value in applied_counts.items()},
        "proposed": {key.lower(): value for key, value in proposed_counts.items()},
        "input_chars": input_chars,
        "output_chars": output_chars,
        "applied_reduced_chars": applied_reduced_chars,
        "applied_reduction_ratio": round((applied_reduced_chars / input_chars) if input_chars else 0.0, 6),
        "proposed_reduced_chars": proposed_reduced_chars,
        "proposed_reduction_ratio": round((proposed_reduced_chars / input_chars) if input_chars else 0.0, 6),
        "provider_requests": len(request_meta),
        "preserve_tail": tail,
        "policy": local_policy,
    }
    decision_list = [decisions[item.id].as_dict() for item in normalized]
    if selected_mode == "shadow":
        ledger.record_shadow_plan(contract=contract, goal=goal, decisions=decision_list, stats=stats)

    provider = next((req.get("provider") for req in request_meta if req.get("provider")), "")
    model = next((req.get("model") for req in request_meta if req.get("model")), "")
    return {
        "contract": contract,
        "goal": goal,
        "mode": selected_mode,
        "curated_items": curated_items,
        "decisions": decision_list,
        "stats": stats,
        "usage": total_usage,
        "latency_ms": round(total_latency, 3),
        "requests": request_meta,
        "provider": provider,
        "model": model,
        "execution": execution_provenance(live_provider_call=bool(request_meta)),
    }
