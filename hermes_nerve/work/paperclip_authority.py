"""Paperclip issue lifecycle authority for verified Nerve work."""
from __future__ import annotations

from dataclasses import dataclass

from ..integrations.paperclip import PaperclipRunContext
from ..integrations.paperclip_client import PaperclipClient, PaperclipTransportError
from .authority import WorkIdentity
from .contract import PaperclipExecutionContract
from .models import CompletionVerdict


@dataclass(frozen=True)
class HandoffResult:
    local_pass: bool
    remote_updated: bool
    reason: str = ""


class PaperclipIssueAuthority:
    def __init__(
        self,
        client: PaperclipClient,
        execution: PaperclipExecutionContract,
    ) -> None:
        self.client = client
        self.execution = execution
        if client.run_id != execution.context.run_id:
            raise ValueError("Paperclip client run id does not match execution context")

    @property
    def context(self) -> PaperclipRunContext:
        return self.execution.context

    def identity(self) -> WorkIdentity:
        return WorkIdentity("paperclip", self.execution.task_id, self.context.run_id)

    def _ownership_error(self, issue) -> str:
        if issue.id != self.execution.task_id:
            return "Paperclip returned the wrong issue"
        if issue.checkout_run_id and issue.checkout_run_id != self.context.run_id:
            return "Paperclip checkout belongs to a different run"
        if issue.assignee_agent_id and issue.assignee_agent_id != self.context.agent_id:
            return "Paperclip issue belongs to a different agent"
        return ""

    def request_review(
        self,
        result: CompletionVerdict,
        *,
        summary: str = "",
        evidence: tuple[str, ...] = (),
    ) -> HandoffResult:
        if not result.allow:
            return HandoffResult(False, False, "verification failed")

        current = self.client.get_issue(self.execution.task_id)
        if current.id != self.execution.task_id:
            return HandoffResult(True, False, "Paperclip returned the wrong issue")
        # Once review has begun, the builder may no longer own the checkout. That
        # is success, not a stale-run failure; no further mutation is required.
        if current.status in {"in_review", "done"}:
            return HandoffResult(True, True, "already handed off")
        ownership_error = self._ownership_error(current)
        if ownership_error:
            return HandoffResult(True, False, ownership_error)
        if current.status != "in_progress":
            return HandoffResult(
                True, False, f"unexpected Paperclip status {current.status!r}"
            )

        marker = f"<!-- nerve-verification:{self.context.run_id} -->"
        lines = [
            marker,
            "### Nerve Verification",
            "",
            "Result: PASS",
            "",
            "Criteria:",
        ]
        lines.extend(
            f"- PASS — {criterion.description}"
            for criterion in self.execution.criteria
        )
        if evidence:
            lines.extend(["", "Evidence:"])
            lines.extend(f"- {item}" for item in evidence)
        if summary or result.reason:
            lines.extend(["", "Summary:", str(summary or result.reason)])
        lines.extend(["", f"Run: {self.context.run_id}"])
        comment = "\n".join(lines)

        # Paperclip requires agent-authored in_review transitions to carry a
        # real review path. The Nerve handoff IS a human sign-off request, so
        # create the confirmation interaction first (idempotent per run) and
        # then move the issue into review.
        try:
            self.client.create_confirmation_interaction(
                self.execution.task_id,
                prompt=(
                    f"Nerve verified this issue ({result.value}, confidence "
                    f"{float(result.confidence or 0.0):.2f}). Confirm the handoff to review."
                ),
                details=comment,
                run_id=str(self.context.run_id),
            )
        except PaperclipTransportError:
            # The PATCH below re-checks state; a failed interaction create is
            # retried by the idempotency key on the next attempt.
            pass
        try:
            updated = self.client.request_review(
                self.execution.task_id,
                comment=comment,
            )
        except PaperclipTransportError as exc:
            # A PATCH may have reached Paperclip before a client-side timeout.
            # Reconcile via GET instead of blindly repeating a comment-bearing PATCH.
            try:
                reconciled = self.client.get_issue(self.execution.task_id)
            except Exception:
                return HandoffResult(True, False, str(exc))
            if reconciled.id != self.execution.task_id:
                return HandoffResult(True, False, "Paperclip returned the wrong issue")
            if reconciled.status in {"in_review", "done"}:
                return HandoffResult(True, True, "reconciled after transport failure")
            ownership_error = self._ownership_error(reconciled)
            if ownership_error:
                return HandoffResult(True, False, ownership_error)
            return HandoffResult(True, False, str(exc))

        ownership_error = self._ownership_error(updated)
        if ownership_error:
            return HandoffResult(True, False, ownership_error)
        if updated.status not in {"in_review", "done"}:
            return HandoffResult(
                True, False, f"Paperclip did not enter review: {updated.status!r}"
            )
        return HandoffResult(True, True)

    def report_blocked(self, reason: str):
        return False, str(reason or "")
