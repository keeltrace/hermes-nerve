from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Literal

CriterionState = Literal[
    "UNKNOWN", "IN_PROGRESS", "CLAIMED_PASS", "OBSERVED",
    "VERIFIED_PASS", "FAIL", "NEEDS_EVIDENCE",
]
TrajectoryControl = Literal["CONTINUE", "WATCH", "REPLAN", "BLOCK"]
SupervisionMode = Literal["shadow", "advisory", "enforce"]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class CriterionSpec:
    id: str
    description: str
    required: bool = True
    weight: float = 1.0
    estimated_tokens: int = 0
    depends_on: tuple[str, ...] = ()
    evidence_requirements: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "description": self.description,
            "required": self.required,
            "weight": self.weight,
            "estimated_tokens": self.estimated_tokens,
            "depends_on": list(self.depends_on),
            "evidence_requirements": list(self.evidence_requirements),
        }


@dataclass(frozen=True)
class DoDContract:
    task_id: str
    version: int
    goal: str
    criteria: tuple[CriterionSpec, ...]
    reserve_tokens: int
    checkpoint_fractions: tuple[float, ...]
    contract_hash: str
    locked_at: str
    actor: str
    supersedes_hash: str | None = None
    receipt_id: str = ""

    @property
    def allocated_tokens(self) -> int:
        return sum(max(0, c.estimated_tokens) for c in self.criteria) + max(0, self.reserve_tokens)

    def as_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "version": self.version,
            "goal": self.goal,
            "criteria": [c.as_dict() for c in self.criteria],
            "reserve_tokens": self.reserve_tokens,
            "checkpoint_fractions": list(self.checkpoint_fractions),
            "contract_hash": self.contract_hash,
            "locked_at": self.locked_at,
            "actor": self.actor,
            "supersedes_hash": self.supersedes_hash,
            "receipt_id": self.receipt_id,
            "allocated_tokens": self.allocated_tokens,
        }


@dataclass(frozen=True)
class RunIdentity:
    task_id: str
    run_id: int
    contract_hash: str
    claim_identity: str
    worker_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class WorkEvent:
    event_id: str
    event_type: str
    criterion_id: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    source: str = "worker"
    created_at: str = field(default_factory=utc_now)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class EvidenceRecord:
    evidence_id: str
    task_id: str
    run_id: int
    contract_hash: str
    source: str
    kind: str
    criterion_id: str | None
    sha256: str
    preview: str
    pointer: str = ""
    tool_name: str = ""
    is_error: bool = False
    created_at: str = field(default_factory=utc_now)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CriterionProjection:
    criterion_id: str
    description: str
    required: bool
    weight: float
    state: CriterionState
    evidence_count: int = 0
    last_event_id: str = ""
    receipt_id: str = ""
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class BudgetState:
    allocated_tokens: int
    consumed_tokens: int
    remaining_tokens: int
    usage_source: str
    crossed_checkpoints: tuple[float, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "allocated_tokens": self.allocated_tokens,
            "consumed_tokens": self.consumed_tokens,
            "remaining_tokens": self.remaining_tokens,
            "usage_source": self.usage_source,
            "crossed_checkpoints": list(self.crossed_checkpoints),
            "fraction_consumed": (
                round(self.consumed_tokens / self.allocated_tokens, 6)
                if self.allocated_tokens > 0 else 0.0
            ),
        }


@dataclass(frozen=True)
class WorkProjection:
    task_id: str
    run_id: int | None
    contract_hash: str
    contract_version: int
    criteria: tuple[CriterionProjection, ...]
    frontier: tuple[str, ...]
    verified_required: int
    required_total: int
    verified_weight: float
    required_weight: float
    budget: BudgetState
    stale_event_count: int = 0

    @property
    def verified_percent(self) -> float:
        if self.required_weight <= 0:
            return 100.0
        return round(100.0 * self.verified_weight / self.required_weight, 2)

    def as_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "run_id": self.run_id,
            "contract_hash": self.contract_hash,
            "contract_version": self.contract_version,
            "criteria": [c.as_dict() for c in self.criteria],
            "frontier": list(self.frontier),
            "verified_required": self.verified_required,
            "required_total": self.required_total,
            "verified_weight": self.verified_weight,
            "required_weight": self.required_weight,
            "verified_percent": self.verified_percent,
            "budget": self.budget.as_dict(),
            "stale_event_count": self.stale_event_count,
        }


@dataclass(frozen=True)
class TrajectoryAssessment:
    decision_id: str
    control: TrajectoryControl
    confidence: float
    finish_within_budget_probability: float | None
    estimated_tokens_remaining: int | None
    risks: tuple[str, ...]
    reason: str
    receipt_id: str
    mode: SupervisionMode
    enforcement_eligible: bool
    trigger: str
    next_action: str = ""
    directive: str = ""
    supervisor_tokens: int = 0
    worker_tokens_at_decision: int = 0
    estimated_tokens_avoided: int = 0
    created_at: str = field(default_factory=utc_now)

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["risks"] = list(self.risks)
        return data


@dataclass(frozen=True)
class CompletionVerdict:
    allow: bool
    value: str
    confidence: float
    reason: str
    receipt_id: str = ""
    missing_criteria: tuple[str, ...] = ()
    decision_id: str = ""

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["missing_criteria"] = list(self.missing_criteria)
        return data


@dataclass(frozen=True)
class CheckpointPacket:
    checkpoint_id: str
    identity: RunIdentity
    decision_id: str
    reason: str
    frontier: tuple[str, ...]
    verified_criteria: tuple[str, ...]
    failed_criteria: tuple[str, ...]
    budget: BudgetState
    current_plan: str = ""
    test_summary: str = ""
    artifacts: tuple[dict[str, Any], ...] = ()
    workspace_result: dict[str, Any] = field(default_factory=dict)
    checkpoint_complete: bool = False
    created_at: str = field(default_factory=utc_now)

    def as_dict(self) -> dict[str, Any]:
        return {
            "checkpoint_id": self.checkpoint_id,
            "identity": self.identity.as_dict(),
            "decision_id": self.decision_id,
            "reason": self.reason,
            "frontier": list(self.frontier),
            "verified_criteria": list(self.verified_criteria),
            "failed_criteria": list(self.failed_criteria),
            "budget": self.budget.as_dict(),
            "current_plan": self.current_plan,
            "test_summary": self.test_summary,
            "artifacts": list(self.artifacts),
            "workspace_result": self.workspace_result,
            "checkpoint_complete": self.checkpoint_complete,
            "created_at": self.created_at,
        }
