"""Runtime bridge between Paperclip issue runs and Nerve CardSupervisor."""
from __future__ import annotations

from contextvars import ContextVar
import os
from pathlib import Path
import subprocess
from typing import Any

from ..integrations.paperclip import PaperclipRunContext, detect_paperclip_context
from ..integrations.paperclip_client import PaperclipClient
from .contract import bind_execution_contract, issue_to_execution_contract
from .models import CompletionVerdict, RunIdentity
from .paperclip_authority import HandoffResult, PaperclipIssueAuthority


_ACTIVE: ContextVar[tuple[RunIdentity, PaperclipIssueAuthority] | None] = ContextVar(
    "nerve_paperclip_active", default=None
)


def _git_head(workspace: str) -> str:
    root = str(workspace or "").strip()
    if not root or not Path(root).is_dir():
        return ""
    try:
        proc = subprocess.run(
            ["git", "-C", root, "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except Exception:
        return ""
    return proc.stdout.strip() if proc.returncode == 0 else ""


def client_from_env(context: PaperclipRunContext) -> PaperclipClient:
    return PaperclipClient(
        base_url=context.api_url or os.getenv("PAPERCLIP_API_URL", ""),
        token=os.getenv("PAPERCLIP_API_KEY", ""),
        run_id=context.run_id,
    )


def bootstrap_paperclip_worker(
    supervisor,
    *,
    context: PaperclipRunContext | None = None,
    client: PaperclipClient | None = None,
    workspace_path: str = "",
    session_id: str = "",
    token_budget: int = 0,
) -> RunIdentity | None:
    """Bind the triggering Paperclip issue into the existing Nerve evidence engine."""
    context = context or detect_paperclip_context()
    if context is None:
        return None
    client = client or client_from_env(context)
    issue = client.get_issue(context.task_id)
    budget = int(token_budget or 0)
    if budget <= 0:
        # Match the Kanban autobinder: an unestimated budget must not become a
        # 1-token target, or the budget observer kills healthy runs instantly.
        try:
            from .nerve import estimate_task_budget
            from .runtime import settings as _runtime_settings
            cfg = _runtime_settings()
            budget = estimate_task_budget(
                str(issue.description or ""),
                0,
                floor_tokens=int(cfg.get("default_task_budget_tokens", 70000)),
                base_tokens=int(cfg.get("budget_estimator_base_tokens", 120000)),
                per_criterion_tokens=int(cfg.get("budget_estimator_per_criterion_tokens", 75000)),
                body_char_factor=float(cfg.get("budget_estimator_body_char_factor", 25.0)),
                safety_multiplier=float(cfg.get("budget_estimator_safety_multiplier", 1.25)),
                max_tokens=int(cfg.get("budget_estimator_max_tokens", 2000000)),
            )
        except Exception:
            budget = 70000
    execution = issue_to_execution_contract(
        issue, context, token_budget=budget
    )
    workspace = str(
        workspace_path
        or os.getenv("PAPERCLIP_WORKSPACE")
        or os.getenv("TERMINAL_CWD")
        or os.getenv("PWD")
        or ""
    ).strip()
    identity = bind_execution_contract(
        supervisor,
        execution,
        workspace_path=workspace,
        base_revision=_git_head(workspace),
        session_id=session_id,
    )
    authority = PaperclipIssueAuthority(client, execution)
    _ACTIVE.set((identity, authority))
    return identity


def current_identity() -> RunIdentity | None:
    active = _ACTIVE.get()
    return active[0] if active else None


def current_authority() -> PaperclipIssueAuthority | None:
    active = _ACTIVE.get()
    return active[1] if active else None


def owns(identity: RunIdentity | None) -> bool:
    if identity is None:
        return False
    current = current_identity()
    if current is None:
        # Hook-delivery contexts lose the bootstrap ContextVar; fall back to
        # the durable startup binding for the worker's own task env. Compare
        # identity keys only: a reconstructed identity has no claim_identity,
        # and dataclass equality would silently fail the match.
        env_task = str(os.getenv("PAPERCLIP_TASK_ID") or "").strip()
        if env_task and env_task == identity.task_id:
            try:
                from .runtime import supervisor as _supervisor
                current = _supervisor().store.current_identity(env_task)
            except Exception:
                current = None
    if current is None or identity is None:
        return False
    return (
        current.task_id == identity.task_id
        and int(current.run_id) == int(identity.run_id)
        and current.contract_hash == identity.contract_hash
    )


def authority_from_store(supervisor, identity: RunIdentity) -> PaperclipIssueAuthority | None:
    """Rebuild the Paperclip authority from env + the durable startup binding.

    Hook callbacks run in Hermes-delivered contexts where the startup
    bootstrap ContextVar is invisible. The worker's own PAPERCLIP_* env plus
    the run_bindings row are sufficient to reconstruct it safely.
    """
    context = PaperclipRunContext(
        company_id=str(os.getenv("PAPERCLIP_COMPANY_ID") or ""),
        task_id=identity.task_id,
        run_id=str(os.getenv("PAPERCLIP_RUN_ID") or identity.run_id),
        agent_id=str(os.getenv("PAPERCLIP_AGENT_ID") or identity.worker_id or ""),
        api_url=os.getenv("PAPERCLIP_API_URL") or None,
        role=str(os.getenv("PAPERCLIP_AGENT_ROLE") or "builder"),
    )
    client = client_from_env(context)
    try:
        issue = client.get_issue(identity.task_id)
        execution = issue_to_execution_contract(issue, context)
    except Exception:
        return None
    return PaperclipIssueAuthority(client, execution)


def revert_unverified_review(supervisor, identity: RunIdentity, verdict: CompletionVerdict) -> bool:
    """Move a worker-reached ``in_review`` issue back to ``in_progress``.

    Session-end ran the deterministic verification and it did not allow the
    handoff, so any review state on the issue is unverified - the worker may
    have smuggled it through a Paperclip-native sign-off interaction, which
    no tool-level fence can see. Paperclip stays canonical for status; this
    is an API mutation by the controller, never state SQL.
    """
    authority = current_authority()
    if authority is None:
        authority = authority_from_store(supervisor, identity)
    if authority is None:
        return False
    client = authority.client
    issue = client.get_issue(identity.task_id)
    if issue.status != "in_review":
        return False
    body = (
        f"<!-- nerve-controller:{identity.run_id} -->\n"
        "### Nerve Verification\n\n"
        "Result: NOT VERIFIED\n\n"
        f"Nerve session-end verification returned {verdict.value}; the issue was moved "
        "back to in_progress because review requires a Nerve PASS. Missing criteria: "
        + (", ".join(verdict.missing_criteria[:8]) or "(none listed)")
        + ".\n"
    )
    client.revert_to_in_progress(identity.task_id, comment=body)
    return True


def request_review(
    supervisor,
    identity: RunIdentity,
    verdict: CompletionVerdict,
    *,
    summary: str = "",
) -> HandoffResult:
    authority = current_authority()
    if authority is None:
        # Hook-delivery contexts lose the bootstrap ContextVar; rebuild the
        # authority from the worker env + the durable startup binding.
        env_task = str(os.getenv("PAPERCLIP_TASK_ID") or "").strip()
        if env_task and env_task == identity.task_id:
            authority = authority_from_store(supervisor, identity)
    if authority is None:
        return HandoffResult(bool(verdict.allow), False, "Paperclip authority unavailable")
    evidence = tuple(
        str(row.get("pointer") or row.get("tool_name") or row.get("kind") or "").strip()
        for row in supervisor.store.evidence(identity)
        if str(row.get("pointer") or row.get("tool_name") or row.get("kind") or "").strip()
    )
    outcome = authority.request_review(
        verdict,
        summary=summary,
        evidence=evidence[-12:],
    )
    if outcome.remote_updated:
        # Handoff-contingent criteria ("verification transitioned the issue",
        # "issue contains a Nerve Verification comment") are true only after
        # the controller-performed transition, so the controller marks them
        # itself. A pre-handoff judge can never honestly evaluate them.
        try:
            for criterion in authority.execution.criteria:
                desc_low = str(criterion.description or "").lower()
                if (
                    "transitioned this issue to" in desc_low
                    or "transitioned the issue to" in desc_low
                    or ("contains a" in desc_low and "nerve verification" in desc_low)
                ):
                    supervisor.mark_deterministic_verdict(
                        identity,
                        criterion.id,
                        passed=True,
                        reason="Verified controller handoff performed: issue in review with the Nerve Verification comment attached.",
                        evidence_ids=[],
                    )
        except Exception:
            pass
    return outcome


def clear_for_tests() -> None:
    _ACTIVE.set(None)
