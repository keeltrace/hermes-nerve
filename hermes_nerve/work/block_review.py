"""Block-review loop: no worker block is terminally accepted on prose.

Tests 5-10 finding: the goal judge verifies the happy exit (is DONE true)
but terminally accepts the sad exit. A well-documented block gets through
even when it is wrong (Test 9: correct commit cited, zero variants tried,
~60% budget left). This module makes the block exit symmetric with the
completion gate: every worker ``kanban_block`` produces a mechanical
evidence card, gets a deterministic first-pass verdict, and only an
unrefuted, effort-spent block reaches the human (Jev) with the card
attached.

Verdicts:
- REFUTE   — a load-bearing claim in the block is factually wrong. The
             block is bounced; the counterexample rides on the bounce.
- CONTINUE — no refutation found but laziness signals present
             (few/no distinct approaches, budget remaining). Bounced
             with a work order.
- ESCALATE — claims verified and effort spent. Returns None (the native
             block proceeds) and the card is staged for review.

Deterministic first pass only. Jev (LLM judge) reads the staged card
asynchronously; this module never spends an LLM call inside a hook.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .models import RunIdentity, utc_now

# --- tuning constants (kept conservative to protect honest blocks) ---------

LAZY_MAX_VARIANTS = 1          # <= this many distinct approaches => lazy signal
LAZY_MAX_BUDGET_FRACTION = 0.60  # blocked below this budget fraction => lazy signal
MAX_BLOCK_REVIEWS_PER_RUN = 2  # bounce a given run at most this many times


@dataclass(frozen=True)
class BlockReview:
    verdict: str                 # REFUTE | CONTINUE | ESCALATE
    card: dict[str, Any]
    reasons: list[str] = field(default_factory=list)

    @property
    def bounce(self) -> bool:
        return self.verdict in {"REFUTE", "CONTINUE"}


def _distinct_variants(store, identity: RunIdentity) -> int:
    """Count distinct implementation approaches from observed tool evidence.

    A 'variant' is a unique (tool, normalized-target) write attempt against
    a source file, or a unique distinct failing assertion signature. Repeated
    identical failures are retries, not variants.
    """
    try:
        records = store.evidence(identity, include_stale=True)
    except Exception:
        return 0
    targets: set[str] = set()
    fail_sigs: set[str] = set()
    for rec in records:
        tool = str(rec.get("tool_name") or "")
        preview = str(rec.get("preview") or "")
        if tool in {"write_file", "file_write", "patch", "apply_patch", "edit_file"}:
            targets.add(preview[:120])  # distinct write targets by preview head
        elif tool in {"terminal", "terminal.exec", "shell", "bash"}:
            for m in re.finditer(r"(?:>|>>\s*)(\S+\.py)\b", preview):
                targets.add(m.group(1))
        if rec.get("is_error") or "FAILED" in preview or "AssertionError" in preview:
            fail_sigs.add(re.sub(r"0x[0-9a-f]+", "", preview)[:200])
    return len(targets) + len(fail_sigs)


def _budget_fraction(sup, identity: RunIdentity) -> float:
    try:
        from .budget import budget_state
        contract = sup.active_contract(identity.task_id)
        if contract is None:
            return -1.0
        state = budget_state(sup.store, identity, contract)
        return round(state.consumed_tokens / state.allocated_tokens, 4) if state.allocated_tokens else -1.0
    except Exception:
        return -1.0


def _check_claims(store, identity: RunIdentity, reason: str) -> list[dict[str, Any]]:
    """Verify the block's checkable factual assertions against observed evidence.

    Deterministic only: commit hashes must exist in observed git output,
    quoted test failures must appear in observed results.
    """
    checks: list[dict[str, Any]] = []
    try:
        records = store.evidence(identity, include_stale=True)
    except Exception:
        return checks
    observed_blob = "\n".join(str(rec.get("preview") or "") for rec in records)
    # commits cited like 9d163eb or full shas
    for sha in set(re.findall(r"\b[0-9a-f]{7,40}\b", reason or "")):
        checks.append({
            "claim": f"commit {sha[:7]} exists in observed git output",
            "verified": sha in observed_blob,
        })
    # test names cited like test_foo or path::test
    for t in set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*::test_[A-Za-z0-9_]+|test_[A-Za-z0-9_]+", reason or "")):
        checks.append({
            "claim": f"observed output mentions {t}",
            "verified": t in observed_blob,
        })
    return checks



_SEMANTIC_QUIT_MARKERS = (
    # Interpretation-surrender phrasings seen in live blocks. Keep generic:
    # the flag corroborates with edit-diversity before it means anything.
    "which ruling", "which seed", "which value", "which constant",
    "confirm which", "awaiting guidance", "need a decision",
    "needs a decision", "which is authoritative", "which is canonical",
    "clarify which", "which interpretation", "before proceeding",
    "need human decision", "requires human judgment",
)


def _semantic_laziness_flags(store, identity: RunIdentity, reason: str) -> list[str]:
    """Detect the interpretation-level quit: the worker asks the human to make
    a judgment call it could have tested. Deliberately conservative — these are
    flags for the human judge, not automatic refutations."""
    flags: list[str] = []
    text = (reason or "").lower()
    if any(m in text for m in _SEMANTIC_QUIT_MARKERS):
        flags.append("asks-human-for-interpretation-decision")
        # Corroborate: did the worker ever attempt to modify the disputed code
        # itself (any commit/patch beyond its first)? Variants already counted;
        # here we look for edit-diversity on the disputed file.
        try:
            records = store.evidence(identity, include_stale=True) or []
            edit_targets: set[str] = set()
            for rec in records:
                tool = str(rec.get("tool_name") or "")
                if tool in {"write_file", "file_write", "patch", "apply_patch", "edit_file"}:
                    edit_targets.add(str(rec.get("preview") or "")[:120])
            if len(edit_targets) <= 1:
                flags.append(f"edited-only-{len(edit_targets)}-distinct-target(s)-before-asking")
        except Exception:
            pass
    return flags


def review_block(sup, identity: RunIdentity, *, reason: str) -> BlockReview:
    """Deterministic first-pass verdict on a worker block."""
    store = sup.store
    variants = _distinct_variants(store, identity)
    frac = _budget_fraction(sup, identity)
    claims = _check_claims(store, identity, reason)
    refuted = [c for c in claims if not c["verified"]]
    prior_reviews = 0
    try:
        diag = store.latest_diagnostic(
            task_id=identity.task_id, run_id=identity.run_id, kind="nerve_block_review"
        )
        if diag:
            prior_reviews = int((diag.get("payload") or {}).get("review_count") or 0)
    except Exception:
        pass

    reasons: list[str] = []
    if refuted:
        reasons.append(
            "refuted claims: " + "; ".join(c["claim"] for c in refuted)
        )
    if variants <= LAZY_MAX_VARIANTS:
        reasons.append(f"only {variants} distinct implementation approach(es) tried")
    if 0.0 <= frac < LAZY_MAX_BUDGET_FRACTION:
        reasons.append(f"blocked at {frac:.0%} of token budget")

    card = {
        "task_id": identity.task_id,
        "run_id": identity.run_id,
        "block_reason": (reason or "")[:2000],
        "variants_tried": variants,
        "budget_fraction_consumed": frac,
        "claim_checks": claims,
        "refuted_claims": [c["claim"] for c in refuted],
        "prior_block_reviews": prior_reviews,
        # Round-2 refinement (Tests 11-12c): variant COUNT can't see the
        # semantic quit — a worker that applied the mandated seed and stopped
        # at the guard "decision" tried 5 things yet attempted no fix-class
        # on the wall itself. Flag it for the human judge (Jev).
        "semantic_laziness_flags": _semantic_laziness_flags(store, identity, reason),
    }

    if refuted:
        verdict = "REFUTE"
    elif variants <= LAZY_MAX_VARIANTS and (frac < 0.0 or frac < LAZY_MAX_BUDGET_FRACTION):
        if prior_reviews >= MAX_BLOCK_REVIEWS_PER_RUN:
            # two-strike: lazy signals persist after bounces -> escalate, not loop
            verdict = "ESCALATE"
        else:
            verdict = "CONTINUE"
    elif not reasons:
        verdict = "ESCALATE"
    else:
        verdict = "CONTINUE"

    card["verdict"] = verdict
    card["reasons"] = reasons
    return BlockReview(verdict=verdict, card=card, reasons=reasons)


def stage_for_jev(store, identity: RunIdentity, review: BlockReview) -> None:
    """Persist the card so the reviewer lane (Jev) can read it."""
    try:
        store.add_diagnostic(
            task_id=identity.task_id,
            run_id=identity.run_id,
            kind="jev_block_review_card",
            payload=review.card,
            created_at=utc_now(),
        )
    except Exception:
        pass


def bounce_message(review: BlockReview) -> dict[str, Any]:
    """The pre_tool_call block-action that bounces a REFUTE/CONTINUE verdict."""
    if review.verdict == "REFUTE":
        head = (
            "Nerve block review REFUTED this block: one or more factual claims "
            "in your block reason are not supported by observed evidence. "
        )
    else:
        head = (
            "Nerve block review returned CONTINUE: the block is unverified and "
            "effort signals do not support stopping yet. "
        )
    work_order = (
        "Before blocking again you must: (1) try at least "
        f"{LAZY_MAX_VARIANTS + 2} distinct implementation approaches against the "
        "stated obstacle, and (2) either complete the task or produce a "
        "falsification-grade impossibility proof (every alternative attempted, "
        "each with its observed failure). "
    )
    counter = ""
    refuted = review.card.get("refuted_claims") or []
    if refuted:
        counter = (
            " Address the refuted claims first: " + "; ".join(refuted) + ". "
            "A re-block that still asserts them will auto-escalate with the "
            "refutation attached."
        )
    return {
        "action": "block",
        "message": head + work_order + counter + f"[review card: {review.verdict}]",
        "rule_key": "jev:work-block-review",
    }
