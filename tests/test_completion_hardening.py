"""Completion-path hardening: provenance summaries (R1b lesson).

The goal-mode completion judge rejects narration-only summaries. The
canonical summary must carry: pasted command output, the applied value,
and commit provenance — all sourced from durable state + the worker's
own proposal, never regenerated.
"""
import json
import pytest

from hermes_nerve.work.models import CriterionSpec, DoDContract
from hermes_nerve.work.deterministic import canonical_completion_summary


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
    task_id = "t_test"
    run_id = 1
    contract_hash = "abc"


def _contract():
    return DoDContract(
        task_id="t_test",
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


def _verdicts():
    return {"DOD-01": {"state": "VERIFIED_PASS", "reason": "passed"}}


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


def test_summary_includes_pasted_result_and_provenance():
    sup = _Sup(_contract(), _FakeStore(verdicts=_verdicts(), evidence=_suite_evidence()))
    proposal = {"final_response": "SEED APPLIED: 20260901 — commit bc731a1"}
    s = canonical_completion_summary(sup, _Identity(), proposal=proposal)
    assert s is not None
    assert "3 passed" in s                      # pasted command result
    assert "Mandated seed: 20260901" in s       # applied value, from proposal
    assert "Fix commit" in s                    # commit provenance line present


def test_summary_none_when_criteria_unverified():
    sup = _Sup(_contract(), _FakeStore(verdicts={"DOD-01": {"state": "PENDING", "reason": ""}}))
    assert canonical_completion_summary(sup, _Identity()) is None
