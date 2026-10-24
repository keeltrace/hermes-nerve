from __future__ import annotations

import uuid
from typing import Any

from .economics import estimate_tokens_avoided, usage_parts
from .models import DoDContract, RunIdentity, TrajectoryAssessment, WorkProjection, utc_now
from .store import SupervisionStore


def calibration_report(store: SupervisionStore) -> dict[str, Any]:
    rows = store.trajectory_rows()
    labeled = [r for r in rows if isinstance(r.get("outcome"), dict)]
    if not labeled:
        return {"samples": 0, "accuracy": None, "brier": None}
    correct = 0
    brier_total = 0.0
    brier_n = 0
    for row in labeled:
        payload = row.get("payload") or {}
        outcome = row.get("outcome") or {}
        success = bool(outcome.get("successful"))
        control = str(payload.get("control") or "")
        predicted_success = control in {"CONTINUE", "WATCH"}
        if predicted_success == success:
            correct += 1
        p = payload.get("finish_within_budget_probability")
        if isinstance(p, (int, float)):
            brier_total += (float(p) - (1.0 if success else 0.0)) ** 2
            brier_n += 1
    return {
        "samples": len(labeled),
        "accuracy": round(correct / len(labeled), 6),
        "brier": round(brier_total / brier_n, 6) if brier_n else None,
    }


def enforcement_eligible(
    store: SupervisionStore,
    *,
    min_samples: int,
    max_brier: float,
    operator_override: bool,
) -> bool:
    if operator_override:
        return True
    report = calibration_report(store)
    if int(report.get("samples") or 0) < max(1, int(min_samples)):
        return False
    brier = report.get("brier")
    return isinstance(brier, (int, float)) and float(brier) <= float(max_brier)


def compact_packet(contract: DoDContract, projection: WorkProjection, *, trigger: str, failures: list[str] | None = None) -> dict[str, Any]:
    unresolved = []
    for criterion, state in zip(contract.criteria, projection.criteria):
        if criterion.required and state.state != "VERIFIED_PASS":
            unresolved.append({
                "id": criterion.id,
                "description": criterion.description,
                "state": state.state,
                "estimated_tokens": criterion.estimated_tokens,
                "evidence_count": state.evidence_count,
            })
    return {
        "goal": contract.goal,
        "contract_hash": contract.contract_hash,
        "trigger": trigger,
        "budget": projection.budget.as_dict(),
        "verified_percent": projection.verified_percent,
        "frontier": list(projection.frontier),
        "remaining_criteria": unresolved,
        "recent_failures": list(failures or [])[-5:],
    }


def _answer_choice(answer: Any, default: str) -> tuple[str, float, dict[str, float]]:
    a = answer if isinstance(answer, dict) else {}
    value = str(a.get("choice") or a.get("value") or default).upper()
    probabilities = {str(k).upper(): float(v) for k, v in (a.get("probabilities") or {}).items()}
    confidence = float(a.get("confidence", probabilities.get(value, 0.0)) or 0.0)
    return value, confidence, probabilities


def _directive(control: str, next_action: str) -> str:
    action_text = {
        "INSPECT_FAILURES": "Inspect the current failing path before editing more code.",
        "TEST_NARROWLY": "Run the narrowest test that can distinguish the leading hypothesis before making another broad change.",
        "PATCH_CURRENT_HYPOTHESIS": "Patch only the current leading root-cause hypothesis, then test it narrowly.",
        "REVERT_LAST_CHANGE": "Revert the most recent unproven change and return to the last evidence-backed state.",
        "TEST_FULL": "Run the full acceptance suite now; use the result as the next decision boundary.",
        "REQUEST_REPLAN": "Checkpoint useful work and return this run to the orchestrator for replanning.",
        "CONTINUE_PLAN": "Continue the current plan; avoid expanding scope without new evidence.",
    }.get(next_action, "")
    if control == "REPLAN":
        return "Current trajectory is unlikely to finish within budget. " + (action_text or "Checkpoint and request replanning.")
    if control == "BLOCK":
        return "Progress is blocked by the current contract/dependency state. Checkpoint useful work and surface the blocker."
    if control == "WATCH":
        return "Trajectory risk is elevated. " + (action_text or "Use the next action to reduce uncertainty before broadening the change.")
    return action_text


def assess(
    *,
    engine: Any,
    store: SupervisionStore,
    identity: RunIdentity,
    contract: DoDContract,
    projection: WorkProjection,
    trigger: str,
    mode: str,
    min_samples: int,
    max_brier: float,
    operator_override: bool = False,
    failures: list[str] | None = None,
) -> TrajectoryAssessment:
    packet = compact_packet(contract, projection, trigger=trigger, failures=failures)
    usage: dict[str, Any] = {}
    receipt_id = ""
    control = "WATCH"
    confidence = 0.0
    probabilities: dict[str, float] = {}
    next_action = ""
    # Prefer one multi-question System One call: trajectory + bounded next action.
    # This is the actual reasoning-offload path; the worker never chooses to call it.
    if hasattr(engine, "assess"):
        result = engine.assess(
            state=packet,
            questions={
                "trajectory": {
                    "type": "choice",
                    "instructions": (
                        "Given the locked Definition of Done, verified progress, failures, frontier, and remaining token budget, "
                        "judge whether the current implementation trajectory should continue."
                    ),
                    "criteria": {
                        "CONTINUE": "Current approach remains viable within remaining budget.",
                        "WATCH": "Viable but uncertainty or budget pressure warrants a bounded corrective action.",
                        "REPLAN": "Continuing the present approach is unlikely to satisfy the remaining DoD within budget.",
                        "BLOCK": "An external dependency, impossible contract, or unavailable capability blocks useful continuation.",
                    },
                },
                "next_action": {
                    "type": "choice",
                    "instructions": "Choose the single next action most likely to reduce wasted reasoning and execution tokens.",
                    "criteria": {
                        "INSPECT_FAILURES": "Inspect the failing path before editing more code.",
                        "TEST_NARROWLY": "Run a focused discriminating test.",
                        "PATCH_CURRENT_HYPOTHESIS": "Make one narrow patch to the leading root-cause hypothesis.",
                        "REVERT_LAST_CHANGE": "Return to the last evidence-backed state.",
                        "TEST_FULL": "Run the full acceptance suite now.",
                        "REQUEST_REPLAN": "Checkpoint and return to the orchestrator.",
                        "CONTINUE_PLAN": "Continue the current plan without expanding scope.",
                    },
                },
            },
            contract="work-trajectory-copilot/v2",
        )
        answers = result.get("answers") if isinstance(result, dict) else {}
        control, confidence, probabilities = _answer_choice((answers or {}).get("trajectory"), "WATCH")
        next_action, next_confidence, _ = _answer_choice((answers or {}).get("next_action"), "CONTINUE_PLAN")
        # A directive should be no more confident than its weakest relevant answer.
        confidence = min(confidence, next_confidence) if next_action else confidence
        usage = dict(result.get("usage") or {}) if isinstance(result, dict) else {}
        receipt_id = str(result.get("receipt_id") or "") if isinstance(result, dict) else ""
    else:
        result = engine.decide(
            state=packet,
            instructions=(
                "Given the locked Definition of Done, evidence, verified progress, failures, frontier, and remaining token budget, "
                "judge the current run trajectory."
            ),
            choices=["CONTINUE", "WATCH", "REPLAN", "BLOCK"],
            criteria={
                "CONTINUE": "Current approach remains viable within remaining budget.",
                "WATCH": "Viable but uncertainty or budget pressure warrants orchestrator attention.",
                "REPLAN": "Current approach is unlikely to satisfy the remaining DoD within budget.",
                "BLOCK": "External dependency, impossible contract, or unavailable capability blocks progress.",
            },
            contract="work-trajectory/v1",
        )
        control = str(getattr(result, "value", "WATCH")).upper()
        confidence = float(getattr(result, "confidence", 0.0) or 0.0)
        probabilities = {str(k).upper(): float(v) for k, v in (getattr(result, "probabilities", {}) or {}).items()}
        usage = dict(getattr(result, "usage", {}) or {})
        receipt_id = str(getattr(result, "receipt_id", "") or "")

    finish_probability = probabilities.get("CONTINUE")
    if finish_probability is not None:
        finish_probability = float(finish_probability) + 0.5 * float(probabilities.get("WATCH") or 0.0)
        finish_probability = max(0.0, min(1.0, finish_probability))
    unresolved_estimate = sum(
        max(0, c.estimated_tokens)
        for c, state in zip(contract.criteria, projection.criteria)
        if c.required and state.state != "VERIFIED_PASS"
    )
    risks: list[str] = []
    if projection.budget.remaining_tokens < unresolved_estimate:
        risks.append("remaining_budget_below_unresolved_estimate")
    if any(c.state == "FAIL" for c in projection.criteria):
        risks.append("criterion_failure")
    if projection.budget.allocated_tokens and projection.budget.consumed_tokens >= projection.budget.allocated_tokens:
        risks.append("budget_exhausted")
    if not projection.frontier and projection.verified_required < projection.required_total:
        risks.append("no_runnable_frontier")
    eligible = mode == "enforce" and enforcement_eligible(
        store, min_samples=min_samples, max_brier=max_brier, operator_override=operator_override
    )
    parts = usage_parts(usage)
    if parts["accounted_tokens"]:
        store.record_supervisor_usage(
            identity,
            purpose="trajectory",
            input_tokens=parts["input_tokens"],
            output_tokens=parts["output_tokens"],
            reasoning_tokens=parts["reasoning_tokens"],
            total_tokens=parts["accounted_tokens"],
            created_at=utc_now(),
        )
    avoided = estimate_tokens_avoided(control, projection.budget.remaining_tokens)
    assessment = TrajectoryAssessment(
        decision_id="jevtraj-" + uuid.uuid4().hex,
        control=control,  # type: ignore[arg-type]
        confidence=confidence,
        finish_within_budget_probability=finish_probability,
        estimated_tokens_remaining=unresolved_estimate,
        risks=tuple(risks),
        reason=(
            f"Jev trajectory verdict {control} at {projection.verified_percent:.2f}% verified; "
            f"remaining budget {projection.budget.remaining_tokens} tokens."
        ),
        receipt_id=receipt_id,
        mode=mode,  # type: ignore[arg-type]
        enforcement_eligible=eligible,
        trigger=trigger,
        next_action=next_action,
        directive=_directive(control, next_action),
        supervisor_tokens=parts["accounted_tokens"],
        worker_tokens_at_decision=projection.budget.consumed_tokens,
        estimated_tokens_avoided=avoided,
        created_at=utc_now(),
    )
    store.save_trajectory(identity, assessment)
    return assessment
