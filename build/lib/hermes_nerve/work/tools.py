from __future__ import annotations

import json
import os
import uuid
from typing import Any

from ..provenance import execution_provenance
from .kanban_adapter import CanonicalKanbanAdapter
from .models import RunIdentity, WorkEvent
from .runtime import enabled, reviewer, supervisor
from .supervisor import runtime_identity_from_env


def _ok(**payload: Any) -> str:
    return json.dumps(
        {
            "ok": True,
            **payload,
            "execution": execution_provenance(live_provider_call=False, transport="local-work-supervision"),
        },
        sort_keys=True,
        ensure_ascii=False,
        default=str,
    )


def _error(exc: Exception) -> str:
    return json.dumps(
        {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "execution": execution_provenance(live_provider_call=False, transport="local-work-supervision", error=True),
        },
        sort_keys=True,
    )


def _require_enabled() -> None:
    if not enabled():
        raise RuntimeError("distributed card supervision is disabled")


def _current_identity(task_id: str = "") -> RunIdentity:
    env_identity = runtime_identity_from_env(task_id or None)
    if env_identity is not None:
        return env_identity
    tid = str(task_id or "").strip()
    if not tid:
        raise ValueError("task_id is required outside a supervised worker runtime")
    identity = supervisor().store.current_identity(tid)
    if identity is None:
        raise ValueError("task has no active supervised run")
    return identity


def nerve_supervise_card(args: dict, **kwargs) -> str:
    try:
        _require_enabled()
        action = str(args.get("action") or "status").strip().lower()
        sup = supervisor()
        if action in {"bind", "amend"}:
            result = sup.bind_contract(
                task_id=str(args.get("task_id") or ""),
                goal=str(args.get("goal") or ""),
                criteria=list(args.get("criteria") or []),
                reserve_tokens=int(args.get("reserve_tokens") or 0),
                checkpoint_fractions=list(args.get("checkpoint_fractions") or [0.40, 0.70]),
                actor=str(args.get("actor") or "orchestrator"),
                amend=action == "amend",
            )
            return _ok(action=action, result=result)
        if action == "bind_run":
            identity = RunIdentity(
                task_id=str(args.get("task_id") or ""),
                run_id=int(args.get("run_id")),
                contract_hash=str(args.get("contract_hash") or ""),
                claim_identity=str(args.get("claim_identity") or ""),
                worker_id=str(args.get("worker_id") or "") or None,
            )
            projection = sup.bind_run(identity)
            return _ok(action=action, identity=identity.as_dict(), projection=projection.as_dict())
        task_id = str(args.get("task_id") or kwargs.get("task_id") or "")
        if action == "status":
            return _ok(action=action, status=sup.status(task_id))
        identity = _current_identity(task_id)
        if action == "verify_criterion":
            return _ok(action=action, result=sup.verify_criterion(identity, str(args.get("criterion_id") or "")))
        if action == "record_usage":
            result = sup.record_usage(
                identity,
                consumed_tokens=int(args.get("consumed_tokens") or 0),
                source=str(args.get("source") or "estimated"),
            )
            assessments = []
            for fraction in result.get("newly_crossed") or []:
                assessments.append(sup.assess_trajectory(identity, trigger=f"budget_checkpoint:{fraction}").as_dict())
            return _ok(action=action, result=result, assessments=assessments)
        if action == "assess_trajectory":
            assessment = sup.assess_trajectory(
                identity,
                trigger=str(args.get("trigger") or "manual"),
                failures=[str(x) for x in (args.get("failures") or [])],
            )
            return _ok(action=action, assessment=assessment.as_dict(), control=sup.control_for_run(identity))
        if action == "checkpoint":
            packet = sup.checkpoint_packet(
                identity,
                decision_id=str(args.get("decision_id") or "manual"),
                reason=str(args.get("reason") or "manual checkpoint"),
                current_plan=str(args.get("current_plan") or ""),
                test_summary=str(args.get("test_summary") or ""),
                artifacts=list(args.get("artifacts") or []),
                workspace_result=dict(args.get("workspace_result") or {}),
                complete=bool(args.get("checkpoint_complete", True)),
            )
            return _ok(action=action, checkpoint=packet)
        if action == "label_outcome":
            sup.label_trajectory(
                str(args.get("decision_id") or ""),
                successful=bool(args.get("successful")),
                outcome=str(args.get("outcome") or ""),
            )
            return _ok(action=action, calibration=sup.calibration())
        if action == "request_review":
            checkpoint = sup.store.latest_checkpoint(identity.task_id)
            if not checkpoint or not checkpoint.get("checkpoint_complete"):
                raise ValueError("a complete checkpoint is required before canonical review handoff")
            adapter = CanonicalKanbanAdapter()
            ok, reason = adapter.request_review(
                task_id=identity.task_id,
                expected_run_id=identity.run_id,
                summary=str(args.get("summary") or checkpoint.get("reason") or "Nerve trajectory handoff"),
                metadata={
                    "jev_supervision": {
                        "decision_id": checkpoint.get("decision_id"),
                        "checkpoint_id": checkpoint.get("checkpoint_id"),
                        "contract_hash": identity.contract_hash,
                        "frontier": checkpoint.get("frontier") or [],
                        "budget": checkpoint.get("budget") or {},
                    }
                },
                reviewer=str(args.get("reviewer") or reviewer() or ""),
            )
            return _ok(action=action, transitioned=ok, reason=reason, checkpoint=checkpoint)
        raise ValueError(f"unsupported supervise action {action!r}")
    except Exception as exc:
        return _error(exc)


def nerve_work_event(args: dict, **kwargs) -> str:
    try:
        # Remote workers intentionally do not open the controller supervision DB.
        # Their structured tool call is the wire event: the controller parses it
        # from stream-json and applies it to the exact canonical run.
        if str(os.getenv("HERMES_NERVE_REMOTE_MODE") or "").strip() == "1":
            identity = runtime_identity_from_env(str(args.get("task_id") or kwargs.get("task_id") or "") or None)
            if identity is None:
                raise ValueError("remote supervised worker is missing exact run identity")
            return _ok(
                remote_stream_ack=True,
                event={
                    "event_type": str(args.get("event_type") or args.get("type") or "").strip().upper(),
                    "criterion_id": str(args.get("criterion_id") or "").strip() or None,
                    "payload": dict(args.get("payload") or {}),
                    **identity.as_dict(),
                },
            )
        _require_enabled()
        identity = _current_identity(str(args.get("task_id") or kwargs.get("task_id") or ""))
        event_type = str(args.get("event_type") or args.get("type") or "").strip().upper()
        event = WorkEvent(
            event_id=str(args.get("event_id") or "jevwork-" + uuid.uuid4().hex),
            event_type=event_type,
            criterion_id=str(args.get("criterion_id") or "").strip() or None,
            payload=dict(args.get("payload") or {}),
            source="worker",
        )
        projection = supervisor().record_event(identity, event)
        checkpoint = None
        review_result = None
        if event_type == "CHECKPOINT_READY":
            payload = dict(args.get("payload") or {})
            control = supervisor().control_for_run(identity) or {}
            checkpoint = supervisor().checkpoint_packet(
                identity,
                decision_id=str(control.get("decision_id") or payload.get("decision_id") or "worker-checkpoint"),
                reason=str(payload.get("reason") or f"Checkpoint for {control.get('control') or 'handoff'}"),
                current_plan=str(payload.get("current_plan") or ""),
                test_summary=str(payload.get("test_summary") or ""),
                artifacts=list(payload.get("artifacts") or []),
                workspace_result=dict(payload.get("workspace_result") or {}),
                complete=True,
            )
            if str(control.get("control") or "") in {"WATCH", "REPLAN", "BLOCK"}:
                try:
                    ok, reason = CanonicalKanbanAdapter().request_review(
                        task_id=identity.task_id,
                        expected_run_id=identity.run_id,
                        summary=str(checkpoint.get("reason") or "Nerve checkpoint handoff"),
                        metadata={"jev_supervision": checkpoint},
                        reviewer=reviewer(),
                    )
                    review_result = {"transitioned": ok, "reason": reason}
                except Exception as exc:
                    review_result = {"transitioned": False, "reason": f"{type(exc).__name__}: {exc}"}
        return _ok(
            event_id=event.event_id,
            projection=projection.as_dict(),
            checkpoint=checkpoint,
            review=review_result,
            control=supervisor().control_for_run(identity),
        )
    except Exception as exc:
        return _error(exc)


def nerve_work_status(args: dict, **kwargs) -> str:
    try:
        if str(os.getenv("HERMES_NERVE_REMOTE_MODE") or "").strip() == "1":
            identity = runtime_identity_from_env(str(args.get("task_id") or kwargs.get("task_id") or "") or None)
            return _ok(
                remote_worker=True,
                identity=identity.as_dict() if identity else None,
                control_file=str(os.getenv("HERMES_NERVE_REMOTE_CONTROL_FILE") or ""),
            )
        _require_enabled()
        task_id = str(args.get("task_id") or kwargs.get("task_id") or "").strip()
        if not task_id:
            identity = runtime_identity_from_env()
            if identity is None:
                raise ValueError("task_id is required outside a supervised worker runtime")
            task_id = identity.task_id
        return _ok(status=supervisor().status(task_id))
    except Exception as exc:
        return _error(exc)
