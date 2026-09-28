from __future__ import annotations

import json
import os
import shlex
from contextvars import ContextVar
from types import SimpleNamespace
from typing import Any

from .autobind import ensure_kanban_binding
from .deterministic import canonical_completion_summary, observe_test_result
from .runtime import dispatch_tool, enabled, reviewer, settings, supervisor, tool_dispatcher_available
from .router import recent_failure_fingerprints, should_call_provider
from .nerve import evaluate as evaluate_nerve, forecast_extension
from .supervisor import runtime_identity_from_env

_INTERNAL_PREFIXES = ("jev_",)
_DELEGATED_CHILD_ENV_MARKER = "HERMES_DELEGATED_CHILD_CONTEXT"
_TERMINAL_READY_CONTROL = "COMPLETE_READY"
_TERMINAL_READY_RULE = "jev:work-terminal-ready"
_CONTROLLER_COMPLETION_DISPATCH = ContextVar("jev_controller_completion_dispatch", default=False)
_CONTROLLER_COMPLETION_ATTEMPTS = 3

_CHECKPOINT_SAFE_TOOLS = {
    "nerve_work_event", "nerve_work_status", "kanban_request_review", "kanban_block",
    "read_file", "search_files", "file_read", "file_search",
}


def _serialized_args(args: dict[str, Any] | None) -> str:
    try:
        return json.dumps(args or {}, sort_keys=True, ensure_ascii=False, default=str).lower()
    except Exception:
        return str(args or {}).lower()


def _kanban_authority_bypass(tool_name: str, args: dict[str, Any] | None) -> str | None:
    """Return a reason when a generic worker tool tries to mutate Kanban authority.

    Dispatcher-owned workers must use the registered ``kanban_*`` lifecycle tools.
    This is not OS sandboxing; it is a fail-closed plugin policy that blocks the
    concrete escape paths observed in dev10 (CLI lifecycle mutation, worker-authored
    SQLite mutators, and direct task-status SQL).
    """
    name = str(tool_name or "")
    if name in {"kanban_complete", "kanban_block", "kanban_request_review", "kanban_request_changes"}:
        return None
    blob = _serialized_args(args)
    lifecycle_cli = (
        "hermes kanban complete",
        "hermes kanban block",
        "hermes kanban request-review",
        "hermes kanban request_review",
        "hermes kanban request-changes",
        "hermes kanban request_changes",
    )
    mutation_markers = (
        "update tasks", "insert into tasks", "delete from tasks", "replace into tasks",
        "complete_task(", ".complete_task(", "status = 'done'", 'status = "done"',
        "status='done'", 'status="done"', "_kanban_done.py", "_finish_kanban.py",
    )
    board_markers = ("kanban.db", "/.hermes/kanban/", "hermes_kanban_db")

    if name in {"tool_call", "tool_search", "tool_describe"} and any(
        lifecycle in blob for lifecycle in ("kanban_complete", "kanban_block", "kanban_request_review", "kanban_request_changes")
    ):
        return "Kanban lifecycle tools are directly listed; generic tool wrappers/searches cannot be used for lifecycle mutation."
    if name in {"terminal", "terminal.exec", "shell", "bash"}:
        if any(marker in blob for marker in lifecycle_cli) or ("hermes kanban" in blob and any(token in blob for token in (" complete ", " block ", " request-review ", " request-changes "))):
            return "Kanban lifecycle mutation must use controller-owned native lifecycle dispatch, not terminal/CLI."
        if any(script in blob for script in ("_kanban_done.py", "_finish_kanban.py")):
            return "Executing a worker-authored Kanban completion script is forbidden; use the native kanban tool."
        if any(marker in blob for marker in mutation_markers) and (
            any(marker in blob for marker in board_markers) or "sqlite3" in blob
        ):
            return "Direct Kanban database mutation is forbidden from a supervised worker."
    if name in {"write_file", "file_write", "patch", "apply_patch", "edit_file"}:
        if any(marker in blob for marker in mutation_markers) and (
            any(marker in blob for marker in board_markers) or "sqlite3" in blob or "kanban" in blob
        ):
            return "Writing a Kanban database/lifecycle bypass script is forbidden; use the native kanban tool."
    return None


def _completion_decision_id(identity, verdict) -> str:
    return str(getattr(verdict, "receipt_id", "") or getattr(verdict, "decision_id", "") or f"completion-ready-{identity.run_id}")


def _set_terminal_lifecycle(sup, identity, verdict, state: str, *, attempts: int = 0, last_result: str = "") -> None:
    """Persist the controller-owned completion state without introducing a second scheduler.

    ``COMPLETE_READY`` remains the durable control key for backward compatibility
    with dev10-dev14 stores. Dev15 adds a lifecycle state inside its payload so a
    verified run can be retried/reconciled without waking the model.
    """
    from .models import utc_now
    sup.store.set_control(
        identity,
        control=_TERMINAL_READY_CONTROL,
        decision_id=_completion_decision_id(identity, verdict),
        payload={
            "reason": str(getattr(verdict, "reason", "") or "Nerve verified completion."),
            "confidence": float(getattr(verdict, "confidence", 1.0) or 0.0),
            "receipt_id": str(getattr(verdict, "receipt_id", "") or ""),
            "lifecycle_state": str(state),
            "controller_completion_attempts": int(attempts),
            "last_native_result": str(last_result or "")[:1200],
        },
        created_at=utc_now(),
    )


def _arm_terminal_ready(sup, identity, verdict) -> None:
    """Freeze a verified run before the controller performs the terminal transition."""
    try:
        sup.store.acknowledge_control(identity)
    except Exception:
        pass
    _set_terminal_lifecycle(sup, identity, verdict, "VERIFIED")


def _prior_verdict(control: dict[str, Any]):
    payload = dict(control.get("payload") or {})
    return SimpleNamespace(
        reason=str(payload.get("reason") or "Nerve completion already verified."),
        confidence=float(payload.get("confidence") or 1.0),
        receipt_id=str(payload.get("receipt_id") or control.get("decision_id") or ""),
        decision_id=str(control.get("decision_id") or ""),
    )


def _terminal_state(control: dict[str, Any] | None) -> str:
    if not control or str(control.get("control") or "") != _TERMINAL_READY_CONTROL:
        return ""
    return str((control.get("payload") or {}).get("lifecycle_state") or "VERIFIED")


def _worker_completion_intent(tool_name: str, args: dict[str, Any] | None) -> bool:
    """Recognize attempts by the model to finish the board outside controller hooks."""
    name = str(tool_name or "")
    if name == "kanban_complete":
        return True
    blob = _serialized_args(args)
    markers = (
        "kanban_complete", "hermes kanban complete", "complete_task(", ".complete_task(",
        "_kanban_done.py", "_finish_kanban.py", "complete_task.py", "complete_task2.py",
        "canonicalkanbanadapter", "adapter.complete(", "hermes_nerve.work.kanban_adapter",
    )
    if any(marker in blob for marker in markers):
        return True
    return "hermes kanban" in blob and " complete " in f" {blob} "


def _paperclip_completion_intent(tool_name: str, args: dict[str, Any] | None) -> bool:
    """Recognize attempts by a Paperclip worker to transition its own issue.

    The Kanban completion fence cannot see these: a ``hermes_local`` worker
    mutates the issue through Paperclip's HTTP API (curl or the paperclip
    client), so no Kanban tool name or marker appears in the call. For runs
    Nerve supervises, the verified transition belongs to the controller, not
    to the worker.
    """
    name = str(tool_name or "")
    if name not in {"terminal", "execute_code", "process"}:
        return False
    blob = _serialized_args(args)
    if "/api/issues/" in blob and "in_review" in blob:
        return True
    if "request_review(" in blob and "paperclipclient" in blob.lower():
        return True
    # jq-variable PATCH shape: the status literal only appears as a jq
    # --arg, with the curl carrying "$api/issues/<id>".
    lowered = blob.lower()
    if (
        ("--arg status in_review" in lowered or "'{status:$status" in lowered)
        and ("/api/issues/" in blob or "issues/" in blob)
        and ("curl" in lowered or "patch" in lowered)
    ):
        return True
    return False


def _native_completion_args(sup, identity, proposal: dict[str, Any], verdict) -> dict[str, Any]:
    # dev13: when deterministic authority proved the locked DoD, the terminal
    # summary is synthesized from controller-observed verdicts rather than trusting
    # the worker to phrase every label/command/result exactly. This makes DOD-08 a
    # real harness-owned deliverable and removes a semantic completion veto.
    deterministic_summary = None
    try:
        deterministic_summary = canonical_completion_summary(sup, identity)
    except Exception:
        deterministic_summary = None
    summary = str(
        deterministic_summary
        or proposal.get("final_response")
        or proposal.get("response")
        or proposal.get("summary")
        or proposal.get("result")
        or "Nerve verified the locked Definition of Done."
    ).strip()
    return {
        "summary": summary[:12000],
        "result": "Nerve verified completion.",
        "metadata": {
            "hermes_nerve": {
                "verified": True,
                "run_id": identity.run_id,
                "contract_hash": identity.contract_hash,
                "receipt_id": str(verdict.receipt_id or ""),
                "confidence": float(verdict.confidence or 0.0),
                "authority": "deterministic" if str(verdict.receipt_id or "") == "deterministic" else "semantic",
            }
        },
    }


def _native_dispatch_succeeded(result: Any) -> tuple[bool, str]:
    """Interpret Hermes tool-dispatch results conservatively.

    Current Hermes returns JSON-ish tool output for built-ins, but plugin hosts and
    offline test doubles may return dicts or plain strings. Only an explicit success
    shape (``ok: true``) or an unambiguous success string is accepted; structured
    errors and empty results fail closed.
    """
    if result is None:
        return False, ""
    if isinstance(result, dict):
        text = json.dumps(result, sort_keys=True, ensure_ascii=False, default=str)
        if result.get("ok") is True:
            return True, text
        if result.get("ok") is False or result.get("error") or result.get("tool_error"):
            return False, text
        return False, text

    text = str(result).strip()
    if not text:
        return False, text
    try:
        parsed = json.loads(text)
    except Exception:
        parsed = None
    if isinstance(parsed, dict):
        if parsed.get("ok") is True:
            return True, text
        if parsed.get("ok") is False or parsed.get("error") or parsed.get("tool_error"):
            return False, text

    low = text.lower()
    if any(token in low for token in ("tool_error", '"error"', "could not complete", "refused:", "failed", "not allowed")):
        return False, text
    if any(token in low for token in ('"ok": true', '"ok":true', "status=done", "status: done", "completed")):
        return True, text
    return False, text


def _dispatch_native_completion(sup, identity, proposal: dict[str, Any], verdict) -> tuple[bool, str]:
    """Complete via Hermes' registered kanban_complete tool from the hook itself."""
    from .models import utc_now
    token = _CONTROLLER_COMPLETION_DISPATCH.set(True)
    try:
        result = dispatch_tool("kanban_complete", _native_completion_args(sup, identity, proposal, verdict))
        ok, text = _native_dispatch_succeeded(result)
        sup.store.add_diagnostic(
            task_id=identity.task_id, run_id=identity.run_id, kind="completion_native_dispatch",
            payload={"ok": ok, "result": text[:2000]}, created_at=utc_now(),
        )
        if ok:
            # Hermes' text-stop guard decides whether to emit a synthetic
            # "call kanban_complete" nudge by inspecting conversation messages.
            # A hook-owned dispatch is intentionally invisible to that history,
            # so without this worker-local terminal marker Hermes would spend one
            # unnecessary provider turn after the card/run are already complete.
            # Disable only after explicit native success; failures stay fail-closed.
            os.environ["HERMES_KANBAN_STOP_NUDGE"] = "0"
            try:
                sup.store.add_diagnostic(
                    task_id=identity.task_id, run_id=identity.run_id,
                    kind="completion_stop_nudge_suppressed",
                    payload={"reason": "native kanban_complete already succeeded"},
                    created_at=utc_now(),
                )
            except Exception:
                pass
        return ok, text
    except Exception as exc:
        try:
            sup.store.add_diagnostic(
                task_id=identity.task_id, run_id=identity.run_id, kind="completion_native_dispatch_failed",
                payload={"error": f"{type(exc).__name__}: {exc}"}, created_at=utc_now(),
            )
        except Exception:
            pass
        return False, f"{type(exc).__name__}: {exc}"
    finally:
        _CONTROLLER_COMPLETION_DISPATCH.reset(token)


def _complete_verified_run(sup, identity, proposal: dict[str, Any], verdict, *, attempts: int | None = None) -> tuple[bool, str]:
    """Controller-owned, bounded terminal transition with no model retry loop.

    A verified run is frozen before this is called. Transient lifecycle failures are
    retried locally through the captured Hermes dispatcher. If every attempt fails,
    the durable state becomes ``COMPLETION_RETRY`` and later lifecycle hooks may
    retry it; the worker is *not* instructed to search for completion mechanisms.
    """
    from .models import utc_now
    if not tool_dispatcher_available():
        text = "Hermes plugin tool dispatcher is unavailable"
        _set_terminal_lifecycle(sup, identity, verdict, "COMPLETION_RETRY", attempts=0, last_result=text)
        sup.store.add_diagnostic(
            task_id=identity.task_id, run_id=identity.run_id, kind="completion_controller_deferred",
            payload={"reason": text}, created_at=utc_now(),
        )
        return False, text

    last = ""
    total = max(1, int(attempts if attempts is not None else settings().get("completion_controller_attempts", _CONTROLLER_COMPLETION_ATTEMPTS)))
    _set_terminal_lifecycle(sup, identity, verdict, "COMPLETING", attempts=0)
    for number in range(1, total + 1):
        ok, text = _dispatch_native_completion(sup, identity, proposal, verdict)
        last = text
        sup.store.add_diagnostic(
            task_id=identity.task_id, run_id=identity.run_id, kind="completion_controller_attempt",
            payload={"attempt": number, "max_attempts": total, "ok": bool(ok), "result": str(text)[:1200]},
            created_at=utc_now(),
        )
        if ok:
            _set_terminal_lifecycle(sup, identity, verdict, "COMPLETED", attempts=number, last_result=text)
            return True, text
    _set_terminal_lifecycle(sup, identity, verdict, "COMPLETION_RETRY", attempts=total, last_result=last)
    return False, last


def _owns_kanban_terminal_authority() -> bool:
    """True only for the dispatcher-owned Kanban worker context.

    Delegate-task children inherit the parent's ``HERMES_KANBAN_*`` environment,
    but Hermes deliberately fences them from mutating the board. Completion
    verification/control must respect that same authority boundary or a child can
    consume Jev calls and mutate the parent's supervision state without being able
    to issue the terminal Kanban tool that closes the run.

    The environment marker covers descendant processes. The lazy Hermes import
    additionally catches in-process ContextVar-scoped children. Offline/plugin
    package tests do not ship the Hermes ``agent`` package, so absence of that
    helper preserves the historical top-level behavior.
    """
    if str(os.getenv(_DELEGATED_CHILD_ENV_MARKER) or "").strip():
        return False
    try:
        from agent.delegation_context import is_dispatcher_owned_worker_context
    except Exception:
        return True
    try:
        return bool(is_dispatcher_owned_worker_context())
    except Exception:
        return True


def _paperclip_env_task() -> str:
    return str(os.getenv("PAPERCLIP_TASK_ID") or "").strip()


def _identity(task_id: str | None = None, *, session_id: str = ""):
    identity = runtime_identity_from_env(task_id or os.getenv("HERMES_KANBAN_TASK") or None)
    if identity is not None:
        return identity
    try:
        from .paperclip_runtime import current_identity as paperclip_identity
        identity = paperclip_identity()
    except Exception:
        identity = None
    if identity is not None:
        return identity
    # Paperclip workers carry PAPERCLIP_* env, but hook callbacks run in
    # Hermes-delivered contexts where the startup bootstrap ContextVar is not
    # visible and the Kanban env is absent. Resolve the durable startup
    # binding from the store so supervision hooks still bind the run.
    env_task = _paperclip_env_task()
    if enabled() and env_task:
        try:
            identity = supervisor().store.current_identity(env_task)
        except Exception:
            identity = None
        if identity is not None:
            return identity
    if not enabled():
        return None
    try:
        identity = ensure_kanban_binding(supervisor(), task_id=str(task_id or ""), session_id=session_id)
    except Exception:
        identity = None
    if identity is not None:
        return identity
    # Dispatcher-pinned identity wins over hook-local identifiers. Some Hermes
    # lifecycle paths pass a session/subtask id in ``task_id``; treating that as
    # canonical creates noisy ``autobind_task_missing`` rows and can detach the
    # completion audit from the owning run.
    tid = str(os.getenv("HERMES_KANBAN_TASK_ID") or os.getenv("HERMES_KANBAN_TASK") or task_id or "").strip()
    if tid:
        try:
            return supervisor().store.current_identity(tid)
        except Exception:
            return None
    return None


def _checkpoint_safe(tool_name: str, args: dict[str, Any]) -> bool:
    if tool_name in _CHECKPOINT_SAFE_TOOLS or tool_name.startswith(_INTERNAL_PREFIXES):
        return True
    if tool_name in {"terminal", "terminal.exec", "shell", "bash"}:
        command = str(args.get("command") or args.get("cmd") or "").strip()
        try:
            parts = shlex.split(command)
        except ValueError:
            return False
        if not parts:
            return True
        if parts[0] == "git" and len(parts) >= 2 and parts[1] in {"status", "diff", "add", "commit", "stash", "rev-parse"}:
            return True
    return False


def _maybe_assess(identity, *, trigger: str) -> None:
    sup = supervisor()
    decision = should_call_provider(sup, identity, trigger=trigger)
    if not decision.get("call"):
        try:
            sup.store.add_diagnostic(
                task_id=identity.task_id,
                run_id=identity.run_id,
                kind="provider_call_skipped",
                payload={"trigger": trigger, **decision},
                created_at=__import__("hermes_nerve.work.models", fromlist=["utc_now"]).utc_now(),
            )
        except Exception:
            pass
        return
    try:
        assessment = sup.assess_trajectory(
            identity,
            trigger=trigger,
            failures=list(decision.get("failures") or recent_failure_fingerprints(sup, identity)),
        )
    except Exception as exc:
        try:
            from .models import utc_now
            sup.store.add_diagnostic(
                task_id=identity.task_id,
                run_id=identity.run_id,
                kind="trajectory_provider_failed",
                payload={"trigger": trigger, "error": f"{type(exc).__name__}: {exc}"},
                created_at=utc_now(),
            )
        except Exception:
            pass
        return
    cfg = settings()
    threshold = float(cfg.get("directive_high_confidence", 0.90))
    watch_threshold = float(cfg.get("directive_watch_confidence", 0.75))
    required = watch_threshold if assessment.control == "WATCH" else threshold
    if assessment.directive and assessment.confidence >= required:
        sup.stage_directive(identity, assessment)




def bootstrap_kanban_worker(*, task_id: str = "", session_id: str = ""):
    """Eagerly bind a dispatcher-spawned worker before its first model call.

    This is local-only and provider-free. It exists so supervision validity is
    established at process startup instead of depending on a later hook/tool
    path. Failures stay non-fatal to Hermes but are durably diagnosable.
    """
    if not enabled():
        return None
    tid = str(os.getenv("HERMES_KANBAN_TASK_ID") or os.getenv("HERMES_KANBAN_TASK") or task_id or "").strip()
    if not tid:
        return None
    try:
        identity = ensure_kanban_binding(supervisor(), task_id=tid, session_id=session_id)
    except Exception as exc:
        try:
            from .models import utc_now
            supervisor().store.add_diagnostic(
                task_id=tid, kind="autobind_startup_exception",
                payload={"error": f"{type(exc).__name__}: {exc}"}, created_at=utc_now(),
            )
        except Exception:
            pass
        return None
    if identity is None:
        return None
    try:
        from .models import utc_now
        supervisor().store.add_diagnostic(
            task_id=identity.task_id, run_id=identity.run_id, kind="autobind_startup_bound",
            payload={"contract_hash": identity.contract_hash, "session_id": session_id}, created_at=utc_now(),
        )
    except Exception:
        pass
    return identity


def pre_llm_call(*, task_id: str = "", session_id: str = "", **kwargs: Any):
    """One-time turn prologue: silently bind the canonical run + structured DoD.

    Returns no context on healthy startup. The worker does not pay an explanatory
    prompt tax merely because supervision is present.
    """
    if not enabled():
        return None
    identity = _identity(task_id or None, session_id=session_id)
    if identity is None:
        return None
    try:
        control = supervisor().control_for_run(identity)
        payload = dict((control or {}).get("payload") or {})
        if str((control or {}).get("control") or "") == "BLOCK" and payload.get("source") == "nerve_observer":
            return (
                "[JEV NERVE KILL SWITCH] This run has been controller-blocked for a confirmed runaway token trajectory. "
                "Do not continue implementation, search for alternate completion paths, or consume more provider turns. "
                "Preserve the workspace for controller/human review."
            )
    except Exception:
        pass
    return None


def pre_tool_call(tool_name: str, args: dict, task_id: str | None = None, session_id: str = "", **kwargs):
    if not enabled():
        return None
    identity = _identity(task_id, session_id=session_id)
    if identity is None:
        return None
    sup = supervisor()
    name = str(tool_name or "")

    try:
        from . import paperclip_runtime
        if paperclip_runtime.owns(identity) and name in {
            "kanban_complete", "kanban_block", "kanban_request_review", "kanban_request_changes"
        }:
            return {
                "action": "block",
                "message": "This run is governed by Paperclip issue lifecycle; Nerve will request review after verified completion.",
                "rule_key": "nerve:paperclip-lifecycle",
            }
    except Exception:
        pass

    # Paperclip workers reach their issue through the HTTP API (curl, the
    # paperclip client, CLI wrappers), not through Kanban tools, so the Kanban
    # completion fence never sees the transition. A controller-supervised
    # Paperclip run may not transition itself: intercept the completion intent
    # and replace it with controller verification + the authoritative
    # request_review handoff, mirroring the Kanban completion-intent path.
    paperclip_completion = False
    try:
        from . import paperclip_runtime as _pc_runtime
        if _pc_runtime.owns(identity) and _paperclip_completion_intent(name, args or {}):
            paperclip_completion = True
    except Exception:
        paperclip_completion = False

    if paperclip_completion and tool_dispatcher_available():
        try:
            verdict = sup.verify_completion(
                identity,
                proposal={
                    "summary": (args or {}).get("summary"),
                    "result": (args or {}).get("result"),
                    "trigger": "paperclip_completion_intent",
                    "tool_name": name,
                },
            )
        except Exception as exc:
            return {
                "action": "block",
                "message": f"Nerve controller could not verify completion: {type(exc).__name__}: {exc}",
                "rule_key": "nerve:paperclip-completion-error",
            }
        if verdict.allow:
            try:
                from . import paperclip_runtime
                outcome = paperclip_runtime.request_review(
                    sup,
                    identity,
                    verdict,
                    summary=str(verdict.reason),
                )
            except Exception as exc:
                return {
                    "action": "block",
                    "message": f"Nerve verified completion but the Paperclip review handoff failed: {type(exc).__name__}: {exc}",
                    "rule_key": "nerve:paperclip-handoff-error",
                }
            if outcome.remote_updated:
                return {
                    "action": "block",
                    "message": (
                        "Nerve verified the locked Definition of Done and handed the issue to review. "
                        "No further worker action is required."
                    ),
                    "rule_key": "nerve:paperclip-controller-completion",
                }
            return {
                "action": "block",
                "message": (
                    "Nerve verified completion locally, but the Paperclip review handoff is pending: "
                    + outcome.reason
                ),
                "rule_key": "nerve:paperclip-review-pending",
            }
        missing = f" Missing: {', '.join(verdict.missing_criteria)}" if verdict.missing_criteria else ""
        return {
            "action": "block",
            "message": verdict.reason + missing,
            "rule_key": "nerve:paperclip-completion",
        }
    if paperclip_completion:
        # No dispatcher available: fail closed rather than letting an
        # unsupervised transition through, and preserve the verified latch.
        return {
            "action": "block",
            "message": (
                "This Paperclip run is Nerve-supervised; issue transitions are controller-owned. "
                "Nerve will request review after verified completion."
            ),
            "rule_key": "nerve:paperclip-lifecycle",
        }

    # A controller-owned dispatch re-enters Hermes' normal tool pipeline. Let
    # that exact recursive kanban_complete invocation through; all model-originated
    # lifecycle attempts remain fenced below.
    if name == "kanban_complete" and bool(_CONTROLLER_COMPLETION_DISPATCH.get()):
        return None

    bypass_reason = _kanban_authority_bypass(name, args or {})
    completion_intent = _worker_completion_intent(name, args or {})

    try:
        terminal_control = sup.control_for_run(identity)
    except Exception:
        terminal_control = None

    # Dev15: the model never owns completion when the plugin has Hermes'
    # in-process dispatcher capability. Any completion intent (native tool,
    # CLI/search wrapper, direct adapter script) is converted into a controller
    # verification + native transition. This closes the Pair-3 loop where Solar
    # spent dozens of calls trying alternative completion mechanisms.
    if completion_intent and tool_dispatcher_available():
        if terminal_control and str(terminal_control.get("control") or "") == _TERMINAL_READY_CONTROL:
            state = _terminal_state(terminal_control)
            prior = _prior_verdict(terminal_control)
            if state != "COMPLETED":
                ok, native_result = _complete_verified_run(
                    sup, identity,
                    {"summary": (args or {}).get("summary"), "result": (args or {}).get("result"), "trigger": "worker_completion_intent"},
                    prior,
                )
            else:
                ok, native_result = True, "controller completion already recorded"
            return {
                "action": "block",
                "message": (
                    "Nerve controller owns terminal completion and has already completed this verified run. "
                    "Do not perform any additional completion work."
                    if ok else
                    "Nerve completion is already verified. Controller-owned native completion is deferred for lifecycle retry; "
                    "do not search for, script, or call alternate completion paths. "
                    f"Last native result: {str(native_result)[:500]}"
                ),
                "rule_key": "jev:work-controller-completion",
            }

        try:
            verdict = sup.verify_completion(
                identity,
                proposal={
                    "summary": (args or {}).get("summary"),
                    "result": (args or {}).get("result"),
                    "trigger": "worker_completion_intent",
                    "tool_name": name,
                },
            )
        except Exception as exc:
            return {
                "action": "block",
                "message": f"Nerve controller could not verify completion: {type(exc).__name__}: {exc}",
                "rule_key": "jev:work-completion-error",
            }
        if verdict.allow:
            try:
                _arm_terminal_ready(sup, identity, verdict)
            except Exception as exc:
                return {
                    "action": "block",
                    "message": f"Nerve verified completion but could not persist terminal state: {type(exc).__name__}: {exc}",
                    "rule_key": "jev:work-terminal-ready-error",
                }
            ok, native_result = _complete_verified_run(
                sup, identity,
                {"summary": (args or {}).get("summary"), "result": (args or {}).get("result"), "trigger": "worker_completion_intent"},
                verdict,
            )
            return {
                "action": "block",
                "message": (
                    "Nerve verified the locked Definition of Done and the controller completed the Kanban run. "
                    "No further worker action is required."
                    if ok else
                    "Nerve verified the locked Definition of Done. Controller-owned native completion is queued for lifecycle retry; "
                    "the worker must stop completion work. "
                    f"Last native result: {str(native_result)[:500]}"
                ),
                "rule_key": "jev:work-controller-completion",
            }
        return {
            "action": "block",
            "message": verdict.reason + (f" Missing: {', '.join(verdict.missing_criteria)}" if verdict.missing_criteria else ""),
            "rule_key": "jev:work-completion",
        }

    # Completion bypasses are always fenced, including delegated child contexts.
    # A child not owning board authority is a reason to block, never a reason to
    # let CLI/SQLite mutation escape supervision.
    if bypass_reason:
        return {
            "action": "block",
            "message": bypass_reason,
            "rule_key": "jev:work-kanban-authority",
        }

    # Backward-compatible fallback for plugin hosts that do not expose
    # dispatch_tool. Only the dispatcher-owned worker may call native completion.
    if terminal_control and str(terminal_control.get("control") or "") == _TERMINAL_READY_CONTROL:
        if name == "kanban_complete":
            if not _owns_kanban_terminal_authority():
                return {
                    "action": "block",
                    "message": "Nerve completion authority belongs to the dispatcher-owned Kanban worker.",
                    "rule_key": "jev:work-completion-authority",
                }
            return None
        return {
            "action": "block",
            "message": (
                "Nerve completion is already verified for this exact run. "
                "Only the native kanban_complete fallback is permitted; generic shell/search/file work is fenced."
            ),
            "rule_key": _TERMINAL_READY_RULE,
        }

    if name == "kanban_complete":
        if not _owns_kanban_terminal_authority():
            return {
                "action": "block",
                "message": "Nerve completion authority belongs to the dispatcher-owned Kanban worker.",
                "rule_key": "jev:work-completion-authority",
            }
        try:
            verdict = sup.verify_completion(identity, proposal=args or {})
        except Exception as exc:
            return {
                "action": "block",
                "message": f"Nerve supervised completion could not be verified: {type(exc).__name__}: {exc}",
                "rule_key": "jev:work-completion-error",
            }
        if verdict.allow:
            try:
                _arm_terminal_ready(sup, identity, verdict)
            except Exception as exc:
                return {
                    "action": "block",
                    "message": f"Nerve verified completion but could not arm terminal authority: {type(exc).__name__}: {exc}",
                    "rule_key": "jev:work-terminal-ready-error",
                }
            return None
        return {
            "action": "block",
            "message": verdict.reason + (f" Missing: {', '.join(verdict.missing_criteria)}" if verdict.missing_criteria else ""),
            "rule_key": "jev:work-completion",
        }

    try:
        control = sup.control_for_run(identity)
    except Exception:
        control = None
    if control and str(control.get("control") or "") in {"WATCH", "REPLAN", "BLOCK"} and not _checkpoint_safe(name, args or {}):
        return {
            "action": "block",
            "message": (
                f"Nerve work control {control.get('control')} is active for this exact run. "
                "Checkpoint useful work now, then request canonical review/replan."
            ),
            "rule_key": "jev:work-control",
        }
    return None

def post_tool_call(*, tool_name: str, args: dict[str, Any], result: str, task_id: str = "", session_id: str = "", duration_ms: int = 0, **kwargs: Any) -> None:
    if not enabled() or str(tool_name).startswith(_INTERNAL_PREFIXES):
        return
    identity = _identity(task_id or None, session_id=session_id)
    if identity is None:
        return
    criterion_id = str((args or {}).get("_jev_criterion_id") or (args or {}).get("criterion_id") or "").strip() or None
    try:
        supervisor().observe_evidence(
            identity,
            value={"args": args or {}, "result": result, "duration_ms": duration_ms, "status": kwargs.get("status")},
            kind="tool_result",
            source="controller_observed",
            criterion_id=criterion_id,
            tool_name=str(tool_name),
            is_error=bool(kwargs.get("is_error") or str(kwargs.get("status") or "").lower() in {"error", "failed"}),
        )
    except Exception:
        pass
    test_observation = {"observed": False, "failed": False}
    try:
        test_observation = observe_test_result(
            supervisor(), identity, tool_name=str(tool_name), args=args or {}, result=result, kwargs=kwargs
        )
    except Exception:
        pass
    # Only a NEW failing test execution reaches the zero-cost ROI router. An
    # earlier repeated failure must not cause every unrelated tool call to
    # spend another supervisor decision.
    if test_observation.get("failed"):
        try:
            _maybe_assess(identity, trigger="test_failure")
        except Exception:
            pass


def _nerve_review_handoff(sup, identity, decision, *, forecast: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return irreversible budget authority to Hermes' reviewer/orchestrator lane.

    Nerve may pause/handoff spending, but it never marks the task failed for economic
    reasons. The reviewer/orchestrator owns the final COMPLETE / CHANGES / BLOCK call.
    """
    from .models import utc_now
    payload = {
        **decision.as_dict(),
        "source": "nerve_observer",
        "kill_switch": False,
        "orchestrator_review_required": True,
        "forecast": dict(forecast or {}),
    }
    existing = sup.control_for_run(identity)
    existing_payload = dict((existing or {}).get("payload") or {})
    if str((existing or {}).get("control") or "") == "REPLAN" and existing_payload.get("orchestrator_review_required"):
        return payload

    # Fence broad worker mutation locally before the canonical review transition.
    sup.store.set_control(
        identity,
        control="REPLAN",
        decision_id=f"nerve-orch-review-{identity.run_id}-{decision.api_calls}",
        payload=payload,
        created_at=utc_now(),
    )

    if tool_dispatcher_available():
        args: dict[str, Any] = {
            "task_id": identity.task_id,
            "summary": (
                "Nerve budget watchdog is returning this run to orchestrator review before any stop decision. "
                + decision.reason
            )[:1000],
            "metadata": {
                "source": "nerve_observer",
                "orchestrator_review_required": True,
                "nerve": decision.as_dict(),
                "forecast": dict(forecast or {}),
            },
        }
        configured_reviewer = str(reviewer() or "").strip()
        if configured_reviewer:
            args["reviewer"] = configured_reviewer
        try:
            native = dispatch_tool("kanban_request_review", args)
            ok, text = _native_dispatch_succeeded(native)
            sup.store.add_diagnostic(
                task_id=identity.task_id, run_id=identity.run_id, kind="nerve_orchestrator_handoff",
                payload={"ok": bool(ok), "result": str(text)[:1200], **payload}, created_at=utc_now(),
            )
        except Exception as exc:
            sup.store.add_diagnostic(
                task_id=identity.task_id, run_id=identity.run_id, kind="nerve_orchestrator_handoff_failed",
                payload={"error": f"{type(exc).__name__}: {exc}", **payload}, created_at=utc_now(),
            )
    return payload


def _nerve_observe_and_act(sup, identity, *, lifecycle_state: str = "") -> dict[str, Any] | None:
    cfg = settings()
    if not bool(cfg.get("nerve_observer_enabled", True)):
        return None
    try:
        decision = evaluate_nerve(sup, identity, lifecycle_state=lifecycle_state, cfg=cfg)
        payload = decision.as_dict()
        from .models import utc_now
        previous = sup.store.latest_diagnostic(
            task_id=identity.task_id, run_id=identity.run_id, kind="nerve_observer"
        )
        previous_level = str(((previous or {}).get("payload") or {}).get("level") or "")
        level_changed = previous_level != decision.level
        if level_changed or decision.requires_orchestrator_review:
            sup.store.add_diagnostic(
                task_id=identity.task_id, run_id=identity.run_id, kind="nerve_observer",
                payload=payload, created_at=utc_now(),
            )

        if decision.level == "FORECAST" and level_changed:
            try:
                forecast = forecast_extension(sup, identity, cfg=cfg)
            except Exception as exc:
                # An unavailable forecast is never a handoff signal: fail
                # open to WATCH and let the next checkpoint re-evaluate.
                # A Jev outage must not yank a healthy run to review.
                forecast = {
                    "value": "WATCH", "confidence": 0.0,
                    "reason": f"forecast unavailable: {type(exc).__name__}: {exc}",
                    "created_at": utc_now(),
                }
            min_conf = float(cfg.get("nerve_extension_min_confidence", 0.60))
            if str(forecast.get("value") or "").upper() == "YES" and float(forecast.get("confidence") or 0.0) < min_conf:
                forecast["raw_value"] = "YES"
                forecast["value"] = "MAYBE"
                forecast["reason"] = f"YES confidence below automatic-extension threshold {min_conf:.2f}"
            sup.store.add_diagnostic(
                task_id=identity.task_id, run_id=identity.run_id, kind="nerve_budget_forecast",
                payload=forecast, created_at=utc_now(),
            )
            value = str(forecast.get("value") or "MAYBE").upper()
            if value == "YES":
                base = max(1, int(forecast.get("base_token_target") or decision.base_token_target or decision.token_target))
                extra = max(1000, int(forecast.get("proposed_extension_tokens") or (base * float(cfg.get("nerve_extension_fraction", 0.25)))))
                ext = {
                    "extension_round": 1,
                    "base_token_target": base,
                    "extension_tokens": extra,
                    "effective_token_target": base + extra,
                    "extension_fraction": extra / base,
                    "forecast": forecast,
                    "granted_by": "nerve_watchdog",
                    "authority": "bounded_auto_extension",
                    "created_at": utc_now(),
                }
                sup.store.add_diagnostic(
                    task_id=identity.task_id, run_id=identity.run_id, kind="nerve_budget_extension",
                    payload=ext, created_at=utc_now(),
                )
                sup.store.stage_directive(
                    identity,
                    decision_id=f"nerve-extension-{identity.run_id}-{decision.api_calls}",
                    directive=(
                        f"Nerve forecast YES: one bounded +{extra} token extension was granted (effective target {base + extra}). "
                        "Finish the current plan without expanding scope. A second exhaustion goes to orchestrator review."
                    ),
                    confidence=float(forecast.get("confidence") or 0.0),
                    created_at=utc_now(),
                )
                return {**payload, "forecast": forecast, "extension": ext}
            if value == "MAYBE":
                review_decision = evaluate_nerve(sup, identity, lifecycle_state=lifecycle_state, cfg=cfg)
                # evaluate now observes the persisted MAYBE and yields ORCH_REVIEW.
                return _nerve_review_handoff(sup, identity, review_decision, forecast=forecast)
            # NO gets a short bounded checkpoint window. If already at/over the handoff
            # threshold, return authority immediately; otherwise stage a narrow directive.
            base = max(1, int(forecast.get("base_token_target") or decision.base_token_target or decision.token_target))
            consumed_fraction = int(forecast.get("consumed_tokens") or decision.consumed_tokens) / base
            if consumed_fraction >= float(cfg.get("nerve_no_handoff_fraction", 0.90)):
                review_decision = evaluate_nerve(sup, identity, lifecycle_state=lifecycle_state, cfg=cfg)
                return _nerve_review_handoff(sup, identity, review_decision, forecast=forecast)
            sup.store.stage_directive(
                identity,
                decision_id=f"nerve-no-extension-{identity.run_id}-{decision.api_calls}",
                directive=(
                    "Nerve forecast NO: do not broaden scope. Checkpoint useful work now; if completion is not reached "
                    "before the review threshold, authority returns to the main orchestrator."
                ),
                confidence=float(forecast.get("confidence") or 0.0),
                created_at=utc_now(),
            )
            return {**payload, "forecast": forecast}

        if decision.level in {"WATCH", "REPLAN"} and level_changed:
            sup.store.stage_directive(
                identity,
                decision_id=f"nerve-{identity.run_id}-{decision.api_calls}-{decision.level.lower()}",
                directive=(
                    f"Nerve observer {decision.level}: {decision.reason}. "
                    "Prefer a discriminating test/checkpoint over broad new exploration; do not expand scope."
                ),
                confidence=decision.confidence,
                created_at=utc_now(),
            )
            return payload

        if decision.requires_orchestrator_review or decision.should_kill:
            # Compatibility: even if an older caller sets should_kill, economic stop
            # authority is routed to canonical review rather than kanban_block.
            forecast = sup.store.latest_diagnostic(
                task_id=identity.task_id, run_id=identity.run_id, kind="nerve_budget_forecast"
            )
            return _nerve_review_handoff(
                sup, identity, decision, forecast=dict((forecast or {}).get("payload") or {})
            )

        return payload
    except Exception as exc:
        try:
            from .models import utc_now
            sup.store.add_diagnostic(
                task_id=identity.task_id, run_id=identity.run_id, kind="nerve_observer_failed",
                payload={"error": f"{type(exc).__name__}: {exc}"}, created_at=utc_now(),
            )
        except Exception:
            pass
        return None


def post_api_request(*, task_id: str = "", session_id: str = "", api_request_id: str = "", usage: dict[str, Any] | None = None, **kwargs: Any) -> None:
    """Per-provider-call accounting plus the dev15 post-verification invariant."""
    if not enabled():
        return
    identity = _identity(task_id or None, session_id=session_id)
    if identity is None:
        return
    sup = supervisor()
    try:
        outcome = sup.record_api_usage(identity, api_request_id=str(api_request_id or ""), usage=usage or {})
    except Exception:
        return
    state = ""
    try:
        control = sup.control_for_run(identity)
        state = _terminal_state(control)
        if state in {"VERIFIED", "COMPLETING", "COMPLETION_RETRY", "COMPLETED"}:
            from .models import utc_now
            sup.store.add_diagnostic(
                task_id=identity.task_id, run_id=identity.run_id,
                kind="completion_worker_call_after_verified",
                payload={"api_request_id": str(api_request_id or ""), "lifecycle_state": state},
                created_at=utc_now(),
            )
    except Exception:
        state = ""
    # If a provider call somehow lands after verification, first try to finish
    # through the controller-owned native path. Only if that cannot close the
    # run does the nerve observer escalate to the kill switch.
    if state in {"VERIFIED", "COMPLETING", "COMPLETION_RETRY"} and tool_dispatcher_available():
        try:
            prior = _prior_verdict(control)
            _complete_verified_run(sup, identity, {"trigger": "post_verified_provider_call"}, prior)
            control = sup.control_for_run(identity)
            state = _terminal_state(control)
        except Exception:
            pass
    _nerve_observe_and_act(sup, identity, lifecycle_state=state)
    for fraction in outcome.get("newly_crossed") or []:
        _maybe_assess(identity, trigger=f"budget_checkpoint:{float(fraction):.2f}")

def api_request_error(*, task_id: str = "", session_id: str = "", error: Any = None, reason: str | None = None, **kwargs: Any) -> None:
    if not enabled():
        return
    identity = _identity(task_id or None, session_id=session_id)
    if identity is None:
        return
    try:
        supervisor().observe_evidence(
            identity,
            value={"reason": reason, "error": error, "status_code": kwargs.get("status_code")},
            kind="provider_error",
            source="controller_observed",
            tool_name="provider",
            is_error=True,
        )
    except Exception:
        pass


def transform_tool_result(*, tool_name: str, result: str, task_id: str = "", session_id: str = "", **kwargs: Any):
    """Inject at most one tiny hidden directive into the next model cycle."""
    if not enabled():
        return None
    identity = _identity(task_id or None, session_id=session_id)
    if identity is None:
        return None
    try:
        directive = supervisor().consume_directive(identity)
    except Exception:
        directive = None
    if not directive:
        return None
    text = str(directive.get("directive") or "").strip()
    if not text:
        return None
    max_chars = int(settings().get("directive_max_chars", 320))
    text = text[:max_chars]
    return f"{result}\n\n[JEV SUPERVISORY DIRECTIVE]\n{text}"


def pre_verify(*, task_id: str = "", session_id: str = "", **kwargs: Any):
    """Verify the locked DoD and let the controller own terminal completion.

    Dev15's invariant is stronger than dev14: once PASS is established, this hook
    never asks the worker/model to discover or retry a completion mechanism. Native
    transition retries happen synchronously through ``PluginContext.dispatch_tool``
    and later lifecycle hooks can reconcile a deferred transition without spending
    another worker-model call.
    """
    if not enabled():
        return None
    identity = _identity(task_id or None, session_id=session_id)
    if identity is None:
        return None
    try:
        from . import paperclip_runtime
        is_paperclip = paperclip_runtime.owns(identity)
    except Exception:
        is_paperclip = False

    # Paperclip owns its issue lifecycle through its API, not kanban_complete.
    # Kanban retains the historical controller-dispatch authority fence.
    if not is_paperclip and not tool_dispatcher_available() and not _owns_kanban_terminal_authority():
        return None
    sup = supervisor()
    proposal = {
        "final_response": kwargs.get("final_response"),
        "response": kwargs.get("response"),
        "turn_id": kwargs.get("turn_id"),
        "exit_reason": kwargs.get("exit_reason"),
    }

    if is_paperclip:
        try:
            verdict = sup.verify_completion(identity, proposal=proposal)
        except Exception as exc:
            return {
                "action": "continue",
                "message": f"Nerve Paperclip completion verification failed: {type(exc).__name__}: {exc}",
            }
        if not verdict.allow:
            missing = f" Missing: {', '.join(verdict.missing_criteria)}" if verdict.missing_criteria else ""
            return {
                "action": "continue",
                "message": f"Nerve completion gate: {verdict.reason}{missing}",
            }
        outcome = paperclip_runtime.request_review(
            sup,
            identity,
            verdict,
            summary=str(proposal.get("final_response") or proposal.get("response") or verdict.reason),
        )
        if outcome.remote_updated:
            return None
        return {
            "action": "continue",
            "message": (
                "Nerve verified completion locally, but Paperclip review handoff is pending: "
                + outcome.reason
            ),
            "rule_key": "nerve:paperclip-review-pending",
        }

    try:
        control = sup.control_for_run(identity)
    except Exception:
        control = None
    if control and str(control.get("control") or "") == _TERMINAL_READY_CONTROL:
        state = _terminal_state(control)
        if state == "COMPLETED":
            return None
        prior = _prior_verdict(control)
        if tool_dispatcher_available():
            _complete_verified_run(sup, identity, proposal, prior)
            # PASS is terminal from the worker's perspective when controller
            # dispatch exists. Never return action=continue in real Hermes.
            return None
        return {
            "action": "continue",
            "message": (
                "Nerve completion is already verified, but this plugin host does not expose controller dispatch. "
                "Use only the directly-listed kanban_complete fallback."
            ),
            "rule_key": _TERMINAL_READY_RULE,
        }

    try:
        verdict = sup.verify_completion(identity, proposal=proposal)
    except Exception as exc:
        try:
            from .models import utc_now
            sup.store.add_diagnostic(
                task_id=identity.task_id, run_id=identity.run_id, kind="completion_pre_verify_failed",
                payload={"error": f"{type(exc).__name__}: {exc}"}, created_at=utc_now(),
            )
        except Exception:
            pass
        return {
            "action": "continue",
            "message": "Nerve completion verification failed; re-check the locked Definition of Done before finishing.",
        }
    try:
        from .models import utc_now
        sup.store.add_diagnostic(
            task_id=identity.task_id, run_id=identity.run_id, kind="completion_pre_verify",
            payload={
                "allow": bool(verdict.allow), "value": verdict.value,
                "confidence": verdict.confidence, "missing_criteria": list(verdict.missing_criteria),
                "receipt_id": verdict.receipt_id,
                "reason": verdict.reason,
                "authority": "deterministic" if str(verdict.receipt_id or "") == "deterministic" else "semantic",
            },
            created_at=utc_now(),
        )
    except Exception:
        pass
    if verdict.allow:
        try:
            _arm_terminal_ready(sup, identity, verdict)
        except Exception as exc:
            return {
                "action": "continue",
                "message": f"Nerve verified completion but could not persist terminal state: {type(exc).__name__}: {exc}.",
                "rule_key": "jev:work-terminal-ready-error",
            }
        if tool_dispatcher_available():
            _complete_verified_run(sup, identity, proposal, verdict)
            return None
        return {
            "action": "continue",
            "message": (
                "Nerve completion is verified. This plugin host lacks controller dispatch; "
                "use only the directly-listed kanban_complete fallback."
            ),
            "rule_key": _TERMINAL_READY_RULE,
        }
    missing = f" Missing: {', '.join(verdict.missing_criteria)}" if verdict.missing_criteria else ""
    return {
        "action": "continue",
        "message": f"Nerve completion gate: {verdict.reason}{missing}",
    }

def post_llm_call(*, task_id: str = "", session_id: str = "", **kwargs: Any) -> None:
    # Per-call usage is intentionally handled by post_api_request. This hook is
    # retained as a lifecycle observation seam, not as the budget source.
    if not enabled():
        return
    _identity(task_id or None, session_id=session_id)


def on_session_end(*, task_id: str = "", session_id: str = "", **kwargs: Any) -> None:
    """Controller-side reconciliation for sessions that end around completion."""
    if not enabled():
        return
    identity = _identity(task_id or None, session_id=session_id)
    if identity is None:
        return
    sup = supervisor()
    try:
        control = sup.control_for_run(identity)
    except Exception:
        control = None

    # Paperclip owns its issue lifecycle through its API; controller completion
    # is the verified request_review handoff, never the Kanban native dispatch.
    from .models import utc_now as _utc_now
    try:
        from . import paperclip_runtime
        if paperclip_runtime.owns(identity):
            if control and str(control.get("control") or "") == _TERMINAL_READY_CONTROL and _terminal_state(control) == "COMPLETED":
                return
            if not paperclip_runtime.current_authority():
                # Hook-delivery context: rebuild the authority from the worker
                # env + the durable startup binding. Without this the session
                # ended with a silently unverified run whenever a worker found
                # an unverified transition path (live: KEE-7 sign-off
                # interaction reached in_review with an unverified PASS).
                from .models import RunIdentity as _RunIdentity
                full_identity = sup.store.current_identity(identity.task_id) or identity
                rebuilt = paperclip_runtime.authority_from_store(sup, full_identity)
                if rebuilt is None:
                    sup.store.add_diagnostic(
                        task_id=identity.task_id, run_id=identity.run_id, kind="completion_session_end_skipped",
                        payload={"reason": "paperclip authority unavailable in hook context", "authority": "paperclip"},
                        created_at=_utc_now(),
                    )
                    return
                paperclip_runtime._ACTIVE.set((full_identity, rebuilt))
                identity = full_identity
            try:
                verdict = sup.verify_completion(identity, proposal={"session_end": True, "session_id": session_id})
                sup.store.add_diagnostic(
                    task_id=identity.task_id, run_id=identity.run_id, kind="completion_session_end",
                    payload={
                        "allow": bool(verdict.allow), "value": verdict.value,
                        "confidence": verdict.confidence, "missing_criteria": list(verdict.missing_criteria),
                        "receipt_id": verdict.receipt_id, "authority": "paperclip",
                    },
                    created_at=_utc_now(),
                )
                if verdict.allow:
                    outcome = paperclip_runtime.request_review(
                        sup,
                        identity,
                        verdict,
                        summary=str(verdict.reason),
                    )
                    sup.store.add_diagnostic(
                        task_id=identity.task_id, run_id=identity.run_id, kind="completion_session_end_handoff",
                        payload={"remote_updated": bool(outcome.remote_updated), "result": str(outcome.reason)[:600]},
                        created_at=_utc_now(),
                    )
            except Exception as exc:
                sup.store.add_diagnostic(
                    task_id=identity.task_id, run_id=identity.run_id, kind="completion_session_end_failed",
                    payload={"error": f"{type(exc).__name__}: {exc}", "authority": "paperclip"}, created_at=_utc_now(),
                )
            return

    except Exception:
        pass


    # A verified run is never handed back to the model. Session-end simply
    # reconciles a deferred native transition (or records that it already closed).
    if control and str(control.get("control") or "") == _TERMINAL_READY_CONTROL:
        from .models import utc_now
        state = _terminal_state(control)
        if not tool_dispatcher_available():
            try:
                sup.store.add_diagnostic(
                    task_id=identity.task_id, run_id=identity.run_id, kind="completion_session_end_skipped",
                    payload={"reason": "controller dispatcher unavailable; preserve verified latch", "lifecycle_state": state}, created_at=utc_now(),
                )
            except Exception:
                pass
            return
        if state == "COMPLETED":
            try:
                sup.store.add_diagnostic(
                    task_id=identity.task_id, run_id=identity.run_id, kind="completion_session_end_skipped",
                    payload={"reason": "controller completion already recorded", "lifecycle_state": state}, created_at=utc_now(),
                )
            except Exception:
                pass
            return
        prior = _prior_verdict(control)
        ok, native_result = _complete_verified_run(
            sup, identity, {"session_end": True, "session_id": session_id}, prior
        )
        try:
            sup.store.add_diagnostic(
                task_id=identity.task_id, run_id=identity.run_id, kind="completion_session_end_retry",
                payload={"ok": bool(ok), "result": str(native_result)[:1200], "prior_state": state}, created_at=utc_now(),
            )
        except Exception:
            pass
        return

    if not tool_dispatcher_available() and not _owns_kanban_terminal_authority():
        return
    try:
        verdict = sup.verify_completion(identity, proposal={"session_end": True, "session_id": session_id})
        from .models import utc_now
        sup.store.add_diagnostic(
            task_id=identity.task_id, run_id=identity.run_id, kind="completion_session_end",
            payload={
                "allow": bool(verdict.allow), "value": verdict.value,
                "confidence": verdict.confidence, "missing_criteria": list(verdict.missing_criteria),
                "receipt_id": verdict.receipt_id,
            },
            created_at=utc_now(),
        )
        if verdict.allow:
            _arm_terminal_ready(sup, identity, verdict)
            _complete_verified_run(sup, identity, {"session_end": True, "session_id": session_id}, verdict)
    except Exception as exc:
        try:
            from .models import utc_now
            sup.store.add_diagnostic(
                task_id=identity.task_id, run_id=identity.run_id, kind="completion_session_end_failed",
                payload={"error": f"{type(exc).__name__}: {exc}"}, created_at=utc_now(),
            )
        except Exception:
            pass
