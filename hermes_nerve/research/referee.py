"""DRK PR4: the blocking referee gate — pure, deterministic, fail closed.

Models may propose. Evidence may support. The referee decides what is allowed
to become fact.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from hermes_nerve.research.contracts import Claim, ClaimStatus


@dataclass
class RefereeDecision:
    candidate: str
    passed: bool
    reasons: list[str] = field(default_factory=list)
    allowed_claim_ids: list[str] = field(default_factory=list)


def referee(cand_name: str, claims: list[Claim]) -> RefereeDecision:
    """A candidate passes only if every blocking claim is SUPPORTED.
    Non-blocking claims never block; unresolved non-blocking claims are
    disclosed downstream but cannot kill the candidate."""
    reasons: list[str] = []
    allowed: list[str] = []
    for c in claims:
        if c.blocking and c.status is not ClaimStatus.SUPPORTED:
            reasons.append(f"BLOCKING FAIL [{c.freshness.value}] {c.text} -> {c.status.value}")
        else:
            allowed.append(c.id)
            if c.status in (ClaimStatus.UNRESOLVED, ClaimStatus.UNVERIFIED):
                reasons.append(f"disclose [{c.freshness.value}] {c.text} -> {c.status.value}")
    return RefereeDecision(candidate=cand_name, passed=not reasons or all(
        not r.startswith("BLOCKING") for r in reasons), reasons=reasons,
        allowed_claim_ids=allowed)
