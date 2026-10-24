from __future__ import annotations

import json
from typing import Any

from ..provenance import execution_provenance
from ..work.runtime import supervisor as work_supervisor
from ..work.supervisor import runtime_identity_from_env
from .execution import RemoteManager


def _ok(**payload: Any) -> str:
    return json.dumps(
        {"ok": True, **payload, "execution": execution_provenance(live_provider_call=False, transport="ssh-remote-execution")},
        sort_keys=True, ensure_ascii=False, default=str,
    )


def _error(exc: Exception) -> str:
    return json.dumps(
        {"ok": False, "error": f"{type(exc).__name__}: {exc}", "execution": execution_provenance(live_provider_call=False, transport="ssh-remote-execution", error=True)},
        sort_keys=True,
    )


def nerve_remote_delegate_task(args: dict, **kwargs) -> str:
    try:
        task_id = str(args.get("task_id") or "").strip()
        identity = runtime_identity_from_env(task_id or None)
        if identity is None and task_id:
            identity = work_supervisor().store.current_identity(task_id)
        supervision = None
        db = ""
        mode = "shadow"
        if identity is not None:
            sup = work_supervisor()
            contract = sup.active_contract(identity.task_id)
            if contract is None:
                raise ValueError("supervised remote delegation requires a locked Definition of Done")
            projection = sup.projection(identity.task_id)
            supervision = {"contract": contract.as_dict(), "projection": projection.as_dict()}
            db = str(sup.store.path)
            mode = sup.mode
        execution = RemoteManager().start(
            host_alias=str(args.get("host") or ""),
            goal=str(args.get("goal") or ""),
            context=str(args.get("context") or ""),
            workspace=str(args.get("workspace") or "") or None,
            profile=str(args.get("profile") or "") or None,
            max_turns=args.get("max_turns"),
            identity=identity,
            supervision=supervision,
            supervision_db=db,
            supervision_mode=mode,
            require_claim_fence=bool(args.get("require_claim_fence", bool(identity))),
        )
        return _ok(job=execution.status())
    except Exception as exc:
        return _error(exc)


def nerve_remote_worker_status(args: dict, **kwargs) -> str:
    try:
        job = RemoteManager().execution(str(args.get("job_id") or "")).status()
        return _ok(job=job)
    except Exception as exc:
        return _error(exc)


def nerve_remote_worker_result(args: dict, **kwargs) -> str:
    try:
        execution = RemoteManager().execution(str(args.get("job_id") or ""))
        timeout = args.get("wait_seconds")
        if timeout not in (None, "", 0, 0.0):
            execution.wait(timeout=float(timeout))
        return _ok(result=execution.result())
    except Exception as exc:
        return _error(exc)


def nerve_remote_worker_cancel(args: dict, **kwargs) -> str:
    try:
        return _ok(job=RemoteManager().execution(str(args.get("job_id") or "")).cancel())
    except Exception as exc:
        return _error(exc)


def nerve_remote_worker_control(args: dict, **kwargs) -> str:
    try:
        control = str(args.get("control") or "STOP_REQUESTED").strip().upper()
        if control not in {"WATCH", "REPLAN", "BLOCK", "STOP_REQUESTED"}:
            raise ValueError("control must be WATCH, REPLAN, BLOCK, or STOP_REQUESTED")
        payload = RemoteManager().execution(str(args.get("job_id") or "")).request_control({
            "control": control,
            "decision_id": str(args.get("decision_id") or ""),
            "reason": str(args.get("reason") or ""),
        })
        return _ok(control=payload)
    except Exception as exc:
        return _error(exc)
