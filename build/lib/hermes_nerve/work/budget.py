from __future__ import annotations

from .models import BudgetState, DoDContract, RunIdentity, utc_now
from .store import SupervisionStore


def budget_state(store: SupervisionStore, identity: RunIdentity, contract: DoDContract) -> BudgetState:
    consumed, source = store.latest_usage(identity)
    allocated = max(0, contract.allocated_tokens)
    crossed = tuple(sorted(store.crossed_checkpoints(identity)))
    return BudgetState(
        allocated_tokens=allocated,
        consumed_tokens=max(0, consumed),
        remaining_tokens=max(0, allocated - max(0, consumed)),
        usage_source=source,
        crossed_checkpoints=crossed,
    )


def record_usage(
    store: SupervisionStore,
    identity: RunIdentity,
    contract: DoDContract,
    *,
    consumed_tokens: int,
    source: str,
) -> tuple[BudgetState, tuple[float, ...]]:
    if source not in {"provider_usage", "estimated", "remote_provider_usage", "worker_reported"}:
        raise ValueError("usage source must be explicit")
    if source == "worker_reported":
        # Worker-reported usage may inform telemetry, but it cannot mint budget.
        source = "worker_reported"
    stale = store.record_usage(identity, consumed_tokens=max(0, int(consumed_tokens)), source=source, created_at=utc_now())
    if stale:
        raise ValueError("stale run usage cannot mutate current budget projection")
    allocated = contract.allocated_tokens
    fraction = (max(0, int(consumed_tokens)) / allocated) if allocated > 0 else 0.0
    newly_crossed: list[float] = []
    for threshold in sorted(set(contract.checkpoint_fractions)):
        if fraction >= threshold and store.mark_checkpoint_crossed(identity, threshold, crossed_at=utc_now()):
            newly_crossed.append(threshold)
    return budget_state(store, identity, contract), tuple(newly_crossed)
