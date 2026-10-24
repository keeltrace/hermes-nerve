"""DRK §17 adversarial tests + BBQ regression + PR7/PR9/PR10 units. Hermetic.
Run: python3 -m pytest tests/test_drk_* -v
"""
from datetime import datetime, timedelta, timezone

from hermes_nerve.research.budget import BudgetGovernor, ResearchBudget
from hermes_nerve.research.contradictions import compare_evidence
from hermes_nerve.research.contracts import (Authority, Claim, ClaimKind, ClaimStatus, Evidence,
                       Freshness, Method)
from hermes_nerve.research.referee import referee
from hermes_nerve.research.lint import citation_lint

NOW = datetime(2026, 9, 28, 12, 0, 0, tzinfo=timezone.utc)  # Monday noon


def mk_claim(**kw):
    d = dict(text="x", kind=ClaimKind.TEMPORAL, freshness=Freshness.LIVE)
    d.update(kw)
    return Claim(**d)


def mk_ev(**kw):
    d = dict(source_name="s", extracted_text="t", retrieved_at=NOW, method=Method.API)
    d.update(kw)
    return Evidence(**d)


# --- §17 stable falsehood: three lanes repeat the same stale fact ----------
def test_stable_falsehood_blocked():
    c = mk_claim(text="Truth BBQ is open at noon Monday", blocking=True,
                 evidence_ids=["e1", "e2", "e3"])
    evs = {f"e{i}": mk_ev(method=Method.MODEL_MEMORY, retrieved_at=NOW - timedelta(days=400))
           for i in (1, 2, 3)}
    # registry verify path: no retrieval evidence -> cannot be supported
    assert all(not ev.method is Method.API for ev in evs.values())
    c.status = ClaimStatus.UNVERIFIED
    d = referee("Truth BBQ", [c])
    assert d.passed is False


# --- §17 freshness mismatch: authoritative but 6 months old, claim LIVE ----
def test_freshness_mismatch_rejected():
    c = mk_claim(freshness=Freshness.LIVE, blocking=True, evidence_ids=["e1"])
    ev = mk_ev(retrieved_at=NOW - timedelta(days=180), authority=Authority.PRIMARY)
    assert ev.authority is Authority.PRIMARY  # authoritative...
    from hermes_nerve.research.contracts import can_upgrade_to_supported
    assert can_upgrade_to_supported(c, {"e1": ev}, NOW) is False  # ...and still rejected


# --- §17 source disagreement: two authoritative sources conflict -----------
def test_source_disagreement_unresolved():
    c = mk_claim(status=ClaimStatus.SUPPORTED)
    a = mk_ev(normalized_value="open 11-9", authority=Authority.PRIMARY)
    b = mk_ev(normalized_value="closed", authority=Authority.PRIMARY)
    assert compare_evidence(c, [a, b]) is ClaimStatus.UNRESOLVED


# --- §17 provider failure: fetch failed produces unresolved, never success -
def test_provider_failure_fail_closed():
    c = mk_claim(blocking=True, evidence_ids=["e1"])
    ev = mk_ev(method=Method.PAGE_EXTRACT, authority=Authority.UNKNOWN,
               extracted_text="", source_name="fetch failed")
    from hermes_nerve.research.contracts import can_upgrade_to_supported
    # unknown authority fails the requirement -> not supportable
    assert can_upgrade_to_supported(c, {"e1": ev}, NOW) is False


# --- §17 synthesizer rebellion: recommends a contradicted candidate --------
def test_synthesizer_rebellion_rejected():
    ok, v = citation_lint("Pick: Truth BBQ for brisket.", survivors=["The Pit Room"],
                          contradicted=["Truth BBQ"])
    assert ok is False and any("REBELLION" in x for x in v)


# --- BBQ regression: the original failure is now impossible ----------------
def test_bbq_regression_truth_rejected_on_monday():
    """The old champion confidently picked Truth BBQ, closed Mondays.
    The referee must reject that candidate given a grounded contradicted claim."""
    c = mk_claim(text="Truth BBQ is open at the query time", blocking=True,
                 status=ClaimStatus.CONTRADICTED)
    d = referee("Truth BBQ", [c])
    assert d.passed is False
    ok, v = citation_lint("Go to Truth BBQ.", survivors=[], contradicted=["Truth BBQ"])
    assert ok is False


def test_bbq_regression_pit_room_passes():
    c = mk_claim(text="The Pit Room is open at the query time", blocking=True,
                 status=ClaimStatus.SUPPORTED, evidence_ids=["e1"])
    ev = mk_ev(retrieved_at=NOW - timedelta(seconds=20), authority=Authority.PRIMARY,
               method=Method.PAGE_EXTRACT, normalized_value="open 11-9")
    c.evidence_ids = ["e1"]
    from hermes_nerve.research.contracts import can_upgrade_to_supported
    assert can_upgrade_to_supported(c, {"e1": ev}, NOW) is True
    d = referee("The Pit Room", [c])
    assert d.passed is True


# --- PR7 lint: unsupported recommendation caught ---------------------------
def test_lint_catches_unsupported_pick():
    ok, v = citation_lint("Pick: Gotham DBQ, it's great.", survivors=["The Pit Room"],
                          contradicted=[])
    assert ok is False and any("UNSUPPORTED" in x for x in v)


def test_lint_allows_survivor_and_refusal():
    ok1, _ = citation_lint("Pick: The Pit Room.", survivors=["The Pit Room"], contradicted=[])
    assert ok1 is True
    ok2, _ = citation_lint("No verified pick is available.", survivors=[], contradicted=[])
    assert ok2 is True


# --- PR9: agreeing sources keep prior status ------------------------------
def test_agreeing_sources_keep_status():
    """normalized_value is the CANONICAL form — producers normalize, the engine
    compares exactly. Two sources that normalize to the same value agree."""
    c = mk_claim(status=ClaimStatus.SUPPORTED)
    a = mk_ev(normalized_value="11:00-21:00")
    b = mk_ev(normalized_value="11:00-21:00")
    assert compare_evidence(c, [a, b]) is ClaimStatus.SUPPORTED


def test_superficially_similar_but_different_values_unresolved():
    """'open 11-9' vs '11:00-21:00' are NOT normalized to the same value here —
    the engine must not pretend they agree."""
    c = mk_claim(status=ClaimStatus.SUPPORTED)
    a = mk_ev(normalized_value="open 11-9")
    b = mk_ev(normalized_value="11:00-21:00")
    assert compare_evidence(c, [a, b]) is ClaimStatus.UNRESOLVED


# --- PR10: budget governor exhausts deterministically ----------------------
def test_budget_governor_exhaustion():
    gov = BudgetGovernor(ResearchBudget(max_wall_ms=60000, max_pages=2,
                                        max_searches=1, max_depth=0))
    gov.spend("page"); gov.spend("page")
    assert gov.allow("page") is False      # pages gone
    assert gov.allow("search") is True     # still has searches
    gov.spend("search")
    assert gov.allow("search") is False
    assert gov.allow("depth") is False     # depth budget zero
    assert gov.exhausted is True
