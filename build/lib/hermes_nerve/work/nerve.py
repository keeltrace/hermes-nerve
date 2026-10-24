from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, asdict
from typing import Any

from .economics import usage_parts
from .models import RunIdentity, utc_now
from .progress import project


@dataclass(frozen=True)
class NerveDecision:
    level: str
    confidence: float
    reason: str
    token_target: int
    consumed_tokens: int
    fraction_of_target: float
    api_calls: int
    repeated_failure_count: int
    high_context_streak: int
    should_kill: bool = False
    requires_orchestrator_review: bool = False
    base_token_target: int = 0
    extension_tokens: int = 0
    extension_round: int = 0
    created_at: str = ""

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        if not data.get("created_at"):
            data["created_at"] = utc_now()
        return data


def estimate_task_budget(
    body: str,
    criterion_count: int,
    *,
    floor_tokens: int = 70000,
    base_tokens: int = 120000,
    per_criterion_tokens: int = 75000,
    body_char_factor: float = 25.0,
    safety_multiplier: float = 1.25,
    max_tokens: int = 2_000_000,
) -> int:
    """Estimate a conservative worker-token target without spending a model call.

    The estimator is intentionally biased toward false-negative avoidance: a task
    receives a meaningful safety margin, while pathological multi-million-token
    loops still cross the hard nerve threshold long before they consume several
    healthy runs' worth of budget.
    """
    count = max(1, int(criterion_count or 1))
    floor = max(1000, int(floor_tokens or 0))
    base = max(0, int(base_tokens or 0))
    per = max(0, int(per_criterion_tokens or 0))
    chars = len(str(body or ""))
    body_cost = min(250_000, int(chars * max(0.0, float(body_char_factor))))
    raw = max(floor, base + count * per + body_cost)
    estimate = int(raw * max(1.0, float(safety_multiplier or 1.0)))
    estimate = min(max(floor, estimate), max(floor, int(max_tokens or estimate)))
    # Stable/legible contract values and easier benchmark comparisons.
    return int(((estimate + 9999) // 10000) * 10000)


def budget_criterion(token_target: int, *, tolerance: float = 1.10) -> dict[str, Any]:
    target = max(1, int(token_target))
    tol = max(1.0, float(tolerance or 1.0))
    ceiling = int(target * tol)
    return {
        "id": "DOD-BUDGET",
        "description": (
            f"Efficiency budget telemetry: expected worker-token target is {target} accounted tokens; the legacy "
            f"completion warning ceiling is {ceiling} tokens ({tol:.2f}x target). Budget is execution policy, not "
            "a correctness criterion: overruns must be recorded and escalated by Nerve but must not make otherwise "
            "correct work impossible to complete."
        ),
        "required": False,
        "weight": 1.0,
        # The target already defines the contract allocation. Do not double-count it.
        "estimated_tokens": 0,
    }


def _repeat_failure_count(store, identity: RunIdentity) -> int:
    fps: list[str] = []
    for row in store.events(identity.task_id, run_id=identity.run_id, include_stale=False):
        if str(row.get("event_type") or "").upper() != "TEST_FAILED":
            continue
        payload = row.get("payload") or {}
        if isinstance(payload, str):
            try:
                import json
                payload = json.loads(payload)
            except Exception:
                payload = {}
        candidates = list((payload or {}).get("failure_fingerprints") or [])
        if not candidates and (payload or {}).get("fingerprint"):
            candidates = [str((payload or {}).get("fingerprint"))]
        fps.extend(str(x) for x in candidates if str(x))
    return max(Counter(fps).values(), default=0)


def _high_context_streak(rows: list[dict[str, Any]], token_target: int) -> int:
    threshold = max(50_000, int(max(1, token_target) * 0.06))
    streak = 0
    for row in reversed(rows):
        if int(row.get("input_tokens") or 0) >= threshold:
            streak += 1
        else:
            break
    return streak



def _latest_payload(supervisor, identity: RunIdentity, kind: str) -> dict[str, Any]:
    row = supervisor.store.latest_diagnostic(task_id=identity.task_id, run_id=identity.run_id, kind=kind)
    payload = dict((row or {}).get("payload") or {})
    return payload


def budget_extension_state(supervisor, identity: RunIdentity) -> dict[str, Any]:
    """Return the single automatic extension state, if one was granted."""
    return _latest_payload(supervisor, identity, "nerve_budget_extension")


def budget_forecast_state(supervisor, identity: RunIdentity) -> dict[str, Any]:
    return _latest_payload(supervisor, identity, "nerve_budget_forecast")


def effective_token_target(supervisor, identity: RunIdentity) -> tuple[int, int, int, int]:
    contract = supervisor._contract_for_identity(identity, allow_stale=True)
    base = max(1, int(contract.allocated_tokens))
    ext = budget_extension_state(supervisor, identity)
    extra = max(0, int(ext.get("extension_tokens") or 0))
    round_no = max(0, int(ext.get("extension_round") or (1 if extra else 0)))
    return base + extra, base, extra, round_no


def forecast_extension(supervisor, identity: RunIdentity, *, cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    """Ask the configured Reflex backend whether one bounded token extension is likely to finish the task.

    This is an advisory watchdog question only. YES may authorize the one configured automatic
    extension. NO/MAYBE never terminate a task; they route authority back to the orchestrator.
    """
    cfg = dict(cfg or {})
    contract = supervisor._contract_for_identity(identity, allow_stale=True)
    projection = project(supervisor.store, contract, identity)
    consumed = supervisor.store.api_usage_total(identity)
    base = max(1, int(contract.allocated_tokens))
    extension_fraction = max(0.01, min(1.0, float(cfg.get("nerve_extension_fraction", 0.25))))
    proposed_extra = max(1_000, int(base * extension_fraction))
    remaining = []
    for criterion, state in zip(contract.criteria, projection.criteria):
        if criterion.required and state.state != "VERIFIED_PASS":
            remaining.append({
                "id": criterion.id,
                "description": criterion.description,
                "state": state.state,
                "evidence_count": state.evidence_count,
            })
    state = {
        "goal": contract.goal,
        "verified_percent": projection.verified_percent,
        "required_verified": projection.verified_required,
        "required_total": projection.required_total,
        "remaining_criteria": remaining,
        "frontier": list(projection.frontier),
        "worker_tokens": consumed,
        "base_token_target": base,
        "fraction_of_base_target": consumed / base,
        "proposed_extension_fraction": extension_fraction,
        "proposed_extension_tokens": proposed_extra,
        "recent_checkpoint": supervisor.store.latest_checkpoint(identity.task_id),
        "repeated_failure_count": _repeat_failure_count(supervisor.store, identity),
    }
    engine = supervisor.engine_factory()
    result: Any
    if hasattr(engine, "assess"):
        result = engine.assess(
            state=state,
            questions={
                "finish_with_extension": {
                    "type": "choice",
                    "instructions": (
                        "Given the locked Definition of Done, verified evidence, current agent status, token use, and "
                        "remaining work, would granting the proposed additional token budget likely let this worker "
                        "finish the task correctly? This is a spending forecast, not termination authority."
                    ),
                    "criteria": {
                        "YES": "The current approach is sound and the proposed extension is likely sufficient to finish.",
                        "NO": "Even with the proposed extension, the current trajectory is unlikely to finish correctly.",
                        "MAYBE": "Evidence is insufficient or mixed; the main orchestrator should review before more spending.",
                    },
                }
            },
            contract="work-nerve-extension-forecast/v1",
        )
        answers = result.get("answers") if isinstance(result, dict) else {}
        answer = dict((answers or {}).get("finish_with_extension") or {})
        value = str(answer.get("choice") or answer.get("value") or "MAYBE").upper()
        probabilities = {str(k).upper(): float(v) for k, v in (answer.get("probabilities") or {}).items()}
        confidence = float(answer.get("confidence", probabilities.get(value, 0.0)) or 0.0)
        receipt_id = str(result.get("receipt_id") or result.get("id") or "") if isinstance(result, dict) else ""
        usage = dict(result.get("usage") or {}) if isinstance(result, dict) else {}
    else:
        result = engine.decide(
            state=state,
            instructions=(
                "Would the proposed additional token budget likely let this worker finish the locked Definition of Done? "
                "Answer YES, NO, or MAYBE. This is advisory only; the main orchestrator owns stop authority."
            ),
            choices=["YES", "NO", "MAYBE"],
            criteria={
                "YES": "Likely to finish correctly with the proposed extension.",
                "NO": "Unlikely to finish correctly even with the extension.",
                "MAYBE": "Uncertain; require orchestrator review.",
            },
            contract="work-nerve-extension-forecast/v1",
        )
        value = str(getattr(result, "value", "MAYBE") or "MAYBE").upper()
        probabilities = {str(k).upper(): float(v) for k, v in (getattr(result, "probabilities", {}) or {}).items()}
        confidence = float(getattr(result, "confidence", probabilities.get(value, 0.0)) or 0.0)
        receipt_id = str(getattr(result, "receipt_id", "") or "")
        usage = dict(getattr(result, "usage", {}) or {})
    if value not in {"YES", "NO", "MAYBE"}:
        value = "MAYBE"
    parts = usage_parts(usage)
    if parts["accounted_tokens"]:
        supervisor.store.record_supervisor_usage(
            identity,
            purpose="nerve_extension_forecast",
            input_tokens=parts["input_tokens"], output_tokens=parts["output_tokens"],
            reasoning_tokens=parts["reasoning_tokens"], total_tokens=parts["accounted_tokens"],
            created_at=utc_now(),
        )
    return {
        "value": value,
        "confidence": confidence,
        "probabilities": probabilities,
        "receipt_id": receipt_id,
        "extension_fraction": extension_fraction,
        "proposed_extension_tokens": proposed_extra,
        "base_token_target": base,
        "consumed_tokens": consumed,
        "verified_percent": projection.verified_percent,
        "remaining_required": len(remaining),
        "created_at": utc_now(),
    }


def evaluate(supervisor, identity: RunIdentity, *, lifecycle_state: str = "", cfg: dict[str, Any] | None = None) -> NerveDecision:
    cfg = dict(cfg or {})
    rows = supervisor.store.api_usage_rows(identity)
    consumed = supervisor.store.api_usage_total(identity)
    target, base_target, extension_tokens, extension_round = effective_token_target(supervisor, identity)
    ratio = consumed / target
    base_ratio = consumed / base_target
    calls = len(rows)
    repeats = _repeat_failure_count(supervisor.store, identity)
    high_ctx = _high_context_streak(rows, base_target)

    watch_fraction = float(cfg.get("nerve_watch_fraction", 0.65))
    forecast_fraction = float(cfg.get("nerve_forecast_fraction", 0.80))
    no_handoff_fraction = float(cfg.get("nerve_no_handoff_fraction", 0.90))
    replan_fraction = float(cfg.get("nerve_replan_fraction", 0.90))
    emergency_multiplier = float(cfg.get("nerve_hard_budget_multiplier", 1.75))
    repeat_trip = int(cfg.get("nerve_repeated_failure_kill", 3))
    high_ctx_trip = int(cfg.get("nerve_high_context_streak_kill", 3))

    terminal = str(lifecycle_state or "").upper()
    if terminal == "COMPLETED":
        return NerveDecision(
            "CONTINUE", 1.0, "run is already canonically completed",
            target, consumed, ratio, calls, repeats, high_ctx, False, False,
            base_target, extension_tokens, extension_round, utc_now(),
        )
    if terminal in {"VERIFIED", "COMPLETING", "COMPLETION_RETRY"}:
        # This is a lifecycle invariant, not an economic/model judgment. The controller
        # should reconcile completion; if it cannot, authority still returns to review.
        return NerveDecision(
            "ORCH_REVIEW", 1.0,
            f"provider call observed after terminal verification state {terminal}; controller reconciliation/review required",
            target, consumed, ratio, calls, repeats, high_ctx, False, True,
            base_target, extension_tokens, extension_round, utc_now(),
        )

    forecast = budget_forecast_state(supervisor, identity)
    forecast_value = str(forecast.get("value") or "").upper()

    # After one automatic extension, exhausting the extended target always returns
    # authority to the main orchestrator. Reflex never owns the stop decision.
    if extension_round >= 1 and ratio >= 1.0:
        return NerveDecision(
            "ORCH_REVIEW", 0.999,
            f"granted token extension exhausted: {consumed}/{target} ({ratio:.2f}x extended target); orchestrator must decide next action",
            target, consumed, ratio, calls, repeats, high_ctx, False, True,
            base_target, extension_tokens, extension_round, utc_now(),
        )

    # Absolute emergency economic fence becomes a pause/review, never a Jev-owned kill.
    if extension_round == 0 and base_ratio >= emergency_multiplier:
        return NerveDecision(
            "ORCH_REVIEW", 0.999,
            f"emergency spend threshold reached: {consumed}/{base_target} ({base_ratio:.2f}x base target); orchestrator review required",
            target, consumed, ratio, calls, repeats, high_ctx, False, True,
            base_target, extension_tokens, extension_round, utc_now(),
        )

    # Corroborated looping is strong watchdog evidence but still only causes handoff.
    if extension_round == 0 and base_ratio >= 1.35 and repeats >= repeat_trip and high_ctx >= high_ctx_trip:
        return NerveDecision(
            "ORCH_REVIEW", 0.97,
            f"corroborated runaway loop: {base_ratio:.2f}x base target, identical failure repeated {repeats} times, high-context streak={high_ctx}",
            target, consumed, ratio, calls, repeats, high_ctx, False, True,
            base_target, extension_tokens, extension_round, utc_now(),
        )

    # MAYBE is always a main-orchestrator decision. A NO forecast gets a short bounded
    # checkpoint window, then hands off by the configured consumed-budget fraction.
    if forecast_value == "MAYBE":
        return NerveDecision(
            "ORCH_REVIEW", float(forecast.get("confidence") or 0.5),
            "Reflex extension forecast is MAYBE; main orchestrator review required before additional spending",
            target, consumed, ratio, calls, repeats, high_ctx, False, True,
            base_target, extension_tokens, extension_round, utc_now(),
        )
    if forecast_value == "NO" and base_ratio >= no_handoff_fraction:
        return NerveDecision(
            "ORCH_REVIEW", float(forecast.get("confidence") or 0.8),
            f"Reflex extension forecast is NO and base budget is {base_ratio:.0%} consumed; return to orchestrator",
            target, consumed, ratio, calls, repeats, high_ctx, False, True,
            base_target, extension_tokens, extension_round, utc_now(),
        )

    # Before any automatic extension, ask one explicit YES/NO/MAYBE forecasting question.
    if extension_round == 0 and not forecast_value and base_ratio >= forecast_fraction:
        return NerveDecision(
            "FORECAST", 1.0,
            f"base token trajectory reached {base_ratio:.0%}; ask whether one bounded extension is likely to finish",
            target, consumed, ratio, calls, repeats, high_ctx, False, False,
            base_target, extension_tokens, extension_round, utc_now(),
        )

    if ratio >= replan_fraction and (ratio >= 1.0 or repeats >= 2 or high_ctx >= 3):
        return NerveDecision(
            "REPLAN", 0.93 if ratio >= 1.0 else 0.88,
            f"token trajectory is at {ratio:.2f}x effective target with repeats={repeats}, high_context_streak={high_ctx}",
            target, consumed, ratio, calls, repeats, high_ctx, False, False,
            base_target, extension_tokens, extension_round, utc_now(),
        )

    if ratio >= watch_fraction:
        return NerveDecision(
            "WATCH", 0.84,
            f"token trajectory reached {ratio:.0%} of the effective target",
            target, consumed, ratio, calls, repeats, high_ctx, False, False,
            base_target, extension_tokens, extension_round, utc_now(),
        )

    return NerveDecision(
        "CONTINUE", 1.0, "token trajectory is inside the effective target",
        target, consumed, ratio, calls, repeats, high_ctx, False, False,
        base_target, extension_tokens, extension_round, utc_now(),
    )
