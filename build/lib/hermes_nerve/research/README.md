# Nerve Deep Research Kit (DRK)

Grounding layer for Nerve's parallel fan-out answering. Prevents
**stable-but-wrong** answers produced from ungrounded model memory.

## The invariant

> Models may propose. Evidence may support. The referee decides what is
> allowed to become fact.

## Architecture

```text
question → candidate lanes (fast models, parallel)
         → verification (grounded open-now, search+extract reputation)
         → evidence ledger (SQLite receipts: URL, hash, excerpt, timestamps)
         → claim registry (unverified / supported / contradicted / unresolved)
         → blocking referee gate (fail closed)
         → synthesizer (sees only the registry)
         → citation lint (target-level + claim-level; blocks on violation)
         → answer
```

## Modules

| module | role |
|---|---|
| `contracts` | Claim/Evidence types, freshness tiers, upgrade guard |
| `ledger` | SQLite evidence receipts, claim registry, referee decisions |
| `referee` | deterministic blocking gate — a failed blocking claim rejects the candidate |
| `grounded_lane` | live page fetch → hours extraction → deterministic cross-check |
| `reputation` | search+extract reputation evidence (SECONDARY authority) |
| `contradictions` | normalized cross-source comparison; disagreement → UNRESOLVED |
| `budget` | deterministic wall/page/search/depth budgets; the same engine for 4s or 120s runs |
| `lint` | post-synthesis gate: no unverified claim asserted as fact, no rejected candidate recommended |
| `providers` | env-based model bindings (`MUNA_API_BASE`, `MUNA_API_KEY`, `NOUS_API_TOKEN`) |

## Proof it works (the Monday test)

Given "best BBQ within a 10-minute Uber" asked at noon on a Monday, the
knowledge-only pipeline confidently recommends a closed restaurant. With DRK:

- candidate's live hours page says closed → claim `contradicted` → referee **REJECT**
- a verified-open candidate passes and is the only selectable pick
- if the synthesizer asserts an unverified reputation claim, citation lint
  fails the draft and the rewrite is forced to hedge

Run the tests: `pytest tests/test_drk_test_contracts.py tests/test_drk_test_adversarial.py`
(21 tests, hermetic, no network required.)

## Design rules

- **Fail closed.** Missing evidence, wrong authority, stale data, ambiguous
  pages — all produce "unresolved"/reject, never a guessed pass.
- **Model memory never supports a live claim.** Encoded in
  `contracts.can_upgrade_to_supported`, unit-tested.
- **Consistency is not correctness.** A stable wrong answer is worse than an
  honest refusal; the lint accepts a refusal, never an unsupported pick.
- **Depth is a budget setting,** not a different architecture.
