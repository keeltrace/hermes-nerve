# Nerve v0.3.2 — Verified Experience

Nerve 0.3.2 builds from the exact stable v0.3.1 release commit
`3de7d94982b7a2cb87c829e4b2366ced927d8fed`.

The release adds a local **Verified Experience** layer around primitives Nerve already
had: the context evidence ledger, work-supervision evidence and checkpoints,
decision receipts, outcome learning, and failure fingerprints.

The design rule is:

> Nerve remembers verified experience, not conversations.

Hermes remains the authority for canonical task state and policy. Verified
Experience is advisory, provider-free on its normal path, and never mutates
configuration during reconciliation.

## What is new

### Typed claims

`hermes_nerve.experience.ExperienceStore` stores append-only claim events at
`$HERMES_HOME/nerve/experience.jsonl` by default.

Claims carry:

- stable claim ID;
- subject and redacted statement;
- type: `observed | inferred | verified | disproven`;
- evidence IDs;
- confidence and explicit polarity;
- scope for repository, branch, task type, worker, backend, host, and environment;
- active/superseded/contradicted/expired state;
- temporal validity and last verification;
- supersession/conflict links;
- provenance;
- estimated retrieval cost and outcome/usefulness counters.

Verified and disproven claims require evidence IDs.

### Negative knowledge

Repeated or deterministic local failure replans persist first-class
`disproven` claims keyed by Nerve's existing failure/action fingerprints.
This records "do not retry this unchanged path" as evidence-backed experience
instead of another transcript note.

### Claim supersession and conflicts

A claim may explicitly supersede an earlier claim. Deterministic conflict
detection flags high-confidence opposite-polarity claims with the same subject
when their scopes overlap. Contradictory claims are excluded from normal
retrieval unless explicitly requested.

### Temporal and scoped retrieval

Expired claims are not retrieved. Scope mismatches fail closed. Global claims
may still participate with a lower scope score.

Retrieval is deterministic and ranks by:

- lexical relevance;
- verified/disproven status;
- confidence;
- scope match;
- recency;
- observed historical usefulness;
- estimated inclusion-token cost.

Every result contains score components and `why_retrieved` metadata.

### Token-aware admission

Claim admission estimates whether durable memory is likely to earn its cost.
Low-confidence, low-evidence, expensive, low-reuse inferred memories can be
rejected without a provider call. Verified controller facts naturally score
higher.

### Evidence graph

`ExperienceStore.evidence_graph()` exposes a bounded graph across claims,
evidence IDs, supersession/conflict edges, linked outcomes, decision IDs, and
receipt IDs. It reuses existing Nerve receipts rather than introducing a
second decision-receipt format.

### Automatic verified-fact capture

When work supervision verifies a DoD criterion:

- a Reflex-backed `PASS` may produce a verified claim tied to its evidence and
  receipt;
- a deterministic PASS with explicit evidence produces a verified claim tied
  to the deterministic verifier.

The canonical work store remains authoritative. Experience is reusable
advisory knowledge only.

### Successor-run capsules

Existing `CheckpointPacket` data is promoted into a compact run capsule when
Verified Experience is enabled. Capsules preserve:

- source task/run/contract/checkpoint identity;
- verified criteria;
- failed paths from criterion/test/blocker events;
- discoveries from plan/artifact/test-pass events;
- evidence IDs;
- artifacts;
- unresolved frontier;
- suggested next move;
- bounded summary and scope.

Work status can expose recent task capsules to the controller. No capsule can
change canonical Kanban state.

### Failure fingerprint recovery playbooks

Recovery observations aggregate by existing failure fingerprint + recovery
action + scope. A recovery becomes a reusable playbook only after repeated
success clears a local sample/rate floor.

Lookup returns the exact fingerprint match, historical success rate,
confidence, scope score, and retrieval reasons. This creates a path for
common recoveries to become local System-1 behavior without another Reflex
call.

### Outcome-linked learning

Nervous `OUTCOME` / `CHALLENGE_OUTCOME` events may now attribute:

- success/failure and token savings to a claim using `claim_id`;
- a recovery outcome to a failure fingerprint using
  `failure_fingerprint + recovery` (or `next_action`).

Decision and receipt IDs remain attached where supplied.

### Hierarchical summaries

`hierarchical_summary()` provides compact run, task, project, and global
lesson views. It is derived from structured experience rather than raw
conversation history.

### Explainability

Named profiles with `context_governor` enabled expose:

`nerve_context_explain`

It can:

- explain a claim by ID, including evidence/provenance/history; or
- retrieve relevant experience and return exactly why each item was selected.

The tool performs no provider call.

### Offline reconciliation

`ExperienceStore.reconcile_runtime()` combines local experience with Nerve's
existing receipt and outcome reports. It can recommend review of:

- expired claims;
- contradictory claims;
- strong recovery playbooks;
- provider admission thresholds when disagreements are mostly useless;
- provider calls that appear not to be earning their cost.

Every recommendation is `review_required` and
`applies_automatically=false`. Reconciliation never edits profile/config
state.

CLI:

```bash
nerve experience status
nerve experience search --query "completion retry" --scope '{"repository":"keeltrace/hermes-nerve"}'
nerve experience explain --claim-id ncl-...
nerve experience summary --scope '{"repository":"keeltrace/hermes-nerve"}'
nerve experience graph --claim-id ncl-...
nerve experience reconcile
```

## Profile and compatibility behavior

Verified Experience intentionally does **not** add a new Nerve module.

It is owned by the existing `context_governor` and `local_learning`
capabilities:

- Legacy: runtime contract remains the historical 16-tool surface and Verified
  Experience automatic capture is OFF.
- Fat Cat: enabled through context/local-learning ownership.
- Operator: enabled through context/local-learning ownership.
- Lean: no new tool schema; bounded work-supervision evidence/capsule capture is enabled locally.
- Marie Kondo: no new tool schema; bounded work-supervision evidence/capsule capture is enabled locally.
- Headless Kanban workers: still expose zero Nerve tool schemas.

The core package remains dependency-free and Python 3.10+.

## Storage and privacy

Experience is append-only JSONL using Nerve's existing serialized JSONL writer.
Statements, provenance, recovery descriptions, artifacts, and scope values pass
through the existing privacy redactor before persistence. There is no
background service, network access, embedding model, or provider dependency.

Override the store path for testing/operations with:

`HERMES_NERVE_EXPERIENCE=/path/to/experience.jsonl`

## Deliberate limitations

0.3.2 does not introduce semantic embeddings or a vector database. Retrieval is
local and deterministic. Automatic contradiction detection is deliberately
conservative: it requires the same normalized subject, overlapping scope,
sufficient confidence, and opposite explicit polarity.

Recovery playbooks require observable outcomes; Nerve does not invent success.
Reconciliation recommends policy changes but cannot apply them.

## Verification in this build environment

The release was compiled and exercised with the dependency-free Python runtime.
The dedicated v0.3.2 experience suite and broad unittest regressions pass.

The sandbox image does not contain the optional development packages `pytest`
or `PyYAML`, and outbound package installation is blocked by DNS/network
policy. Therefore the canonical pytest invocation and the one unittest module
that imports `yaml` could not run in this environment. See
`BUILD_VERIFICATION_0.3.2.txt` for exact commands and results.
