"""DRK PR7: citation lint — post-synthesis gate, deterministic, blocking.

Every recommendation must name a gate-survivor; a contradicted candidate may
never be recommended. Fail closed.
"""
from __future__ import annotations

import re

_REC_PATTERNS = [
    r"(?:[Pp]ick|[Gg]o to|[Hh]ead to|[Cc]hoose|[Oo]rder from)[:\s]+.{0,4}?([A-Z][A-Za-z'&.\- ]{2,40})",
    r"\*\*([A-Z][A-Za-z'&.\- ]{2,40})\*\*",
]
_STOP = {"the", "a", "an", "it", "i", "pick", "no", "none", "not"}


def _recommendation_targets(text: str) -> list[str]:
    targets = []
    for pat in _REC_PATTERNS:
        for m in re.finditer(pat, text):
            name = m.group(1).strip().rstrip(".,!?")
            if name.lower() not in _STOP and len(name) > 2:
                targets.append(name)
    return targets


def citation_lint(final_text: str, survivors: list[str],
                  contradicted: list[str]) -> tuple[bool, list[str]]:
    """Returns (ok, violations). Empty survivors => only a refusal passes."""
    v: list[str] = []
    targets = _recommendation_targets(final_text)
    low_surv = [s.lower() for s in survivors]
    low_contra = [c.lower() for c in contradicted]

    for t in targets:
        tl = t.lower()
        if any(tl.startswith(c.lower()) for c in low_contra):
            v.append(f"REBELLION: recommends contradicted candidate '{t}'")
        elif survivors and not any(tl.startswith(s.lower()) for s in low_surv):
            v.append(f"UNSUPPORTED: recommends '{t}' which is not a gate survivor")

    if not survivors:
        for t in targets:
            v.append(f"UNSUPPORTED: recommends '{t}' but no candidate passed the gate")
    return (len(v) == 0, v)


_HEDGE = re.compile(r"(?:unverified|uncertain|not verified|reported|unconfirmed|may|might|could)",
                    flags=re.I)


def lint_claims(final_text: str, registry: list[dict]) -> tuple[bool, list[str]]:
    """Claim-level gate (§12/§13): a non-SUPPORTED claim's substance may not be
    asserted as fact. Hedged phrasing within the same sentence is allowed."""
    v: list[str] = []
    for c in registry:
        if c["status"].upper() == "SUPPORTED":
            continue
        # claim text minus candidate name -> the assertion stem
        stem = c["text"]
        cand = (c.get("candidate") or "").strip()
        if cand and cand.lower() in stem.lower():
            stem = re.sub(re.escape(cand), "", stem, flags=re.I)
        stem = re.sub(r"\s+(is|was|are)\s+", " ", stem).strip()
        # find the key assertion words (len>4) present in the final text
        key_words = [w for w in re.findall(r"[A-Za-z']{5,}", stem)
                     if w.lower() in final_text.lower()]
        if len(key_words) >= 2:
            # locate first key word occurrence; check for a hedge nearby (before)
            idx = final_text.lower().find(key_words[0].lower())
            window = final_text[max(0, idx-80):idx]
            if not _HEDGE.search(window):
                v.append(f"SOFT-CLAIM-AS-FACT [{c['status']}] '{c['text']}' "
                         f"asserted without hedge (near: ...{window[-40:]!r})")
    return (len(v) == 0, v)
