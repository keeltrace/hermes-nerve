from __future__ import annotations

import uuid
from typing import Any

from ..work.kanban_adapter import CanonicalKanbanAdapter
from ..work.models import RunIdentity, WorkEvent
from ..work.supervisor import CardSupervisor


class RemoteEventBridge:
    """Translate remote Hermes stream-json into controller-owned supervision facts.

    The bridge never owns task lifecycle. It only accepts events for one exact
    canonical run identity, persists supervisory evidence/progress, and asks the
    canonical Kanban API for a review handoff after a complete checkpoint.
    """

    def __init__(
        self,
        supervisor: CardSupervisor,
        identity: RunIdentity,
        *,
        reviewer: str = "",
        kanban_adapter: CanonicalKanbanAdapter | None = None,
    ) -> None:
        self.supervisor = supervisor
        self.identity = identity
        self.reviewer = str(reviewer or "").strip()
        self.kanban = kanban_adapter or CanonicalKanbanAdapter()
        self.rejected = 0
        self.ingested = 0
        self.review_handoff: dict[str, Any] | None = None
        self.stop_requested = False

    def _matches(self, payload: dict[str, Any]) -> bool:
        checks = {
            "task_id": self.identity.task_id,
            "run_id": self.identity.run_id,
            "contract_hash": self.identity.contract_hash,
        }
        for key, expected in checks.items():
            supplied = payload.get(key)
            if supplied not in (None, "") and str(supplied) != str(expected):
                return False
        claim = payload.get("claim_identity")
        if claim not in (None, "") and str(claim) != self.identity.claim_identity:
            return False
        return True

    def _checkpoint_ready(self, raw: dict[str, Any]) -> None:
        payload = dict(raw.get("payload") or {})
        control = self.supervisor.control_for_run(self.identity) or {}
        packet = self.supervisor.checkpoint_packet(
            self.identity,
            decision_id=str(control.get("decision_id") or payload.get("decision_id") or "remote-checkpoint"),
            reason=str(payload.get("reason") or f"Remote checkpoint for {control.get('control') or 'handoff'}"),
            current_plan=str(payload.get("current_plan") or ""),
            test_summary=str(payload.get("test_summary") or ""),
            artifacts=list(payload.get("artifacts") or []),
            workspace_result=dict(payload.get("workspace_result") or {}),
            complete=True,
        )
        if str(control.get("control") or "") not in {"WATCH", "REPLAN", "BLOCK"}:
            return
        try:
            ok, reason = self.kanban.request_review(
                task_id=self.identity.task_id,
                expected_run_id=self.identity.run_id,
                summary=str(packet.get("reason") or "Hermes-Jev remote checkpoint handoff"),
                metadata={"jev_supervision": packet},
                reviewer=self.reviewer,
            )
            self.review_handoff = {"transitioned": bool(ok), "reason": reason, "checkpoint": packet}
            if ok:
                self.stop_requested = True
        except Exception as exc:
            self.review_handoff = {
                "transitioned": False,
                "reason": f"{type(exc).__name__}: {exc}",
                "checkpoint": packet,
            }

    def consume(self, event: dict[str, Any]) -> None:
        event_type = str(event.get("type") or "")
        if event_type == "tool_use" and str(event.get("name") or "") == "nerve_work_event":
            raw = (
                event.get("input") if isinstance(event.get("input"), dict)
                else event.get("args") if isinstance(event.get("args"), dict)
                else {}
            )
            if not self._matches(raw):
                self.rejected += 1
                return
            work_event = WorkEvent(
                event_id=str(raw.get("event_id") or "nerveremote-" + uuid.uuid4().hex),
                event_type=str(raw.get("event_type") or raw.get("type") or "").upper(),
                criterion_id=str(raw.get("criterion_id") or "").strip() or None,
                payload=dict(raw.get("payload") or {}),
                source="remote_worker",
            )
            try:
                self.supervisor.record_event(self.identity, work_event)
                self.ingested += 1
                if work_event.event_type == "CRITERION_CLAIMED_PASS" and work_event.criterion_id:
                    try:
                        self.supervisor.verify_criterion(self.identity, work_event.criterion_id)
                    except Exception:
                        pass
                elif work_event.event_type == "CHECKPOINT_READY":
                    self._checkpoint_ready(raw)
            except Exception:
                self.rejected += 1
            return

        if event_type == "tool_result":
            name = str(event.get("name") or "unknown")
            if name.startswith(("nerve_", "jev_")):
                return
            value = event.get("output") if "output" in event else event
            try:
                self.supervisor.observe_evidence(
                    self.identity,
                    value=value,
                    kind="remote_tool_result",
                    source="controller_observed_remote",
                    tool_name=name,
                    is_error=bool(event.get("is_error")),
                )
                self.ingested += 1
            except Exception:
                self.rejected += 1
            return

        if event_type == "result":
            usage = event.get("tokens") or event.get("usage")
            total = 0
            if isinstance(usage, dict):
                try:
                    total = int(usage.get("total_tokens") or 0)
                    if not total:
                        total = int(usage.get("input_tokens") or 0) + int(usage.get("output_tokens") or 0)
                except Exception:
                    total = 0
            elif isinstance(usage, (int, float)):
                total = int(usage)
            if total > 0:
                try:
                    outcome = self.supervisor.record_usage(
                        self.identity,
                        consumed_tokens=total,
                        source="remote_provider_usage",
                    )
                    for fraction in outcome.get("newly_crossed") or []:
                        try:
                            self.supervisor.assess_trajectory(
                                self.identity,
                                trigger=f"budget_checkpoint:{fraction}",
                            )
                        except Exception:
                            pass
                except Exception:
                    pass
