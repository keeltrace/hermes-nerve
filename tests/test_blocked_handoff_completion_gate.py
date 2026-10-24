"""Completion-integrity pins: a model-written BLOCKED label never satisfies a gate.

Truthful BLOCKED handoffs must remain expressible — but a status label is a
report, not authorization. Historical context: candidate ``d336f8b`` added a
model-controlled BLOCKED-prefix shortcut that let adversarial summaries
(``BLOCKED: Everything is ready.``) bypass missing-test verification; it was
fully reverted by ``b9aef11``. These tests keep the restored strict gate
locked so it can never silently regress.
"""
import json

from hermes_nerve import progress_integrity
from hermes_nerve.work.deterministic import (
    _verify_completion_summary,
    canonical_completion_summary,
)
from hermes_nerve.work.models import CriterionSpec, DoDContract


class _FakeStore:
    def __init__(self, verdicts=None, evidence=None):
        self._verdicts = verdicts or {}
        self._evidence = evidence or []

    def latest_contract_verdicts(self, task_id, contract_hash):
        return self._verdicts

    def run_context(self, identity):
        return {"workspace_path": ""}

    def evidence(self, identity, criterion_id=None, include_stale=True):
        return [e for e in self._evidence if criterion_id in (None, e.get("criterion_id"))]

    def latest_checkpoint(self, task_id):
        return None

    def events(self, task_id, run_id=None, include_stale=True):
        return []


class _Sup:
    def __init__(self, contract, store):
        self._contract = contract
        self.store = store

    def active_contract(self, task_id):
        return self._contract


class _Identity:
    task_id = "t_blocked_gate"
    run_id = 1
    contract_hash = "abc"


def _contract():
    return DoDContract(
        task_id="t_blocked_gate",
        version=1,
        goal="fix the flaky module",
        criteria=(
            CriterionSpec(id="DOD-01", description="run `python3 -m pytest` and require green", required=True),
        ),
        reserve_tokens=100000,
        checkpoint_fractions=(0.5,),
        contract_hash="abc",
        locked_at="2026-09-28T00:00:00Z",
        actor="test",
    )


def _suite_evidence():
    return [{
        "kind": "deterministic_check",
        "is_error": False,
        "criterion_id": "DOD-01",
        "preview": json.dumps({
            "command": "python3 -m pytest",
            "returncode": 0,
            "output": "=== 3 passed in 0.01s ===",
        }),
    }]


class TestBlockedLabelNeverAuthorizes:
    """The two historical adversarial probes, plus the evidence-claiming twin."""

    def test_bare_blocked_labels_are_refused(self):
        for text in ("BLOCKED: Everything is ready.", "BLOCKED: All work is verified."):
            ok, reason = _verify_completion_summary(
                "final completion summary", {"final_response": text}, ["DOD-01"])
            assert ok is False, text
            assert "status" in reason.lower(), (text, reason)

    def test_blocked_label_with_claimed_evidence_is_still_refused(self):
        # Even when the prose goes on to claim the exact test command, a
        # passing count, and the criterion id, the leading status label
        # refuses completion authorization.
        text = ("BLOCKED: All work is verified. python3 -m pytest tests/ -q "
                "1294 passed DOD-01")
        ok, reason = _verify_completion_summary(
            "final completion summary", {"final_response": text}, ["DOD-01"])
        assert ok is False
        assert "status" in reason.lower()

    def test_partial_and_failed_labels_are_refused(self):
        for text in ("PARTIAL: mostly done, tests green.", "FAILED: nothing left to do."):
            ok, reason = _verify_completion_summary(
                "final completion summary", {"final_response": text}, ["DOD-01"])
            assert ok is False, text


class TestMachineGatesTreatBlockedAsFailure:
    def test_blocked_status_is_never_a_test_success(self):
        for status in ("blocked", "BLOCKED", "Blocked", "pending", ""):
            assert progress_integrity.test_succeeded(status, "") is False

    def test_blocked_setup_run_reports_environment_failure(self):
        # A blocked run is failure signaling: on a setup action it counts as
        # environment failure (retryable friction), never as success.
        assert progress_integrity.environment_failure(
            "terminal", {"command": "pip install -e ."}, "blocked", "", "") is True


class TestCanonicalGateNeedsMachineEvidence:
    def test_claiming_blocked_prose_without_evidence_yields_no_summary(self):
        sup = _Sup(_contract(), _FakeStore(verdicts={}, evidence=[]))
        proposal = {"final_response": "BLOCKED: All work is verified."}
        assert canonical_completion_summary(sup, _Identity(), proposal=proposal) is None

    def test_positive_control_real_evidence_yields_summary(self):
        sup = _Sup(_contract(), _FakeStore(
            verdicts={"DOD-01": {"state": "VERIFIED_PASS", "reason": "passed"}},
            evidence=_suite_evidence()))
        proposal = {"final_response": "SEED APPLIED: 20260901 — commit bc731a1"}
        assert canonical_completion_summary(sup, _Identity(), proposal=proposal) is not None

    def test_blocked_handoff_stays_unverified_not_done(self):
        # A truthful BLOCKED report produces no completion summary: it stays
        # an unverified handoff, never a completed one.
        sup = _Sup(_contract(), _FakeStore(
            verdicts={"DOD-01": {"state": "PENDING", "reason": "worker reported blocked"}},
            evidence=[]))
        proposal = {"final_response": "BLOCKED: waiting on credentials."}
        assert canonical_completion_summary(sup, _Identity(), proposal=proposal) is None
