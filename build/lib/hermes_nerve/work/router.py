from __future__ import annotations

from typing import Any

from .runtime import settings


def recent_failure_fingerprints(supervisor, identity, limit: int = 8) -> list[str]:
    """Return fingerprints from the last N *failed test executions*.

    dev7 incorrectly sliced the last N general work events and only then filtered
    TEST_FAILED rows. Normal reads/patches/tool calls quickly pushed failures out of
    that tiny window, so repeated failures disappeared before the ROI router could
    see them. dev8 filters failures first and preserves per-test fingerprints so a
    shrinking/changing pytest failure set can still recognize the same persistent
    failing test across executions.
    """
    rows = supervisor.store.events(identity.task_id, run_id=identity.run_id, include_stale=False)
    failed_rows = [row for row in rows if str(row.get("event_type")) == "TEST_FAILED"]
    out: list[str] = []
    for row in failed_rows[-max(1, limit):]:
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        fps = payload.get("failure_fingerprints")
        if isinstance(fps, list):
            out.extend(str(fp) for fp in fps if str(fp))
            continue
        fp = str(payload.get("fingerprint") or "")
        if fp:
            out.append(fp)
    return out


def should_call_provider(supervisor, identity, *, trigger: str) -> dict[str, Any]:
    cfg = settings()
    if not cfg.get("provider_decisions_enabled", True):
        return {"call": False, "reason": "provider_decisions_disabled", "expected_savings": 0}
    projection = supervisor.projection(identity.task_id)
    econ = supervisor.economics(identity)
    remaining = projection.budget.remaining_tokens
    allowance = econ["supervisor_budget_allowance"]
    if allowance and econ["supervisor_tokens"] + int(cfg.get("estimated_decision_call_tokens", cfg.get("estimated_jev_call_tokens", 800))) > allowance:
        return {"call": False, "reason": "supervisor_budget_exhausted", "expected_savings": 0}
    failures = recent_failure_fingerprints(supervisor, identity)
    repeated = 0
    if failures:
        repeated = max(failures.count(fp) for fp in set(failures))
    trigger_l = str(trigger or "").lower()
    expected = 0
    if repeated >= int(cfg.get("repeated_failure_trigger", 2)):
        expected = int(remaining * 0.45)
    elif "blocker" in trigger_l or "plan_changed" in trigger_l:
        expected = int(remaining * 0.35)
    elif "0.7" in trigger_l or "70" in trigger_l:
        if projection.verified_percent < 40.0 or failures:
            expected = int(remaining * 0.30)
    elif "0.4" in trigger_l or "40" in trigger_l:
        if repeated >= 2 or any(c.state == "FAIL" for c in projection.criteria):
            expected = int(remaining * 0.20)
    elif "failure" in trigger_l:
        if repeated >= int(cfg.get("repeated_failure_trigger", 2)):
            expected = int(remaining * 0.35)
        elif failures and projection.budget.allocated_tokens:
            fraction = projection.budget.consumed_tokens / projection.budget.allocated_tokens
            if fraction >= 0.25 and projection.verified_percent < 20.0:
                expected = int(remaining * 0.20)
    if expected <= 0:
        return {"call": False, "reason": "healthy_or_low_signal", "expected_savings": 0, "repeated_failure": repeated}
    min_savings = int(cfg.get("roi_min_expected_savings_tokens", 1500))
    call_cost = int(cfg.get("estimated_decision_call_tokens", cfg.get("estimated_jev_call_tokens", 800)))
    if expected < max(min_savings, int(call_cost * 1.5)):
        return {"call": False, "reason": "roi_below_threshold", "expected_savings": expected, "repeated_failure": repeated}
    rows = supervisor.store.trajectory_rows(task_id=identity.task_id)
    if rows:
        latest = rows[-1].get("payload") or {}
        last_worker = int(latest.get("worker_tokens_at_decision") or 0)
        cooldown = int(cfg.get("provider_decision_cooldown_tokens", 8000))
        current = projection.budget.consumed_tokens
        urgent = "blocker" in trigger_l or "plan_changed" in trigger_l
        # After one trajectory call, repeated failures must not bypass the token
        # cooldown and turn every subsequent pytest run into another Jev spend.
        if not urgent and current - last_worker < cooldown:
            return {"call": False, "reason": "decision_cooldown", "expected_savings": expected, "repeated_failure": repeated}
    return {"call": True, "reason": "roi_positive", "expected_savings": expected, "repeated_failure": repeated, "failures": failures[-5:]}
