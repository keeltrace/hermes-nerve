"""DRK PR1 tests — the five contract tests from spec section 1, plus guards.
Run: python3 -m pytest tests/test_drk_* -v
"""
from datetime import datetime, timedelta, timezone

from hermes_nerve.research.contracts import (
    Authority, Claim, ClaimKind, ClaimStatus, Evidence, Freshness, Method,
    can_upgrade_to_supported, evidence_satisfies_freshness,
)

NOW = datetime(2026, 9, 28, 12, 0, 0, tzinfo=timezone.utc)  # a Monday noon, Houston


def mk_claim(**kw):
    d = dict(text="x", kind=ClaimKind.TEMPORAL, freshness=Freshness.LIVE)
    d.update(kw)
    return Claim(**d)


def mk_ev(**kw):
    d = dict(source_name="s", extracted_text="txt", retrieved_at=NOW, method=Method.API)
    d.update(kw)
    return Evidence(**d)


# --- The five spec tests -------------------------------------------------

def test_unsupported_claim_cannot_become_supported():
    """No evidence -> no upgrade. Ever."""
    c = mk_claim(status=ClaimStatus.UNVERIFIED)
    assert can_upgrade_to_supported(c, {}, NOW) is False


def test_contradicted_evidence_marks_claim_contradicted():
    """Contradiction registration downgrades and locks the claim."""
    c = mk_claim(status=ClaimStatus.SUPPORTED, evidence_ids=["e1"])
    c.contradiction_ids.append("x1")
    assert can_upgrade_to_supported(c, {"e1": mk_ev()}, NOW) is False


def test_stale_evidence_cannot_satisfy_live_claim():
    """Authoritative source, but 6 hours old, claim requires LIVE -> rejected."""
    c = mk_claim(freshness=Freshness.LIVE, blocking=True)
    ev = mk_ev(retrieved_at=NOW - timedelta(hours=6), method=Method.PAGE_EXTRACT,
               authority=Authority.PRIMARY)
    assert evidence_satisfies_freshness(c, ev, NOW) is False


def test_model_memory_cannot_satisfy_live_claim():
    """The Truth BBQ lesson, as a unit test."""
    c = mk_claim(freshness=Freshness.LIVE, text="Truth BBQ is open at noon Monday")
    ev = mk_ev(method=Method.MODEL_MEMORY, authority=Authority.PRIMARY,
               source_name="lane memory", extracted_text="i remember it being open")
    assert evidence_satisfies_freshness(c, ev, NOW) is False
    assert can_upgrade_to_supported(c, {"e1": ev}, NOW) is False


def test_historical_evidence_may_satisfy_historical_ok():
    c = mk_claim(freshness=Freshness.HISTORICAL_OK, kind=ClaimKind.SUBJECTIVE)
    ev = mk_ev(retrieved_at=NOW - timedelta(days=400))
    assert evidence_satisfies_freshness(c, ev, NOW) is True


# --- Guards beyond the five ----------------------------------------------

def test_live_claim_accepts_fresh_primary_api():
    c = mk_claim(freshness=Freshness.LIVE, required_authority=(Authority.PRIMARY,),
                 evidence_ids=["e1"])
    ev = mk_ev(retrieved_at=NOW - timedelta(seconds=30), authority=Authority.PRIMARY)
    assert evidence_satisfies_freshness(c, ev, NOW) is True
    assert can_upgrade_to_supported(c, {"e1": ev}, NOW) is True


def test_today_claim_matches_effective_date_not_retrieval_date():
    """Weekly-hours page fetched today, effective for today -> TODAY satisfied."""
    c = mk_claim(freshness=Freshness.TODAY)
    ev = mk_ev(retrieved_at=NOW - timedelta(days=2),
               effective_at=datetime(2026, 9, 28, tzinfo=timezone.utc))
    assert evidence_satisfies_freshness(c, ev, NOW) is True
    # effective yesterday -> not today
    ev2 = mk_ev(retrieved_at=NOW - timedelta(days=2),
                effective_at=datetime(2026, 9, 27, tzinfo=timezone.utc))
    assert evidence_satisfies_freshness(c, ev2, NOW) is False


def test_authority_requirement_fails_closed():
    c = mk_claim(required_authority=(Authority.PRIMARY,))
    ev = mk_ev(authority=Authority.AGGREGATOR)
    assert can_upgrade_to_supported(c, {"e1": ev}, NOW) is False


def test_missing_evidence_id_fails_closed():
    c = mk_claim(evidence_ids=["ghost"])
    assert can_upgrade_to_supported(c, {}, NOW) is False
