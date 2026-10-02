# Nerve

> **Development status:** current `main` identifies as `0.3.2.dev0`. The latest stable release is `v0.3.1`, published from its reviewed release commit. `main` contains post-0.3.1 development work and is not the same tree as the stable release. Hermes catalog installs follow exact reviewed commit-SHA pins and may lag `main` until a catalog SHA-bump PR is reviewed and merged.

Nerve is an asynchronous System-1 supervisory layer for Hermes Agent. `0.3.1` is the stable maintenance release for the profile-aware `0.3.0` line: it concentrates bug fixes, hardening, completion/retry integrity, configuration correctness, setup QOL, and the explicitly approved opt-in ask-only Action Gate while preserving v0.2.3-compatible Legacy behavior when no profile is selected. Other `0.3.2` feature work remains on the development line. Nerve/Reflex remains a watchdog and forecaster; the main Hermes orchestrator/reviewer owns final stop/continue authority. The core plugin remains dependency-free and model runtimes stay in sidecars.


## Dev17 — open backend release matrix

Dev17 adds first-class `reflex_backend=openjev`, generalizes Jev-authoritative shadowing to either Laya or OpenJev, updates the Laya sidecar to the current standalone typed-decisions checkpoint, and packages a crash-safe Jev/Laya/OpenJev matrix runner. It also carries forward the live dev16 benchmark fixes: `completed` is a successful terminal state, results flush after every arm, lingering workers are reaped, external emergency stops remain distinct from Nerve orchestrator-review handoffs, and final summaries survive cleanup errors. See [`docs/DEV17_OPEN_SOURCE_VALIDATION.md`](docs/DEV17_OPEN_SOURCE_VALIDATION.md).

## Dev15b — Reflex + Laya integration

Dev15b introduces a provider-neutral backend seam below `DecisionEngine` without changing the existing Kanban authority model. `reflex_backend=jev` preserves current behavior, `reflex_backend=shadow` keeps Jev authoritative while logging paired Laya decisions, and `reflex_backend=laya` selects the local Laya sidecar for semantic decisions. Laya receipts are correctly marked `LOCAL_ONLY`, and shadow failures never change the authoritative result.

The plugin does not import torch or transformers. The optional Laya runtime lives in a separate process started with `python -m hermes_nerve.reflex.laya_service`, preloading `convaiinnovations/laya` / `typed-decisions` once. See [`docs/DEV15B_LAYA_INTEGRATION.md`](docs/DEV15B_LAYA_INTEGRATION.md) for installation, SSH tunneling, configuration, telemetry, and live-acceptance gates.

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

## Dev9 token-saving Kanban path

For a Kanban worker with Nerve enabled:

```text
card claim
  -> auto-bind locked DoD + 70k default budget
  -> worker starts with ZERO Jev tool schemas
  -> post_api_request records worker input/output/reasoning tokens
  -> post_tool_call records evidence and stable failure fingerprints
  -> deterministic verifiers prove mechanical criteria locally
  -> local ROI router stays silent on healthy work
  -> repeated failure / unhealthy 40% or 70% checkpoint may trigger ONE Jev call
  -> Jev returns trajectory + bounded next action
  -> high-confidence directive is appended once to the next tool result
  -> worker executes without a separate ask-Jev turn
  -> completion proves deterministic remainder locally and batches semantic remainder
```

Design invariant: **Jev must earn every token it spends.** `nerve_work_status` exposes worker tokens, supervisor tokens, combined tokens, estimated avoided tokens, and estimated net savings for controller/debug sessions.

The permanent healthy-run benchmark target is <=2% fixed overhead versus plain Hermes, with <=0.5% as the stretch target. A second benchmark class measures savings when a plausible wrong path causes repeated failures/replanning.



## Dev9 terminal-handoff fixes

Dev9 is a minimal lifecycle hardening release built from the measured dev8 A/B. It preserves dev8 decision policy and provider economics while fixing the post-solution failure that left a correct treatment worker running until protocol violation:

- only the dispatcher-owned Kanban worker may run Jev completion verification or completion auditing; delegated child contexts are fenced from parent completion state and provider spend;
- `kanban_complete` gets a fresh locked-DoD completion verdict before older trajectory control is consulted, so stale WATCH/REPLAN state cannot deadlock an already-complete run;
- a passing completion verdict acknowledges older run control and hands authority back to Hermes' native `kanban_complete`; Jev never owns canonical task state;
- fallback auto-binding prefers canonical dispatcher `HERMES_KANBAN_TASK[_ID]` over hook-local/session identifiers;
- dev8's router activation, deterministic verification, bounded completion batches, cooldown, headless startup binding, and zero worker-visible Jev tools are unchanged.

## Distributed-supervision foundation retained from dev5

- **Locked Definition of Done contracts** for canonical Hermes Kanban cards.
- **Evidence-backed progress**: worker claims, controller observations, and Jev verification are separate states. Only `VERIFIED_PASS` counts as verified progress.
- **Deterministic frontier tracking** across DoD dependencies.
- **Independent completion gate**: `kanban_complete` is treated as a proposal for supervised cards until every required criterion is verified. Mechanical criteria are proved locally; Jev is used only for the remaining semantic judgment when one exists.
- **Token-budget trajectory supervision** with configurable checkpoints and `CONTINUE | WATCH | REPLAN | BLOCK` decisions.
- **Safe mid-task correction**: advisory/enforced controls block new implementation work, permit checkpoint actions, preserve evidence/artifacts, and route the exact canonical run to `review` for orchestrator replanning.
- **Durable SSH Hermes workers** with admin-defined aliases, workspace/profile allowlists, stdin-only task transport, cancellation, timeouts, rate-limit exit 75 preservation, and exact-run fencing.
- **Git-native remote execution**: exact HEAD + dirty/untracked overlay, per-run remote repo, returned result ref under `refs/hermes-kanban-labs/results/*`, and a hard invariant that the controller checkout is never switched/reset/merged by the transport.
- **Successor-run resume**: verified criterion verdicts survive retry/replan while the locked DoD hash is unchanged, so replacement workers resume at the unresolved frontier instead of redoing proven work.

## Authority model

```text
Hermes Kanban             canonical cards / dependencies / runs / claims / review / done
      |
      +-- exact task_id + run_id + claim_lock
      |
Nerve CardSupervisor DoD / evidence / verified progress / budgets / Jev decisions
      |
      +-- local Hermes worker
      +-- SSH Hermes worker
```

Nerve deliberately does **not** own canonical task status, dependencies, retries, queues, or completion state. Those remain in Hermes Kanban.

## Install

From the extracted final package:

```bash
cd nerve-v0.3.1
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

See `docs/DEV16_INSTALL_AND_RETEST.md` for nerve thresholds, live acceptance, completion diagnostics, and the next A/B protocol; dev15 documentation remains for lifecycle history.

Remote machines used as supervised Hermes workers should run a compatible dev15 build so run identity/checkpoint and controller-owned completion semantics match the controller.

### Remote host example

Configure `remote_hosts` through the Hermes plugin configuration mechanism as a mapping. Each host is an admin-defined alias; callers cannot inject arbitrary SSH options. A host can constrain `workspace_root`, `profile` / `allowed_profiles`, `max_turns`, timeout, model/provider/toolsets, and optional `git_cache_root` / `control_root`.

Remote task text is sent over stdin. Controller provider credentials are not forwarded through SSH.

## New tools

- `nerve_supervise_card` — bind/amend DoD, bind run, status, verify criteria, record budget, assess trajectory, checkpoint, label outcomes, request canonical review.
- `nerve_work_event` — worker progress/checkpoint events.
- `nerve_work_status` — compact progress/frontier/control view.
- `nerve_remote_delegate_task` — durable SSH delegation.
- `nerve_remote_worker_status`
- `nerve_remote_worker_result`
- `nerve_remote_worker_cancel`
- `nerve_remote_worker_control`

The original eight dev4 Jev tools remain registered.

## Verification
The canonical release gate is `bash scripts/verify_dev17.sh`; it runs the release verifiers and every `tests/test_*.py` file under pytest. `unittest discover` is not a complete substitute because pytest-style module functions are present.

```bash
bash scripts/verify_dev17.sh
python3 scripts/verify_dev6_benchmark.py
python3 -m compileall -q .
python3 scripts/verify_release.py
```

The North-Star regression deliberately proves: locked DoD → partial verified progress → bad remote trajectory → Jev REPLAN → checkpoint/review handoff → successor run resumes inherited verified facts → remaining criterion verified → terminal completion PASS → 100% verified.

## Development provenance

The dev5 foundation started from the last publicly verifiable Nerve development fixed point, `0.2.2.dev4` commit `a3aeedc0006797c244ef29d7e085616a5253e627`, and incorporates behavior derived from:

- Hermes Outpost `9764b4fd0fb7f7923b8c5796b0e17036046858f0`
- Hermes Kanban Labs `acf73737673c6639ac59991d61e349456738b132`
- current Hermes Kanban lifecycle APIs, while keeping Kanban as the sole canonical work-state authority.

See `docs/DEV_0.2.2.md` and `SOURCE_PROVENANCE.md`.

## Nerve setup and personalities

Nerve can now resolve a small set of user-facing modules instead of exposing every subsystem to every Hermes session. Existing installations remain in **Legacy** mode until a profile is explicitly selected, so upgrading does not silently change v0.2.3 behavior.

Run:

```bash
nerve setup
# or
python -m hermes_nerve setup
```

The first menu is intentionally compact:

```text
1. Full Configuration - Pick every Nerve module and advanced option yourself.
2. Fat Cat            - Max assistant quality; spend more tokens for capability.
3. Operator           - Direct Hermes power use: tools, coding, agents, context.
4. Lean               - Factory-first nervous system with token-heavy extras off.
5. Marie Kondo        - Minimum Nerve: only features that clearly earn their cost.
```

Profile state is persisted atomically at `$HERMES_HOME/nerve/profile.json`. Full Configuration includes a manifest-driven advanced editor for typed sidecar overrides; re-selecting the same profile preserves those overrides, while `--reset` clears them deliberately. Advanced Hermes plugin settings can still also be supplied through the existing Hermes configuration surface. A missing profile sidecar and missing `nerve_profile` setting enter a dedicated v0.2.3 Legacy registration path. The exact 16-tool/9-hook/context-engine/headless behavior is regression-pinned, including the v0.2.3 `nerve_auto_kill` compatibility fallback and pre-LLM first-result semantics. `nerve_auto_kill` is compatibility state only; economic stop authority remains with the orchestrator/reviewer.

### Hard-OFF modules

For selected profiles, disabled modules are omitted from the Hermes model/runtime surface where practical: their tool schemas are not registered, their hooks are not installed, and their optional context engines/provider paths are not activated. Headless Kanban workers continue to expose zero Nerve tool schemas.

### Shared Context

Shared Context is an optional external integration. Nerve does **not** vendor HermesContextBus. Because HermesContextBus is not currently published through a public install source, Shared Context is OFF by default in all named profiles. Users with an explicit local HermesContextBus checkout can opt in and install it manually.

To inspect integration availability:

```bash
nerve setup --explain
```

To install from an explicit local HermesContextBus source checkout:

```bash
nerve setup --install-shared-context /path/to/HermesContextBus
```

Shared-context messages are coordination data, never tool authority or user permission.

### Assistant Accountability

Fat Cat enables optional persistent Assistant loops plus a per-turn accountability audit. Loops carry a next move, owner, dependencies, trigger/deadline, and Definition of Done. Completion is review-gated; generic updates cannot mark a loop done.

Assistant loop content is treated as untrusted coordination data. It cannot create standing permission. The audit is independently switchable so durable loops can be used without recurring provider calls. Profile selection never writes the persistent Assistant operator override; only the explicit Assistant install/disable CLI actions may do that.

See `planning/nerve-setup-profiles-factory-audit/IMPLEMENTATION_PLAN.md`, `FACTORY_AUDIT.md`, and `PROFILE_EVIDENCE.md` for design and evidence.
