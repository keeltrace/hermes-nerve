# Nerve v0.3.2.1 — Selective Context

Nerve 0.3.2.1 is a focused update on the verified v0.3.2 release.

## Purpose

Nerve now has a provider-neutral admission layer for three expensive context surfaces:

- memory;
- tool schemas;
- skill indexes.

Nerve still does not own any of those resources. Providers retrieve memory, Hermes owns and executes tools, skills remain Hermes/plugin assets, and Nerve decides only what deserves attention for the current task.

## Implemented

- ContextCandidate and AdmissionDecision contracts.
- PIN / ADMIT / DEFER / SUPPRESS actions.
- stable TaskFingerprint values and in-process admission leases.
- deterministic relevance, trust, recency, token-cost and mandatory-item handling.
- fail-open behavior for malformed/unknown candidates.
- provider-free default path.
- one optional bounded semantic-decider seam; it is disabled by default.
- Verified Experience to memory candidate adapter.
- Hermes tool schema to tool candidate adapter.
- skill metadata to skill candidate adapter.
- memory selections may be suppressed for a turn but are never deleted.
- unselected tools and skills are deferred, not made unreachable.
- token accounting: baseline, selected, gross saved and net saved.
- bounded process-local telemetry through nerve_stats(section="selective_context").
- independent off / shadow / apply configuration per surface.

## Runtime boundary

The Hermes plugin API used by Nerve does not expose a general hook that lets a plugin rewrite the already-assembled provider tool-schema array. This release therefore does not monkey-patch Hermes.

Tool and skill APPLY plans are explicit host-adapter outputs intended for Hermes existing progressive Tool Search and skill-discovery seams. Memory integrations can map provider candidates through the same controller.

This boundary is intentional: token savings must not bypass Hermes tool scope, approvals, validation, safety middleware, or prompt-cache invariants.

## Defaults

All three surfaces default to shadow mode. Semantic admission defaults off. Leases default on for one hour.

## Safety and authority

- mandatory candidates are always pinned;
- invalid candidate data fails open;
- semantic reranker failure falls back to deterministic planning;
- tool/skill non-selection is DEFER, not deletion;
- memory non-selection never mutates provider storage;
- no provider/network call occurs on the deterministic path;
- existing Nerve tool count and headless-worker behavior remain unchanged.

## Qualification

See BUILD_VERIFICATION_0.3.2.1.txt in the final package for exact observed verification results.
