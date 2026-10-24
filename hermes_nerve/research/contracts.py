"""Deep Research Kit — PR1: claim contracts + freshness (Python port of the DRK spec).

Architectural rule (from the plan, keep this sentence in the codebase):
    Models may propose. Evidence may support. The referee decides what is allowed
    to become fact.

PR1 scope: pure dataclasses + deterministic freshness logic. No networking.
The five contract tests from spec section 1 live in test_contracts.py.
"""
from __future__ import annotations

import enum
import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional


class ClaimStatus(str, enum.Enum):
    UNVERIFIED = "unverified"
    SUPPORTED = "supported"
    CONTRADICTED = "contradicted"
    UNRESOLVED = "unresolved"


class Freshness(str, enum.Enum):
    LIVE = "live"
    TODAY = "today"
    H24 = "24h"
    CURRENT = "current"
    HISTORICAL_OK = "historical_ok"


class ClaimKind(str, enum.Enum):
    FACTUAL = "factual"
    TEMPORAL = "temporal"
    GEOGRAPHIC = "geographic"
    SUBJECTIVE = "subjective"
    COMPUTED = "computed"


class Method(str, enum.Enum):
    SEARCH = "search"
    PAGE_EXTRACT = "page_extract"
    API = "api"
    CRAWL = "crawl"
    MODEL_MEMORY = "model_memory"


class Authority(str, enum.Enum):
    PRIMARY = "primary"
    SECONDARY = "secondary"
    AGGREGATOR = "aggregator"
    UNKNOWN = "unknown"


# Deterministic freshness windows (seconds). Boring on purpose.
LIVE_MAX_AGE_S = 300          # live claims tolerate 5 minutes of retrieval age
H24_WINDOW_S = 24 * 3600
CURRENT_MAX_AGE_S = 7 * 24 * 3600  # "current" official-doc tier: one week

#: model_memory is recall, not retrieval — it can never satisfy a fresh claim.
_RETRIEVAL_METHODS = {Method.SEARCH, Method.PAGE_EXTRACT, Method.API, Method.CRAWL}


@dataclass
class Evidence:
    source_name: str
    extracted_text: str
    retrieved_at: datetime
    method: Method
    authority: Authority = Authority.UNKNOWN
    source_url: Optional[str] = None
    normalized_value: Any = None
    published_at: Optional[datetime] = None
    effective_at: Optional[datetime] = None

    def content_hash(self) -> str:
        h = hashlib.sha256()
        h.update(self.extracted_text.encode("utf-8"))
        h.update(str(self.retrieved_at.timestamp()).encode())
        return h.hexdigest()[:16]

    @property
    def is_model_memory(self) -> bool:
        return self.method is Method.MODEL_MEMORY


@dataclass
class Claim:
    text: str
    kind: ClaimKind
    freshness: Freshness
    status: ClaimStatus = ClaimStatus.UNVERIFIED
    blocking: bool = False
    required_authority: tuple = (Authority.PRIMARY, Authority.SECONDARY)
    evidence_ids: list[str] = field(default_factory=list)
    contradiction_ids: list[str] = field(default_factory=list)
    id: str = field(default_factory=lambda: f"claim-{hashlib.sha1(str(datetime.now(timezone.utc).timestamp()).encode()).hexdigest()[:8]}")


@dataclass
class VerificationResult:
    claim_id: str
    status: ClaimStatus
    evidence_ids: list[str]
    reason: str


def evidence_satisfies_freshness(claim: Claim, evidence: Evidence, now: datetime) -> bool:
    """Pure, deterministic. False is always safe: fail closed."""
    age_s = (now - evidence.retrieved_at).total_seconds()

    if claim.freshness is Freshness.HISTORICAL_OK:
        return True
    if evidence.is_model_memory:
        return False  # memory of a source is not a source

    if claim.freshness is Freshness.LIVE:
        return 0 <= age_s <= LIVE_MAX_AGE_S

    if claim.freshness is Freshness.TODAY:
        if evidence.effective_at is not None:
            return evidence.effective_at.date() == now.date()
        return evidence.retrieved_at.astimezone(now.tzinfo).date() == now.date()

    if claim.freshness is Freshness.H24:
        ref = evidence.published_at or evidence.retrieved_at
        return 0 <= (now - ref).total_seconds() <= H24_WINDOW_S

    if claim.freshness is Freshness.CURRENT:
        return (
            evidence.authority is Authority.PRIMARY
            and 0 <= age_s <= CURRENT_MAX_AGE_S
        )

    return False  # unknown requirement: fail closed


def can_upgrade_to_supported(claim: Claim, evidence_by_id: dict[str, Evidence], now: datetime) -> bool:
    """Claim-registry guard: an unsupported claim cannot become 'supported' unless
    its evidence set is non-empty and every item satisfies the claim's freshness
    and authority requirements. Pure; the referee (PR4) will call this."""
    if claim.status is ClaimStatus.CONTRADICTED:
        return False
    if not claim.evidence_ids:
        return False
    for eid in claim.evidence_ids:
        ev = evidence_by_id.get(eid)
        if ev is None:
            return False
        if not evidence_satisfies_freshness(claim, ev, now):
            return False
        if claim.required_authority and ev.authority not in claim.required_authority:
            return False
    return True
