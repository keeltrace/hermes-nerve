# Nerve development history

These sections were moved verbatim from the README on 2026-10-26 so the
README can lead with user-facing documentation. Content is unchanged; links
point at their original docs.

## v0.3.2.1 — Selective Context

Nerve is an asynchronous System-1 supervisory layer for Hermes Agent. `0.3.2.1` adds **Selective Context** on top of the `0.3.2` Verified Experience runtime: evidence-backed typed claims, negative knowledge, successor-run capsules, outcome-linked learning, recovery playbooks, deterministic utility retrieval, explainability, and advisory offline reconciliation. Nerve remembers verified experience rather than conversations. Hermes remains the authority for canonical task state and policy; the core plugin remains dependency-free and model runtimes stay in sidecars.

Nerve can now plan the smallest useful working set across memory, tools, and skills without becoming the owner of those resources. The deterministic path is provider-free, mandatory context is pinned, irrelevant memory can be suppressed for a turn, and unselected tools/skills are deferred rather than deleted. Stable task leases reduce context churn. All three surfaces default to shadow mode.

The release preserves the existing Nerve tool surface and Hermes execution authority. See [RELEASE_0.3.2.1.md](RELEASE_0.3.2.1.md).

## v0.3.2 — Verified Experience

Nerve now promotes independently observed work evidence into reusable, scoped experience instead of retaining conversations. It adds typed verified/disproven claims, claim supersession/conflict handling, temporal validity, token-aware admission, utility-ranked retrieval with `why_retrieved`, successor-run capsules built from existing checkpoints, failure-fingerprint recovery playbooks, outcome attribution, hierarchical summaries, a bounded evidence graph, provider-free `nerve_context_explain`, and advisory `nerve experience reconcile`.

Legacy remains the 16-tool compatibility surface; Lean and Marie Kondo gain no new tool-schema tax; their existing work-supervision path can preserve bounded local capsules; headless Kanban workers still expose zero Nerve tools. See [RELEASE_0.3.2.md](RELEASE_0.3.2.md).

## Development — generic System One + Liquid D1

The post-0.3.2.1 development line adds a dependency-free generic TypeSafe-compatible /v1/systemone HTTP provider and a LiquidAI/d1-3B preset. D1 can run as an opt-in primary or, preferably first, as a fail-open shadow behind authoritative Jev. The same provider seam supports image data URLs through optional DecisionEngine images input. Hermes retains final stop/continue and canonical Kanban authority. See D1_SYSTEMONE_INTEGRATION.md.

## Dev17 — open backend release matrix

Dev17 adds first-class `reflex_backend=openjev`, generalizes Jev-authoritative shadowing to either Laya or OpenJev, updates the Laya sidecar to the current standalone typed-decisions checkpoint, and packages a crash-safe Jev/Laya/OpenJev matrix runner. It also carries forward the live dev16 benchmark fixes: `completed` is a successful terminal state, results flush after every arm, lingering workers are reaped, external emergency stops remain distinct from Nerve orchestrator-review handoffs, and final summaries survive cleanup errors. See [DEV17_OPEN_SOURCE_VALIDATION.md](DEV17_OPEN_SOURCE_VALIDATION.md).

## Dev15b — Reflex + Laya integration

Dev15b introduces a provider-neutral backend seam below `DecisionEngine` without changing the existing Kanban authority model. `reflex_backend=jev` preserves current behavior, `reflex_backend=shadow` keeps Jev authoritative while logging paired Laya decisions, and `reflex_backend=laya` selects the local Laya sidecar for semantic decisions. Laya receipts are correctly marked `LOCAL_ONLY`, and shadow failures never change the authoritative result.

The plugin does not import torch or transformers. The optional Laya runtime lives in a separate process started with `python -m hermes_nerve.reflex.laya_service`, preloading `convaiinnovations/laya` / `typed-decisions` once. See [DEV15B_LAYA_INTEGRATION.md](DEV15B_LAYA_INTEGRATION.md) for installation, SSH tunneling, configuration, telemetry, and live-acceptance gates.

## Dev14 controller-owned DOD-07 proof

Dev14 closes the live dev13 gap without weakening the deterministic completion gate. In the dev13 smoke, the worker reached a clean commit and a green 26-test visible suite, but direct repeated dispatch of an already-dead event still mutated attempts from 2 to 3. Dev13 correctly held DOD-07 at deterministic FAIL, but its evidence path still depended on worker-authored focused-test names and did not surface the concrete counterexample early enough.

Dev14 executes the DOD-07 semantics itself inside the controller. The probe is in-memory, network-free, and does not modify the workspace. It proves: dead-event redispatch is stable, delivered-event redispatch does not redeliver, transient retry→success→duplicate is stable, and retry-queue work remains unique. When a probe fails, the exact observation is placed in the RETRY directive (for example, `dead-event redispatch changed attempts 2->3`).

The hidden acceptance test now enforces the same dead-event redispatch invariant. All dev13 completion authority remains intact: deterministic FAIL cannot be semantically overridden, DOD-08 is controller-authored, fully machine-verifiable completion uses zero semantic completion calls, and successful PASS flows through native `kanban_complete` with stop-nudge suppression and session-end skip.

## Dev11 verified-completion path

Dev11 closes the dev10 production failure at the authority boundary instead of asking the model to discover its own exit path:

```text
worker satisfies locked DoD
  -> deterministic evidence proves mechanical criteria locally
  -> Jev judges only unresolved semantic criteria
  -> completion PASS
  -> plugin arms durable COMPLETE_READY
  -> hook dispatches registered native kanban_complete in-process
  -> canonical Kanban card/run closes
  -> worker unwinds with zero post-PASS model/tool discovery required
```

Completion now fails closed. A non-PASS verdict leaves the card running and returns the exact missing criteria. Generic lifecycle wrappers, shell/CLI completion, worker-authored Kanban SQLite mutators, and direct task-status SQL are blocked from supervised workers. The registered `kanban_*` tools remain the only canonical mutation surface.

For the frozen event-delivery benchmark, dev11 also machine-verifies public signature parity against the frozen Git baseline, regression-test count, focused retry/dead-letter idempotency evidence, and the final per-DoD command/result summary. This removes the dev10 false-negative completion batch that kept reporting DOD-06/07/08 missing after the implementation was already proven.

## Dev9 terminal-handoff fixes

Dev9 is a minimal lifecycle hardening release built from the measured dev8 A/B. It preserves dev8 decision policy and provider economics while fixing the post-solution failure that left a correct treatment worker running until protocol violation:

- only the dispatcher-owned Kanban worker may run Jev completion verification or completion auditing; delegated child contexts are fenced from parent completion state and provider spend;
- `kanban_complete` gets a fresh locked-DoD completion verdict before older trajectory control is consulted, so stale WATCH/REPLAN state cannot deadlock an already-complete run;
- a passing completion verdict acknowledges older run control and hands authority back to Hermes' native `kanban_complete`; Jev never owns canonical task state;
- fallback auto-binding prefers canonical dispatcher `HERMES_KANBAN_TASK[_ID]` over hook-local/session identifiers;
- dev8's router activation, deterministic verification, bounded completion batches, cooldown, headless startup binding, and zero worker-visible Jev tools are unchanged.

## Dev17-era install and verification notes

From the extracted final package:

```bash
cd nerve-v0.3.0
bash scripts/install_dev17_profile.sh abtest-jev-dev17
```

The installer preserves an existing `hermes-nerve` directory as a timestamped backup, installs this package into the selected profile, and enables the plugin.

Dev17 keeps economical headless supervision and auto-estimates a task token target, records it as non-blocking `DOD-BUDGET` telemetry, and observes token trajectory after every provider call. Budget pressure cannot invalidate correct work: Nerve may grant one bounded extension after a YES forecast or hand the run to canonical review, but only the orchestrator/reviewer may decide to stop/block it. The 70k setting remains a floor rather than the typical final estimate. Controller/admin sessions still expose the public Jev tools; dispatcher-spawned Kanban workers do not.

Verify the package before a live run:

```bash
bash scripts/verify_dev17.sh
```

For the controller-completion and current budget-authority regressions specifically:

```bash
python3 -m pytest -q tests/test_dev15_controller_completion.py tests/test_dev16_nerve_budget.py tests/test_dev17_matrix_qol.py
```

See [DEV16_INSTALL_AND_RETEST.md](DEV16_INSTALL_AND_RETEST.md) for nerve thresholds, live acceptance, completion diagnostics, and the next A/B protocol; dev15 documentation remains for lifecycle history.

Remote machines used as supervised Hermes workers should run a compatible dev15 build so run identity/checkpoint and controller-owned completion semantics match the controller.

## Development provenance

The dev5 foundation started from the last publicly verifiable Nerve development fixed point, `0.2.2.dev4` commit `a3aeedc0006797c244ef29d7e085616a5253e627`, and incorporates behavior derived from:

- Hermes Outpost `9764b4fd0fb7f7923b8c5796b0e17036046858f0`
- Hermes Kanban Labs `acf73737673c6639ac59991d61e349456738b132`
- current Hermes Kanban lifecycle APIs, while keeping Kanban as the sole canonical work-state authority.

See `DEV_0.2.2.md` and `../SOURCE_PROVENANCE.md`.

The North-Star regression deliberately proves: locked DoD → partial verified progress → bad remote trajectory → Jev REPLAN → checkpoint/review handoff → successor run resumes inherited verified facts → remaining criterion verified → terminal completion PASS → 100% verified.
