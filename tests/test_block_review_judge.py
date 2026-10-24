"""Tests for the block-review judge + completion hardening (2026-09-28)."""
import json
import pytest

from hermes_nerve.work.block_review import (
    LAZY_MAX_BUDGET_FRACTION,
    LAZY_MAX_VARIANTS,
    _semantic_laziness_flags,
    bounce_message,
    review_block,
)


class _FakeStore:
    """Minimal store surface: evidence + latest_diagnostic."""

    def __init__(self, evidence=None, diagnostics=None):
        self._evidence = evidence or []
        self._diagnostics = diagnostics or []

    def evidence(self, identity, include_stale=True):
        return self._evidence

    def latest_diagnostic(self, task_id, run_id, kind):
        for d in reversed(self._diagnostics):
            if d.get("kind") == kind:
                return d
        return None


class _FakeSup:
    def __init__(self, store):
        self.store = store


class _Identity:
    task_id = "t_test"
    run_id = 1


def _ev(tool, preview, is_error=False):
    return {"tool_name": tool, "preview": preview, "is_error": is_error}


IDENT = _Identity()


def _review(store, reason="need human help"):
    return review_block(_FakeSup(store), IDENT, reason=reason)


# ── verdict matrix ──────────────────────────────────────────────────────

def test_refute_on_false_claim():
    # worker cites a commit that never appeared in observed output -> claim
    # unverifiable -> REFUTE (citing evidence that isn't there)
    ev = [_ev("write_file", "x = 1")]
    store = _FakeStore(evidence=ev)
    r = _review(store, reason="blocked, but commit abc1234 proves the ruling was applied")
    assert r.verdict == "REFUTE"
    assert r.bounce is True
    assert r.card["refuted_claims"], "refuted claim must be named on the card"


def test_continue_on_lazy_signals():
    # zero variants, low budget, no refuted claims -> CONTINUE (first offense)
    store = _FakeStore(evidence=[])
    r = _review(store)
    assert r.verdict == "CONTINUE"
    assert r.bounce is True


def test_escalate_on_honest_block():
    # many variants + budget spent + nothing refuted -> ESCALATE
    ev = [_ev("write_file", f"variant {i}") for i in range(6)]
    store = _FakeStore(evidence=ev)
    r = _review(store, reason="run control blocks terminal calls; fix already committed")
    assert r.verdict == "ESCALATE"
    assert r.bounce is False


def test_two_strikes_escalates_instead_of_looping():
    # lazy signals persist after MAX bounces -> ESCALATE (no infinite loop)
    store = _FakeStore(
        evidence=[],
        diagnostics=[{"kind": "nerve_block_review", "payload": {
            "review_count": 2  # >= MAX_BLOCK_REVIEWS_PER_RUN
        }}],
    )
    r = _review(store)
    assert r.verdict == "ESCALATE"


# ── semantic laziness flags ────────────────────────────────────────────

def test_semantic_flag_on_interpretation_surrender():
    store = _FakeStore(evidence=[_ev("write_file", "only edit")])
    flags = _semantic_laziness_flags(store, IDENT, "which seed is authoritative? need a decision before proceeding")
    assert "asks-human-for-interpretation-decision" in flags
    # corroborated: only one distinct edit target before asking
    assert any("edited-only-1" in f for f in flags)


def test_no_semantic_flag_on_plain_block():
    store = _FakeStore(evidence=[])
    flags = _semantic_laziness_flags(store, IDENT, "terminal calls rejected; cannot run pytest")
    assert flags == []


# ── bounce messages carry the work order ───────────────────────────────

def test_bounce_message_mentions_verdict():
    store = _FakeStore(evidence=[])
    r = _review(store)
    msg = bounce_message(r)
    text = json.dumps(msg)
    assert r.verdict in text or r.reasons[0] in text


# ── card shape (the human judge's contract) ───────────────────────────

def test_card_contains_judge_fields():
    ev = [_ev("write_file", f"v{i}") for i in range(3)]
    store = _FakeStore(evidence=ev)
    r = _review(store)
    for field in ("variants_tried", "budget_fraction_consumed", "refuted_claims",
                  "semantic_laziness_flags", "block_reason"):
        assert field in r.card
