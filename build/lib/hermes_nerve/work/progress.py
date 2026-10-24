from __future__ import annotations

from typing import Any

from .budget import budget_state
from .models import CriterionProjection, DoDContract, RunIdentity, WorkProjection
from .store import SupervisionStore

_EVENT_STATE = {
    "CRITERION_STARTED": "IN_PROGRESS",
    "CRITERION_CLAIMED_PASS": "CLAIMED_PASS",
    "CRITERION_FAILED": "FAIL",
    "EVIDENCE_ADDED": "OBSERVED",
}


def project(store: SupervisionStore, contract: DoDContract, identity: RunIdentity | None) -> WorkProjection:
    run_id = identity.run_id if identity else None
    event_rows = store.events(contract.task_id, run_id=run_id, include_stale=False) if identity else []
    verdicts = store.latest_contract_verdicts(contract.task_id, contract.contract_hash) if identity else {}
    evidence_rows = store.evidence(identity) if identity else []
    evidence_count: dict[str, int] = {}
    for row in evidence_rows:
        cid = str(row.get("criterion_id") or "")
        if cid:
            evidence_count[cid] = evidence_count.get(cid, 0) + 1

    event_state: dict[str, str] = {}
    last_event: dict[str, str] = {}
    for row in event_rows:
        cid = str(row.get("criterion_id") or "")
        if not cid:
            continue
        state = _EVENT_STATE.get(str(row.get("event_type") or "").upper())
        if state:
            event_state[cid] = state
            last_event[cid] = str(row.get("event_id") or "")

    projections: list[CriterionProjection] = []
    verified_ids: set[str] = set()
    state_by_id: dict[str, str] = {}
    for criterion in contract.criteria:
        verdict = verdicts.get(criterion.id)
        if verdict:
            state = str(verdict.get("state") or "UNKNOWN")
            reason = str(verdict.get("reason") or "")
            receipt_id = str(verdict.get("receipt_id") or "")
        else:
            state = event_state.get(criterion.id, "UNKNOWN")
            reason = ""
            receipt_id = ""
        if state == "VERIFIED_PASS":
            verified_ids.add(criterion.id)
        state_by_id[criterion.id] = state
        projections.append(
            CriterionProjection(
                criterion_id=criterion.id,
                description=criterion.description,
                required=criterion.required,
                weight=criterion.weight,
                state=state,  # type: ignore[arg-type]
                evidence_count=evidence_count.get(criterion.id, 0),
                last_event_id=last_event.get(criterion.id, ""),
                receipt_id=receipt_id,
                reason=reason,
            )
        )

    frontier: list[str] = []
    for criterion in contract.criteria:
        if not criterion.required or state_by_id.get(criterion.id) == "VERIFIED_PASS":
            continue
        if all(dep in verified_ids for dep in criterion.depends_on):
            frontier.append(criterion.id)

    required = [c for c in contract.criteria if c.required]
    verified_required = sum(1 for c in required if c.id in verified_ids)
    required_weight = sum(max(0.0, c.weight) for c in required)
    verified_weight = sum(max(0.0, c.weight) for c in required if c.id in verified_ids)
    stale_count = sum(1 for r in store.events(contract.task_id, include_stale=True) if int(r.get("stale") or 0))
    budget = budget_state(store, identity, contract) if identity else _empty_budget(contract)
    return WorkProjection(
        task_id=contract.task_id,
        run_id=run_id,
        contract_hash=contract.contract_hash,
        contract_version=contract.version,
        criteria=tuple(projections),
        frontier=tuple(frontier),
        verified_required=verified_required,
        required_total=len(required),
        verified_weight=verified_weight,
        required_weight=required_weight,
        budget=budget,
        stale_event_count=stale_count,
    )


def _empty_budget(contract: DoDContract):
    from .models import BudgetState
    return BudgetState(contract.allocated_tokens, 0, contract.allocated_tokens, "none", ())
