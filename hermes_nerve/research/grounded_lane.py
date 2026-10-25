"""DRK PR5: the grounded open_now lane — live fetch, structured extraction,
deterministic cross-check, fail closed on ambiguity.

Milestone this exists to prove: given the Houston BBQ prompt on a Monday, a
candidate whose live hours say CLOSED must be rejected by the referee.
"""
from __future__ import annotations

import json
import re
import sys
import urllib.request
from datetime import datetime, timezone
from typing import Optional

from hermes_nerve.research.providers import muna_call

from hermes_nerve.research.contracts import (
    Authority, Claim, ClaimKind, ClaimStatus, Evidence, Freshness, Method,
)
from hermes_nerve.research.ledger import Ledger

UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) hermes-nerve-drk/0.1 (research receipt)"}
DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def fetch_page(url: str) -> Optional[str]:
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=12) as r:
            raw = r.read().decode("utf-8", errors="replace")
    except Exception as e:
        print(f"    [fetch] {url} -> {type(e).__name__}: {str(e)[:60]}")
        return None
    text = re.sub(r"<script.*?</script>|<style.*?</style>", " ", raw, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text[:12000]


def extract_hours_structured(page_text: str, business: str) -> Optional[dict]:
    """Model extracts weekly hours from page text into strict JSON."""
    prompt = (
        f"From this webpage text for '{business}', extract the weekly opening hours.\n"
        "Reply with ONLY a JSON object, one key per weekday (lowercase), value like "
        "'11:00-21:00', 'closed', or 'unknown' if not stated.\n\nPAGE TEXT:\n" + page_text[:6000])
    try:
        raw = muna_call("@google/gemma-4-26b-a4b-it", prompt)
        m = re.search(r"\{.*\}", raw, flags=re.S)
        if not m:
            return None
        data = json.loads(m.group(0))
        return {d: str(data.get(d, "unknown")).lower() for d in DAYS}
    except Exception:
        return None


def deterministic_crosscheck(page_text: str, day: str) -> str:
    """Cheap lexical check for explicit closed/open statements about one day."""
    t = page_text.lower()
    pats_closed = [rf"{day}\s*[:\-–]?\s*closed", rf"closed\s+(?:on\s+)?{day}s?\b"]
    for p in pats_closed:
        if re.search(p, t):
            return "closed"
    pats_hours = [rf"{day}\s*[:\-–]\s*\d", rf"{day}s?\s+11"]
    for p in pats_hours:
        if re.search(p, t):
            return "hours_found"
    return "unspecified"


def grounded_open_now(ledger: Ledger, run_id: str, business: str, url: str,
                      now: datetime) -> tuple[Claim, dict]:
    """Returns (open_now Claim, debug info). Claim starts UNVERIFIED with linked
    evidence; caller runs ledger.verify_claim then the referee."""
    info: dict = {"business": business, "url": url}
    page = fetch_page(url)
    if page is None:
        info["result"] = "fetch_failed"
        # Fail closed: an unfetchable source cannot support a LIVE claim.
        ev = Evidence(source_name=f"{business} (fetch failed)", extracted_text="",
                      retrieved_at=now, method=Method.PAGE_EXTRACT,
                      authority=Authority.UNKNOWN, source_url=url)
        eid = ledger.store_evidence(run_id, ev)
        claim = Claim(text=f"{business} is open at the query time", kind=ClaimKind.TEMPORAL,
                      freshness=Freshness.LIVE, blocking=True,
                      required_authority=(Authority.PRIMARY,), evidence_ids=[eid])
        return claim, info

    ev = Evidence(source_name=f"{business} official page" if "truthbbq" in url or "pitroom" in url
                  else url, extracted_text=page, retrieved_at=now,
                  method=Method.PAGE_EXTRACT, authority=Authority.PRIMARY, source_url=url)
    eid = ledger.store_evidence(run_id, ev)

    structured = extract_hours_structured(page, business)
    lex = deterministic_crosscheck(page, DAYS[now.weekday()])
    info["structured"] = structured
    info["lexical_today"] = lex

    target = DAYS[now.weekday()]
    says_closed = False
    says_open = False
    if structured:
        v = structured.get(target, "unknown")
        says_closed = v == "closed"
        says_open = bool(re.search(r"\d", v))
        info["model_hours_today"] = v
    if lex == "closed":
        says_closed = True
    elif lex == "hours_found":
        says_open = True

    # Contradiction: model says closed, lexical finds hours, or vice versa.
    if structured and says_closed and says_open and lex != "closed":
        info["result"] = "contradiction_unresolved"
    elif says_closed:
        info["result"] = "closed_today"
    elif says_open:
        info["result"] = "open_today"
    else:
        info["result"] = "unresolved"  # ambiguous page -> fail closed

    claim = Claim(text=f"{business} is open at the query time", kind=ClaimKind.TEMPORAL,
                  freshness=Freshness.LIVE, blocking=True,
                  required_authority=(Authority.PRIMARY,), evidence_ids=[eid])
    if info["result"] == "closed_today":
        claim.contradiction_ids.append(f"hours-say-closed-{target}")
    elif info["result"] != "open_today":
        claim.contradiction_ids.append(f"unresolvable-{info['result']}")
    ledger.verify_claim(run_id, claim, {eid: ev}, now)
    return claim, info
