# Nerve / Hermes test campaign findings — 2026-09-30

> Public sanitized reconstruction of the September 29–30 Nerve test campaign.
>
> This document intentionally excludes credentials, private filesystem details, unrelated private project data, and raw provider payloads. It distinguishes Nerve defects from Hermes/provider/harness defects and preserves invalidated measurements as invalid rather than silently dropping them.

## Executive summary

The campaign tested Nerve as a supervisory layer for Hermes under adversarial and A/B workloads. The strongest conclusion was not that Nerve always saves tokens. The useful result was that Nerve can improve completion integrity and recovery behavior when its local deterministic controls are grounded in current workspace evidence.

The campaign exposed four classes of important issues:

1. **Nerve correctness defects** — stale completion verdicts, retry loops on deterministic failures, false progress, environment thrash, and an overlong context-curation working set.
2. **Nerve quality-of-life defects** — mutation aliases, scratch-file progress, verification accounting, and clearer local recovery behavior.
3. **Hermes/provider defects** — interrupted usage accounting and provider/profile resume behavior that could silently invalidate A/B attribution.
4. **Harness/environment contamination** — runs that appeared comparable until provider provenance, missing dependencies, or collection behavior showed they were not.

The maintenance fixes that survived review were re-landed onto clean current-main ancestry on 2026-10-01.

## Testing rules established by the campaign

Future Nerve tests should follow these rules:

- Compare Nerve OFF vs ON on the same fixture whenever possible.
- Verify the **actual provider and model route** for every live A/B arm.
- Mark a run invalid when routing, dependency, or harness contamination breaks comparability.
- Treat truthful completion and useful intervention as primary metrics; token reduction is secondary.
- Keep these failure classes separate:
  1. Nerve bug
  2. Hermes bug
  3. provider/routing problem
  4. harness/environment defect
  5. feature/QoL opportunity
  6. rejected hypothesis
- Do not treat activity as progress. Progress must be task-relative.
- Do not treat an old verification result as current evidence.
- Prefer deterministic local controls when a failure can be classified without another model call.

## Campaign results

### Runtime activation

The Nerve runtime-activation suite reached **14/14 passing** during the campaign.

This established that the test harness could observe the intended Nerve hooks and state before broader A/B interpretation.

### 40-sample OFF/ON A/B

A 40-sample comparison produced:

| Metric | Nerve OFF | Nerve ON |
| --- | ---: | ---: |
| Correctness | 19/20 | 20/20 |
| Formatting | 16/20 | 20/20 |
| Completion | 17/20 | 18/20 |
| Wall time | 19.93 s | 17.20 s |
| Tokens | 124.7k | 132.0k |

Interpretation:

- Nerve slightly improved correctness and completion in this sample.
- Formatting improved materially.
- Wall time was lower.
- Token usage was **higher**, not lower.

This invalidates any blanket claim that Nerve automatically saves tokens on every workload.

Earlier experiments had shown 30–50% token savings and 4/4 completion versus 1/4 control under a different enforcement setup. Those results remain useful evidence of leverage, but they are not interchangeable with the later 40-sample run.

## Findings that became maintenance fixes

### Deterministic failure retry loops

**Problem:** obvious deterministic failures could consume additional retries/provider work even when repeating the exact action could not change the outcome.

Examples included policy/approval blocks, invalid arguments, missing paths, and other stable failures.

**Desired behavior:**

- deterministic failure -> local REPLAN immediately
- transient failure -> preserve bounded retry behavior
- exact repeated failure -> deduplicate and stop redundant provider calls

The original work appeared in PR #42. It was later re-landed without unrelated feature ancestry in **PR #50**.

### Stale completion / false VERIFIED_PASS

**Problem:** a previous machine-checkable PASS could remain authoritative after the workspace changed.

Adversarial reproductions:

- a required file existed during verification and was then removed
- a test passed and later became failing
- git was clean and later gained an untracked file

Before the fix, all three could retain the old PASS.

**Correct rule:** every machine-checkable non-summary Definition-of-Done criterion is re-evaluated against current state on every completion attempt.

The original work appeared in PR #43. It was re-landed cleanly in **PR #49**.

### Task-relative progress integrity

**Problem:** Nerve could confuse activity with progress.

The hardened behavior:

- scratch/repro-only writes do not satisfy implementation progress
- native patch/apply aliases count as mutations
- mutations must be relevant to the requested task/path where that can be determined
- a coding task that explicitly requests verification does not complete until a successful verification signal exists
- common zero-failure summaries such as “12 passed, 0 failed, 0 errors” are accepted
- repeated environment setup/test friction triggers a local recovery lease rather than endless environment churn

The strongest surviving implementation was PR #46 and was re-landed with deterministic retry handling in **PR #50**.

### Long-session context-curation cap

**Problem:** the context engine could build an oversized semantic-curation working set in long sessions.

The final behavior on current main:

- at most 48 eligible evidence items are curated in one pass
- when more than 48 are eligible, the **most recent** 48 are selected
- older eligible evidence remains exact and deferred
- anchors and deterministically unrecoverable items remain excluded
- selection is deterministic
- failures remain fail-open
- telemetry records total/selected/skipped candidate counts and exception type without leaking raw provider error text

Boundary coverage includes 47, 48, 49, and 80 eligible items plus repeated-compression behavior.

Landed in **PR #48**.

## Action-gate contributor hardening

Contributor work introduced ask-only enforcement in PR #41.

Maintainer follow-up in PR #44 corrected:

- global approval-key behavior -> action-specific approval keys
- environment fallback behavior
- stale tool-gate contract material
- regression coverage

The result is on current main. This was deliberately **not** added retroactively to the maintenance-only `v0.3.1rc1` release candidate.

## Policy-memory experiments

Policy-memory/config/TTL tests reached **36/36** in the campaign.

These tests produced useful evidence, but policy-memory behavior is a feature/policy change rather than a maintenance fix. It was intentionally excluded from the maintenance release/reconciliation path.

## Hermes/provider findings exposed by Nerve testing

These are not Nerve repository defects and should not be “fixed” by hiding them inside Nerve.

### Interrupted usage accounting

An interrupted/Ctrl-C Muna execution could lose token-accounting information.

This was tracked in Hermes rather than Nerve.

### Resume/profile loss and provider fallback

A resumed session with partial options could lose the intended profile/provider selection and silently route through a different provider/model.

Consequences:

- some early A/B measurements became invalid
- model/provider provenance must be verified for every future live comparison
- “requested model” is not sufficient evidence of “served model”

### Stale unknown-toolset warning

Hermes could emit an outdated warning about the Nerve/Jev toolset even though the functionality worked.

This was already covered upstream and did not justify a duplicate Nerve fix.

## Invalidated or limited evidence

The campaign deliberately retained these limitations:

- Any A/B arm whose actual served provider/model could not be verified is **invalid for provider/model comparison**.
- Live Muna/Gemma attempts affected by routing fallback are not treated as clean model A/B evidence.
- Local full-suite runs with missing dependencies are not counted as implementation failures when focused tests and clean CI demonstrate the code path independently.
- The earlier PR #45 Python 3.14 failure was a package-verifier/pytest collection problem around an imported helper named `test_succeeded`; that branch is superseded rather than repaired independently.
- Token savings observed in one benchmark must not be generalized to workloads where Nerve adds supervision cost without preventing wasted work.

## Release/repository reconciliation

### Published maintenance RC

`v0.3.1rc1` remains an immutable maintenance-only release candidate.

Its validated GitHub Actions matrix passed:

- Python 3.10
- Python 3.11
- Python 3.12
- Python 3.13
- Python 3.14
- Hermes Plugin Validate
- Hermes Plugin Doctor
- package/release verification

The RC intentionally excludes ask-only and other feature tracks.

### Current main

Current main is a later development line and is not byte-identical to the RC.

As of the repository cleanup:

- PR #48 — long-session context cap
- PR #49 — stale completion integrity
- PR #50 — deterministic retry + task-relative progress reconciliation
- PR #51 — development/release identity reconciliation

Current main identifies as `0.3.2.dev0`.

The public Hermes catalog intentionally remains pinned to the latest stable release while main carries a `.dev` version.

## Separate feature tracks

The following are intentionally outside this maintenance baseline:

- Deep Research Kit (#37)
- block-review judge (#38)
- Shared Context publication (#39 / draft #40)
- policy-memory feature work

They should be reviewed and released independently.

## Recommended regression set

A future release should retain at least these focused gates:

1. deterministic vs transient failure classifier matrix
2. repeated-failure deduplication
3. stale completion:
   - removed required file
   - newly failing test
   - newly dirty/untracked git state
4. progress integrity:
   - scratch mutation does not count
   - requested-path patch counts
   - requested verification must pass
   - zero-failure summary accepted
   - environment thrash recovery
5. context cap:
   - 47 / 48 / 49 / 80 eligible items
   - recent-first selection
   - deterministic repeated selection
   - anchor/unrecoverable exclusion
   - exception fail-open behavior
6. Python 3.10–3.14
7. Plugin Validate + Doctor
8. self-contained package/release verification

## Bottom line

The most useful result of the campaign was a shift from “supervision means more checks” to **“supervision must use current, task-relative, deterministic evidence before spending another model call.”**

Nerve should be judged on whether it prevents false completion, repeated dead-end work, and misleading progress without introducing new failure modes. Token savings are valuable when they result from that behavior; they are not the primary correctness criterion.
