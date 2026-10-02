"""DRK PR9: contradiction engine — normalized cross-source comparison.

Two sources that disagree produce UNRESOLVED, never a silent average.
"""
from __future__ import annotations

from hermes_nerve.research.contracts import Claim, ClaimStatus, Evidence


def compare_evidence(claim: Claim, evidence_items: list[Evidence]) -> ClaimStatus:
    """If >=2 evidence items carry normalized_values, they must agree.
    Disagreement -> UNRESOLVED regardless of authority (never pretend)."""
    values = [e.normalized_value for e in evidence_items
              if e.normalized_value is not None]
    if len(values) >= 2 and len({str(v) for v in values}) > 1:
        return ClaimStatus.UNRESOLVED
    return claim.status  # no cross-source conflict: prior status stands
