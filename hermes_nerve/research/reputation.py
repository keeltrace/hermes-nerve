"""DRK PR8: reputation lane — search + extract, budget-governed, receipts.

Replaces lane memory with live search: fetch an authoritative page (Texas
Monthly top-25 etc.), extract reputation statements per candidate, store as
SECONDARY evidence. Reputation is 'current' freshness — never LIVE, so the
claim types stay honest.
"""
from __future__ import annotations

import html
import re
import urllib.parse
import urllib.request
from datetime import datetime

from hermes_nerve.research.budget import BudgetGovernor
from hermes_nerve.research.contracts import Authority, Claim, ClaimKind, ClaimStatus, Evidence, Freshness, Method
from hermes_nerve.research.ledger import Ledger

UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) hermes-nerve-drk/0.1"}

#: curated primary/secondary sources for Houston BBQ reputation (stable, citable)
REPUTATION_SOURCES = [
    "https://en.wikipedia.org/wiki/Truth_BarbQue",
    "https://en.wikipedia.org/wiki/Houston",
    "https://www.texasmonthly.com/bbq/top-25-barbecue-joints-guide/",
    "https://houston.eater.com/maps/best-barbecue-houston-texas-map",
]


def _fetch(url: str, gov: BudgetGovernor) -> str | None:
    if not gov.allow("page"):
        return None
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=10) as r:
            raw = r.read().decode("utf-8", errors="replace")
        gov.spend("page")
        text = re.sub(r"<script.*?</script>|<style.*?</style>", " ", raw, flags=re.S | re.I)
        text = html.unescape(re.sub(r"<[^>]+>", " ", text))
        return re.sub(r"\s+", " ", text)[:14000]
    except Exception:
        return None


def reputation_claims(ledger: Ledger, run_id: str, candidates: list[str],
                      now: datetime, gov: BudgetGovernor | None = None) -> dict[str, Claim]:
    """One claim per candidate: '<X> is well regarded for barbecue', grounded in
    whichever reputation source actually mentions it. Unmentioned -> UNVERIFIED."""
    gov = gov or BudgetGovernor(max_wall_ms=4000, max_pages=3, max_searches=0, max_depth=1)
    claims: dict[str, Claim] = {}
    source_text: list[tuple[str, str]] = []
    for url in REPUTATION_SOURCES:
        page = _fetch(url, gov)
        if page:
            source_text.append((url, page))
        if len(source_text) >= 2:
            break

    for name in candidates:
        ev_ids: list[str] = []
        last_ev = None
        for url, page in source_text:
            if re.search(re.escape(name), page, flags=re.I):
                m = re.search(r".{120}" + re.escape(name) + r".{200}", page, flags=re.I)
                ev = Evidence(source_name=url, source_url=url,
                              extracted_text=m.group(0) if m else name,
                              retrieved_at=now, method=Method.PAGE_EXTRACT,
                              authority=Authority.SECONDARY)
                eid = ledger.store_evidence(run_id, ev)
                ev_ids.append(eid)
                last_ev = ev
        claim = Claim(text=f"{name} is well regarded for barbecue",
                      kind=ClaimKind.SUBJECTIVE, freshness=Freshness.CURRENT,
                      blocking=False, required_authority=(Authority.SECONDARY,),
                      evidence_ids=ev_ids)
        if last_ev:
            claim.status = (ClaimStatus.SUPPORTED
                            if claim.freshness is Freshness.CURRENT
                            and last_ev.authority in claim.required_authority
                            else ClaimStatus.UNRESOLVED)
        ledger.register_claim(run_id, claim, candidate=name)
        claims[name] = claim
    return claims
