from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any, Callable

from ..engine import DecisionEngine
from ..paths import hermes_home
from ..privacy import canonical_hash, redact
from . import trajectory as trajectory_logic
from .budget import record_usage as record_budget_usage
from .checkpoint import build_checkpoint
from .evidence import make_evidence
from .models import (
    CompletionVerdict,
    CriterionSpec,
    DoDContract,
    RunIdentity,
    TrajectoryAssessment,
    WorkEvent,
    WorkProjection,
    utc_now,
)
from .progress import project
from .store import SupervisionStore
from .economics import usage_parts

_ALLOWED_EVENTS = {
    "CRITERION_STARTED",
    "CRITERION_CLAIMED_PASS",
    "CRITERION_FAILED",
    "BLOCKER_FOUND",
    "PLAN_CHANGED",
    "ARTIFACT_CREATED",
    "TEST_FAILED",
    "TEST_PASSED",
    "EVIDENCE_ADDED",
    "CHECKPOINT_READY",
    "HANDOFF_REQUESTED",
}


class CardSupervisor:
    """Deep module for evidence-backed Kanban card supervision.

    Canonical task lifecycle remains outside this class. It stores only the
    locked success contract, run-scoped supervisory facts, Reflex verdicts,
    progress/budget projection, controls, and checkpoints.
    """

    def __init__(
        self,
        store: SupervisionStore | None = None,
        *,
        store_path: Path | str | None = None,
        engine_factory: Callable[[], Any] = DecisionEngine,
        mode: str = "shadow",
        preview_chars: int = 1200,
        calibration_min_samples: int = 12,
        calibration_max_brier: float = 0.24,
        enforcement_override: bool = False,
        control_confidence: float = 0.86,
    ) -> None:
        path = Path(store_path).expanduser() if store_path else default_store_path()
        self.store = store or SupervisionStore(path)
        self.engine_factory = engine_factory
        self.mode = normalize_mode(mode)
        self.preview_chars = max(160, min(4000, int(preview_chars)))
        self.calibration_min_samples = max(1, int(calibration_min_samples))
        self.calibration_max_brier = max(0.0, min(1.0, float(calibration_max_brier)))
        self.enforcement_override = bool(enforcement_override)
        self.control_confidence = max(0.0, min(1.0, float(control_confidence)))

    # --------------------------- contracts ---------------------------
    def bind_contract(
        self,
        *,
        task_id: str,
        goal: str,
        criteria: list[dict[str, Any]],
        reserve_tokens: int = 0,
        checkpoint_fractions: list[float] | tuple[float, ...] = (0.40, 0.70),
        actor: str = "orchestrator",
        amend: bool = False,
        preflight_result: Any | None = None,
    ) -> dict[str, Any]:
        """Review and lock a DoD. ``preflight_result`` is an injection seam for tests.

        Provider failure never becomes ACCEPT. Worker actors cannot bind or amend.
        """
        task_id = str(task_id or "").strip()
        goal = str(goal or "").strip()
        if not task_id or not goal:
            raise ValueError("task_id and goal are required")
        actor = str(actor or "").strip().lower()
        if actor not in {"orchestrator", "reviewer", "human"}:
            raise PermissionError("only orchestrator/reviewer/human may bind or amend a Definition of Done")
        specs = validate_criteria(criteria)
        fractions = normalize_checkpoints(checkpoint_fractions)
        reserve_tokens = max(0, int(reserve_tokens or 0))
        previous = self.active_contract(task_id)
        if previous and not amend:
            raise ValueError("task already has a locked contract; use an explicit amendment")
        if amend and not previous:
            raise ValueError("cannot amend a task with no existing contract")

        semantic = {
            "schema": "hermes-nerve-dod/v1",
            "task_id": task_id,
            "goal": goal,
            "criteria": [c.as_dict() for c in specs],
            "reserve_tokens": reserve_tokens,
            "checkpoint_fractions": list(fractions),
        }
        contract_hash = canonical_hash(semantic)
        if previous and previous.contract_hash == contract_hash:
            raise ValueError("amendment is semantically identical to the active locked contract")

        review = preflight_result
        if review is None:
            try:
                review = self.engine_factory().decide(
                    state=redact(semantic),
                    instructions=(
                        "Review whether this Definition of Done is sufficient, testable, non-self-referential, and capable of proving "
                        "the stated goal. ACCEPT only if the required criteria collectively prove the goal. AMEND if a concrete missing, "
                        "ambiguous, circular, or unverifiable criterion should be fixed before execution. ESCALATE only when human/domain "
                        "judgment is required."
                    ),
                    choices=["ACCEPT", "AMEND", "ESCALATE"],
                    criteria={
                        "ACCEPT": "The required criteria are sufficient and independently verifiable.",
                        "AMEND": "The contract needs a concrete fix before execution.",
                        "ESCALATE": "Human/domain judgment is required before locking the contract.",
                    },
                    contract="work-dod-preflight/v1",
                )
            except Exception as exc:
                return {
                    "accepted": False,
                    "value": "ESCALATE",
                    "reason": f"DoD preflight provider unavailable: {type(exc).__name__}: {exc}",
                    "contract_hash": contract_hash,
                    "proposal": semantic,
                }
        value = str(getattr(review, "value", None) or (review.get("value") if isinstance(review, dict) else "")).upper()
        confidence = float(getattr(review, "confidence", None) or (review.get("confidence", 0.0) if isinstance(review, dict) else 0.0) or 0.0)
        receipt_id = str(getattr(review, "receipt_id", None) or (review.get("receipt_id", "") if isinstance(review, dict) else "") or "")
        if value != "ACCEPT":
            return {
                "accepted": False,
                "value": value or "ESCALATE",
                "confidence": confidence,
                "receipt_id": receipt_id,
                "reason": "Jev did not accept the proposed Definition of Done.",
                "contract_hash": contract_hash,
                "proposal": semantic,
            }

        version = self.store.next_contract_version(task_id)
        contract = DoDContract(
            task_id=task_id,
            version=version,
            goal=goal,
            criteria=tuple(specs),
            reserve_tokens=reserve_tokens,
            checkpoint_fractions=fractions,
            contract_hash=contract_hash,
            locked_at=utc_now(),
            actor=actor,
            supersedes_hash=previous.contract_hash if previous else None,
            receipt_id=receipt_id,
        )
        self.store.save_contract(contract, semantic)
        return {
            "accepted": True,
            "value": "ACCEPT",
            "confidence": confidence,
            "receipt_id": receipt_id,
            "contract": contract.as_dict(),
        }

    def active_contract(self, task_id: str) -> DoDContract | None:
        row = self.store.active_contract_row(str(task_id))
        return contract_from_row(row) if row else None

    def bind_run(self, identity: RunIdentity) -> WorkProjection:
        self.store.bind_run(identity, bound_at=utc_now())
        contract = self._contract_for_identity(identity)
        return project(self.store, contract, identity)

    @staticmethod
    def hash_value(value: Any) -> str:
        return canonical_hash(value)

    # --------------------------- events/evidence ---------------------------
    def record_event(self, identity: RunIdentity, event: WorkEvent) -> WorkProjection:
        event_type = str(event.event_type or "").upper()
        if event_type not in _ALLOWED_EVENTS:
            raise ValueError(f"unsupported work event {event_type!r}")
        contract = self._contract_for_identity(identity, allow_stale=True)
        if event.criterion_id is not None and event.criterion_id not in {c.id for c in contract.criteria}:
            raise ValueError(f"unknown criterion_id {event.criterion_id!r}")
        normalized = WorkEvent(
            event_id=event.event_id or "jevwork-" + uuid.uuid4().hex,
            event_type=event_type,
            criterion_id=event.criterion_id,
            payload=redact(event.payload),
            source=event.source,
            created_at=event.created_at,
        )
        self.store.append_event(identity, normalized)
        return self.projection(identity.task_id)

    def observe_evidence(
        self,
        identity: RunIdentity,
        *,
        value: Any,
        kind: str = "tool_result",
        source: str = "controller_observed",
        criterion_id: str | None = None,
        pointer: str = "",
        tool_name: str = "",
        is_error: bool = False,
    ) -> dict[str, Any]:
        contract = self._contract_for_identity(identity, allow_stale=True)
        if criterion_id is not None and criterion_id not in {c.id for c in contract.criteria}:
            raise ValueError(f"unknown criterion_id {criterion_id!r}")
        record = make_evidence(
            identity,
            source=source,
            kind=kind,
            value=value,
            criterion_id=criterion_id,
            pointer=pointer,
            tool_name=tool_name,
            is_error=is_error,
            preview_chars=self.preview_chars,
        )
        stale = self.store.append_evidence(identity, record)
        if not stale:
            self.store.append_event(
                identity,
                WorkEvent(
                    event_id="jevwork-" + uuid.uuid4().hex,
                    event_type="EVIDENCE_ADDED",
                    criterion_id=criterion_id,
                    payload={"evidence_id": record.evidence_id, "sha256": record.sha256, "source": source},
                    source="controller",
                ),
            )
        return {"evidence": record.as_dict(), "stale": stale, "projection": self.projection(identity.task_id).as_dict()}

    def verify_criterion(self, identity: RunIdentity, criterion_id: str) -> dict[str, Any]:
        contract = self._contract_for_identity(identity)
        criterion = next((c for c in contract.criteria if c.id == criterion_id), None)
        if criterion is None:
            raise ValueError(f"unknown criterion_id {criterion_id!r}")
        evidence = [
            e for e in self.store.evidence(identity)
            if e.get("criterion_id") in {None, "", criterion_id}
        ]
        if not evidence:
            self.store.save_verdict(
                identity,
                criterion_id=criterion_id,
                state="NEEDS_EVIDENCE",
                reason="No independently observed evidence is attached to this run.",
                receipt_id="",
                evidence_ids=[],
                created_at=utc_now(),
            )
            return {"state": "NEEDS_EVIDENCE", "reason": "No independently observed evidence.", "projection": self.projection(identity.task_id).as_dict()}
        packet = {
            "goal": contract.goal,
            "criterion": criterion.as_dict(),
            "evidence": [
                {
                    "evidence_id": e["evidence_id"],
                    "source": e["source"],
                    "kind": e["kind"],
                    "sha256": e["sha256"],
                    "preview": e["preview"],
                    "tool_name": e.get("tool_name") or "",
                    "is_error": bool(e.get("is_error")),
                }
                for e in evidence[-16:]
            ],
        }
        try:
            result = self.engine_factory().verify(
                state=packet,
                instructions=(
                    "Verify only this Definition-of-Done criterion from independently observed evidence. PASS only when the evidence "
                    "actually proves the criterion. RETRY means execution should be retried, REPLAN means the approach is invalid, and "
                    "ESCALATE means evidence or human/domain judgment is still required."
                ),
                contract="work-criterion-verify/v1",
            )
        except Exception as exc:
            self.store.save_verdict(
                identity,
                criterion_id=criterion_id,
                state="NEEDS_EVIDENCE",
                reason=f"Verification unavailable: {type(exc).__name__}: {exc}",
                receipt_id="",
                evidence_ids=[str(e["evidence_id"]) for e in evidence],
                created_at=utc_now(),
            )
            return {"state": "NEEDS_EVIDENCE", "reason": "Verification provider unavailable.", "projection": self.projection(identity.task_id).as_dict()}
        self._record_result_usage(identity, result, purpose="criterion_verify")
        value = str(getattr(result, "value", "ESCALATE")).upper()
        state = "VERIFIED_PASS" if value == "PASS" else "FAIL" if value in {"RETRY", "REPLAN"} else "NEEDS_EVIDENCE"
        reason = f"Reflex criterion verdict {value}."
        receipt_id = str(getattr(result, "receipt_id", "") or "")
        self.store.save_verdict(
            identity,
            criterion_id=criterion_id,
            state=state,
            reason=reason,
            receipt_id=receipt_id,
            evidence_ids=[str(e["evidence_id"]) for e in evidence],
            created_at=utc_now(),
        )
        return {
            "state": state,
            "value": value,
            "confidence": float(getattr(result, "confidence", 0.0) or 0.0),
            "receipt_id": receipt_id,
            "projection": self.projection(identity.task_id).as_dict(),
        }

    def mark_deterministic_verdict(
        self,
        identity: RunIdentity,
        criterion_id: str,
        *,
        passed: bool,
        reason: str,
        evidence_ids: list[str] | None = None,
    ) -> None:
        contract = self._contract_for_identity(identity)
        if criterion_id not in {c.id for c in contract.criteria}:
            raise ValueError(f"unknown criterion_id {criterion_id!r}")
        self.store.save_verdict(
            identity,
            criterion_id=criterion_id,
            state="VERIFIED_PASS" if passed else "FAIL",
            reason=str(reason or "Deterministic verifier."),
            receipt_id="deterministic",
            evidence_ids=list(evidence_ids or []),
            created_at=utc_now(),
        )

    # --------------------------- completion ---------------------------
    def verify_completion(self, identity: RunIdentity, proposal: dict[str, Any] | None = None) -> CompletionVerdict:
        # First spend zero model tokens proving everything the controller can
        # establish mechanically from the run workspace/evidence.
        try:
            from .deterministic import auto_verify_completion
            auto_verify_completion(self, identity, proposal)
        except Exception:
            pass
        contract = self._contract_for_identity(identity)
        projection = project(self.store, contract, identity)
        latest = self.store.latest_contract_verdicts(identity.task_id, identity.contract_hash)
        missing = tuple(c.criterion_id for c in projection.criteria if c.required and c.state != "VERIFIED_PASS")

        # Handoff-contingent criteria ("verification transitioned this issue
        # to in_review", "issue contains a Nerve Verification comment") are
        # only true after the controller performs the verified handoff, so
        # they can never block the handoff that makes them true. The
        # controller marks them immediately after a successful handoff
        # (paperclip_runtime.request_review).
        def _is_handoff_contingent(description: str) -> bool:
            low = str(description or "").lower()
            return (
                "transitioned this issue to" in low
                or "transitioned the issue to" in low
                or ("contains a" in low and "nerve verification" in low)
            )

        contingent_ids = {
            c.criterion_id for c in projection.criteria if _is_handoff_contingent(c.description)
        }
        missing = tuple(cid for cid in missing if cid not in contingent_ids)

        # dev13: machine-verifiable facts are authoritative. A semantic model may
        # advise on criteria that have no deterministic verdict, but it may never
        # overwrite a controller-observed deterministic FAIL with prose-level PASS.
        deterministic_failures = tuple(
            cid for cid in missing
            if str((latest.get(cid) or {}).get("receipt_id") or "") == "deterministic"
            and str((latest.get(cid) or {}).get("state") or "") == "FAIL"
        )
        if deterministic_failures:
            failure_details = []
            for cid in deterministic_failures:
                reason = str((latest.get(cid) or {}).get("reason") or "deterministic check failed").strip()
                failure_details.append(f"{cid}: {reason}")
            return CompletionVerdict(
                allow=False,
                value="RETRY",
                confidence=1.0,
                reason=(
                    "Deterministic completion evidence failed: " + " | ".join(failure_details) +
                    ". Fix the observed behavior; semantic verification cannot override this failure."
                ),
                receipt_id="deterministic",
                missing_criteria=deterministic_failures,
            )

        if missing:
            # One batched semantic call is reserved only for criteria that the
            # deterministic controller could not evaluate at all. PASS may upgrade
            # those UNKNOWN/NEEDS_EVIDENCE criteria, never deterministic failures.
            evidence = self.store.evidence(identity)
            if not evidence:
                return CompletionVerdict(
                    allow=False,
                    value="NEEDS_EVIDENCE",
                    confidence=1.0,
                    reason="Required criteria are not independently verified and no observed evidence is attached to this run.",
                    missing_criteria=missing,
                )
            packet = {
                "goal": contract.goal,
                "remaining_criteria": [
                    c.as_dict() for c in contract.criteria if c.id in set(missing)
                ],
                "evidence": [
                    {
                        "evidence_id": e["evidence_id"], "criterion_id": e.get("criterion_id"),
                        "kind": e["kind"], "preview": e["preview"], "tool_name": e.get("tool_name") or "",
                        "is_error": bool(e.get("is_error")),
                    }
                    for e in evidence[-24:]
                ],
                "proposal": redact(proposal or {}),
            }
            try:
                result = self.engine_factory().verify(
                    state=packet,
                    instructions=(
                        "Verify the remaining locked Definition-of-Done criteria as one batch. PASS only if the independently "
                        "observed evidence plus completion proposal proves every listed criterion. RETRY means the current approach "
                        "is valid but more execution/evidence is required; REPLAN means the approach must change; ESCALATE means "
                        "human/external judgment is required."
                    ),
                    contract="work-completion-batch/v1",
                )
            except Exception as exc:
                return CompletionVerdict(False, "ESCALATE", 0.0, f"Completion verification unavailable: {type(exc).__name__}: {exc}", missing_criteria=missing)
            self._record_result_usage(identity, result, purpose="completion_batch")
            value = str(getattr(result, "value", "ESCALATE")).upper()
            if value != "PASS":
                return CompletionVerdict(
                    allow=False,
                    value=value,
                    confidence=float(getattr(result, "confidence", 0.0) or 0.0),
                    reason=f"Reflex batched completion verdict {value}.",
                    receipt_id=str(getattr(result, "receipt_id", "") or ""),
                    missing_criteria=missing,
                )
            evidence_ids = [str(e["evidence_id"]) for e in evidence]
            for cid in missing:
                self.store.save_verdict(
                    identity,
                    criterion_id=cid,
                    state="VERIFIED_PASS",
                    reason="Batched Reflex semantic completion verification PASS.",
                    receipt_id=str(getattr(result, "receipt_id", "") or ""),
                    evidence_ids=evidence_ids,
                    created_at=utc_now(),
                )
            projection = project(self.store, contract, identity)
            if any(c.required and c.state != "VERIFIED_PASS" for c in projection.criteria):
                return CompletionVerdict(False, "NEEDS_EVIDENCE", 1.0, "Required criteria remain unverified after batched verification.")
            return CompletionVerdict(
                allow=True,
                value="PASS",
                confidence=float(getattr(result, "confidence", 0.0) or 0.0),
                reason="All required criteria are independently verified; batched Reflex semantic verification PASS.",
                receipt_id=str(getattr(result, "receipt_id", "") or ""),
                decision_id="reflexcomplete-" + uuid.uuid4().hex,
            )
        # When deterministic evidence proved every requirement, do not spend a
        # second provider call merely to bless facts already established.
        return CompletionVerdict(
            allow=True,
            value="PASS",
            confidence=1.0,
            reason="All required criteria are deterministically VERIFIED_PASS.",
            receipt_id="deterministic",
            missing_criteria=(),
            decision_id="reflexcomplete-" + uuid.uuid4().hex,
        )

    # --------------------------- budget / trajectory ---------------------------
    def record_usage(self, identity: RunIdentity, *, consumed_tokens: int, source: str) -> dict[str, Any]:
        contract = self._contract_for_identity(identity)
        state, crossed = record_budget_usage(
            self.store, identity, contract, consumed_tokens=consumed_tokens, source=source
        )
        return {"budget": state.as_dict(), "newly_crossed": list(crossed)}

    def record_api_usage(self, identity: RunIdentity, *, api_request_id: str, usage: dict[str, Any]) -> dict[str, Any]:
        parts = usage_parts(usage)
        inserted, cumulative = self.store.record_api_usage(
            identity,
            api_request_id=api_request_id,
            input_tokens=parts["input_tokens"],
            output_tokens=parts["output_tokens"],
            reasoning_tokens=parts["reasoning_tokens"],
            cache_read_tokens=parts["cache_read_tokens"],
            accounted_tokens=parts["accounted_tokens"],
            created_at=utc_now(),
        )
        out = self.record_usage(identity, consumed_tokens=cumulative, source="provider_usage") if inserted else {
            "budget": self.projection(identity.task_id).budget.as_dict(), "newly_crossed": []
        }
        out.update({"inserted": inserted, "usage": parts, "cumulative_worker_tokens": cumulative})
        return out

    def assess_trajectory(self, identity: RunIdentity, *, trigger: str, failures: list[str] | None = None) -> TrajectoryAssessment:
        contract = self._contract_for_identity(identity)
        projection = project(self.store, contract, identity)
        assessment = trajectory_logic.assess(
            engine=self.engine_factory(),
            store=self.store,
            identity=identity,
            contract=contract,
            projection=projection,
            trigger=str(trigger or "manual"),
            mode=self.mode,
            min_samples=self.calibration_min_samples,
            max_brier=self.calibration_max_brier,
            operator_override=self.enforcement_override,
            failures=failures,
        )
        if self._should_request_control(assessment):
            self.store.set_control(
                identity,
                control=assessment.control,
                decision_id=assessment.decision_id,
                payload=assessment.as_dict(),
                created_at=utc_now(),
            )
        return assessment

    def label_trajectory(self, decision_id: str, *, successful: bool, outcome: str = "") -> None:
        self.store.label_trajectory(decision_id, {"successful": bool(successful), "outcome": str(outcome), "labeled_at": utc_now()})

    def calibration(self) -> dict[str, Any]:
        report = trajectory_logic.calibration_report(self.store)
        report["enforcement_eligible"] = trajectory_logic.enforcement_eligible(
            self.store,
            min_samples=self.calibration_min_samples,
            max_brier=self.calibration_max_brier,
            operator_override=self.enforcement_override,
        )
        return report

    # --------------------------- checkpoint / control ---------------------------
    def checkpoint_packet(
        self,
        identity: RunIdentity,
        *,
        decision_id: str,
        reason: str,
        current_plan: str = "",
        test_summary: str = "",
        artifacts: list[dict[str, Any]] | None = None,
        workspace_result: dict[str, Any] | None = None,
        complete: bool = False,
    ) -> dict[str, Any]:
        contract = self._contract_for_identity(identity, allow_stale=True)
        packet = build_checkpoint(
            identity,
            project(self.store, contract, identity if self.store.is_current(identity) else None),
            decision_id=decision_id,
            reason=reason,
            current_plan=current_plan,
            test_summary=test_summary,
            artifacts=artifacts,
            workspace_result=workspace_result,
            complete=complete,
        )
        payload = packet.as_dict()
        self.store.save_checkpoint(payload)
        if complete and self.store.is_current(identity):
            try:
                self.store.acknowledge_control(identity)
            except Exception:
                pass
        return payload

    def control_for_run(self, identity: RunIdentity) -> dict[str, Any] | None:
        if not self.store.is_current(identity):
            return {"control": "STALE", "message": "run is no longer authoritative"}
        return self.store.control(identity)

    # --------------------------- projections ---------------------------
    def projection(self, task_id: str) -> WorkProjection:
        contract = self.active_contract(task_id)
        if not contract:
            raise KeyError(f"task {task_id!r} has no active supervision contract")
        identity = self.store.current_identity(task_id)
        return project(self.store, contract, identity)

    def status(self, task_id: str) -> dict[str, Any]:
        contract = self.active_contract(task_id)
        if not contract:
            return {"supervised": False, "task_id": task_id}
        identity = self.store.current_identity(task_id)
        projection = project(self.store, contract, identity)
        return {
            "supervised": True,
            "contract": contract.as_dict(),
            "identity": identity.as_dict() if identity else None,
            "projection": projection.as_dict(),
            "checkpoint": self.store.latest_checkpoint(task_id),
            "control": self.store.control(identity) if identity else None,
            "calibration": self.calibration(),
            "mode": self.mode,
            "economics": self.economics(identity) if identity else None,
            "nerve": self.nerve_status(identity) if identity else None,
        }

    def nerve_status(self, identity: RunIdentity) -> dict[str, Any]:
        from .nerve import evaluate
        from .runtime import settings
        control = self.control_for_run(identity)
        lifecycle_state = ""
        if str((control or {}).get("control") or "") == "COMPLETE_READY":
            lifecycle_state = str(((control or {}).get("payload") or {}).get("lifecycle_state") or "VERIFIED")
        return evaluate(self, identity, lifecycle_state=lifecycle_state, cfg=settings()).as_dict()

    def economics(self, identity: RunIdentity) -> dict[str, Any]:
        from .runtime import settings
        cfg = settings()
        worker = self.store.api_usage_total(identity)
        supervisor_tokens = self.store.supervisor_usage_total(identity)
        contract = self._contract_for_identity(identity, allow_stale=True)
        allowance = int(contract.allocated_tokens * float(cfg.get("supervisor_budget_fraction", 0.03)))
        rows = self.store.trajectory_rows(task_id=identity.task_id)
        avoided = sum(int((r.get("payload") or {}).get("estimated_tokens_avoided") or 0) for r in rows if int(r.get("run_id") or -1) == identity.run_id)
        return {
            "worker_tokens": worker,
            "supervisor_tokens": supervisor_tokens,
            "combined_tokens": worker + supervisor_tokens,
            "supervisor_budget_allowance": allowance,
            "supervisor_budget_remaining": max(0, allowance - supervisor_tokens),
            "estimated_worker_tokens_avoided": avoided,
            "estimated_net_tokens_saved": avoided - supervisor_tokens,
        }

    def stage_directive(self, identity: RunIdentity, assessment: TrajectoryAssessment) -> None:
        if assessment.directive:
            self.store.stage_directive(
                identity,
                decision_id=assessment.decision_id,
                directive=assessment.directive,
                confidence=assessment.confidence,
                created_at=utc_now(),
            )

    def consume_directive(self, identity: RunIdentity) -> dict[str, Any] | None:
        return self.store.consume_directive(identity)

    # --------------------------- internals ---------------------------
    def _contract_for_identity(self, identity: RunIdentity, *, allow_stale: bool = False) -> DoDContract:
        row = self.store.contract_row(identity.contract_hash)
        if not row:
            raise ValueError("unknown supervision contract")
        contract = contract_from_row(row)
        if contract.task_id != identity.task_id:
            raise ValueError("contract/task identity mismatch")
        if not allow_stale and not self.store.is_current(identity):
            raise ValueError("stale run cannot mutate current supervision state")
        return contract

    def _should_request_control(self, assessment: TrajectoryAssessment) -> bool:
        if assessment.control not in {"WATCH", "REPLAN", "BLOCK"}:
            return False
        if assessment.confidence < self.control_confidence:
            return False
        if self.mode == "shadow":
            return False
        if self.mode == "advisory":
            return assessment.control in {"WATCH", "REPLAN", "BLOCK"}
        return assessment.enforcement_eligible

    def _record_result_usage(self, identity: RunIdentity, result: Any, *, purpose: str) -> None:
        parts = usage_parts(getattr(result, "usage", {}) or {})
        if parts["accounted_tokens"] <= 0:
            return
        self.store.record_supervisor_usage(
            identity,
            purpose=purpose,
            input_tokens=parts["input_tokens"],
            output_tokens=parts["output_tokens"],
            reasoning_tokens=parts["reasoning_tokens"],
            total_tokens=parts["accounted_tokens"],
            created_at=utc_now(),
        )


def normalize_mode(mode: str) -> str:
    value = str(mode or "shadow").strip().lower()
    return value if value in {"shadow", "advisory", "enforce"} else "shadow"


def default_store_path() -> Path:
    explicit = str(os.getenv("HERMES_NERVE_SUPERVISION_DB") or "").strip()
    return Path(explicit).expanduser() if explicit else hermes_home() / "nerve" / "work-supervision.sqlite3"


def normalize_checkpoints(values: list[float] | tuple[float, ...]) -> tuple[float, ...]:
    out: list[float] = []
    for raw in values or ():
        value = float(raw)
        if not 0.0 < value < 1.0:
            raise ValueError("checkpoint fractions must be between 0 and 1")
        if value not in out:
            out.append(value)
    return tuple(sorted(out))


def validate_criteria(raw: list[dict[str, Any]]) -> list[CriterionSpec]:
    if not isinstance(raw, list) or not raw:
        raise ValueError("criteria must be a non-empty list")
    specs: list[CriterionSpec] = []
    ids: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("each criterion must be an object")
        cid = str(item.get("id") or "").strip()
        description = str(item.get("description") or "").strip()
        if not cid or not description:
            raise ValueError("criterion id and description are required")
        if cid in ids:
            raise ValueError(f"duplicate criterion id {cid!r}")
        ids.add(cid)
        weight = float(item.get("weight", 1.0))
        if weight < 0:
            raise ValueError("criterion weight cannot be negative")
        estimated = int(item.get("estimated_tokens", 0) or 0)
        if estimated < 0:
            raise ValueError("estimated_tokens cannot be negative")
        depends = tuple(str(x).strip() for x in (item.get("depends_on") or []) if str(x).strip())
        evidence = tuple(str(x).strip() for x in (item.get("evidence_requirements") or []) if str(x).strip())
        specs.append(
            CriterionSpec(
                id=cid,
                description=description,
                required=bool(item.get("required", True)),
                weight=weight,
                estimated_tokens=estimated,
                depends_on=depends,
                evidence_requirements=evidence,
            )
        )
    if not any(c.required for c in specs):
        raise ValueError("at least one criterion must be required")
    for c in specs:
        missing = [d for d in c.depends_on if d not in ids]
        if missing:
            raise ValueError(f"criterion {c.id!r} depends on unknown criteria {missing}")
    _assert_acyclic(specs)
    return specs


def _assert_acyclic(specs: list[CriterionSpec]) -> None:
    deps = {c.id: set(c.depends_on) for c in specs}
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str) -> None:
        if node in visited:
            return
        if node in visiting:
            raise ValueError("criterion dependency graph contains a cycle")
        visiting.add(node)
        for dep in deps.get(node, ()):
            visit(dep)
        visiting.remove(node)
        visited.add(node)

    for node in deps:
        visit(node)


def contract_from_row(row: dict[str, Any]) -> DoDContract:
    payload = json.loads(str(row["payload_json"]))
    criteria = tuple(validate_criteria(list(payload.get("criteria") or [])))
    checkpoints = tuple(float(x) for x in json.loads(str(row["checkpoint_json"])))
    return DoDContract(
        task_id=str(row["task_id"]),
        version=int(row["version"]),
        goal=str(row["goal"]),
        criteria=criteria,
        reserve_tokens=int(row["reserve_tokens"]),
        checkpoint_fractions=checkpoints,
        contract_hash=str(row["contract_hash"]),
        locked_at=str(row["locked_at"]),
        actor=str(row["actor"]),
        supersedes_hash=str(row["supersedes_hash"]) if row.get("supersedes_hash") else None,
        receipt_id=str(row.get("receipt_id") or ""),
    )


def runtime_identity_from_env(task_id: str | None = None) -> RunIdentity | None:
    # Dispatcher-pinned env is canonical for a Kanban worker. Hook ``task_id``
    # may denote an inner/subtask identifier on some Hermes call paths.
    tid = str(os.getenv("HERMES_KANBAN_TASK_ID") or os.getenv("HERMES_KANBAN_TASK") or task_id or "").strip()
    raw_run = str(os.getenv("HERMES_KANBAN_RUN_ID") or "").strip()
    contract_hash = str(os.getenv("HERMES_NERVE_DOD_HASH") or "").strip()
    claim = str(
        os.getenv("HERMES_KANBAN_CLAIM_IDENTITY")
        or os.getenv("HERMES_KANBAN_CLAIM_LOCK")
        or os.getenv("HERMES_KANBAN_CLAIM_TOKEN")
        or ""
    ).strip()
    worker = str(os.getenv("HERMES_KANBAN_WORKER_ID") or os.getenv("HERMES_PROFILE") or "").strip() or None
    if not tid or not raw_run or not contract_hash or not claim:
        return None
    try:
        run_id = int(raw_run)
    except ValueError:
        return None
    return RunIdentity(tid, run_id, contract_hash, claim, worker)
