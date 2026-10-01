# Changelog

## 0.3.1rc2 — final qualification candidate

Final qualification candidate for the stable 0.3.1 maintenance line, superseding `v0.3.1rc1`.

- Fix Nerve settings namespace handling so configuration is read from `plugins.entries.nerve.settings`; warn on the legacy ignored `hermes-nerve` block and preserve omitted profile-script settings instead of overwriting effective values.
- Harden context-engine registration/loading, unknown-mode fallback, stats truthfulness, ledger session attribution, and worker profile secret-scope propagation.
- Re-evaluate machine-checkable completion criteria against current repository state so stale PASS verdicts cannot survive deleted files, regressed tests, or newly dirty Git state.
- Stop deterministic retry loops immediately on policy/permission/schema/path failures while preserving transient retry behavior.
- Make implementation progress task-relative: scratch/repro edits do not count as completion progress, native mutation aliases such as `patch` are recognized consistently, and requested verification must actually pass before completion.
- Detect repeated package/virtualenv/test-environment setup failures and force repository-native recovery instead of continuing environment thrash.
- Make Shared Context opt-in for all named profiles while HermesContextBus lacks a public install source; explicit local-source installation and explicit module enablement remain supported, and `setup --explain` no longer emits a missing-dependency warning when the module is disabled.
- Sanitize historical machine-specific filesystem paths from public planning/evidence material before the final source tag.
- Include the explicitly approved opt-in ask-only Action Gate from #41, hardened by #44 so BLOCK verdicts escalate with action-specific approval keys and registration honors the gate-mode environment fallback. Deep Research Kit (#37), block-review judge (#38), HermesContextBus publication (#40), and other unrelated 0.3.2 feature work remain outside this release.

### Attribution

- **@jcrabapple** — authored #20 (Hermes profile-aware secret resolution), #21 (gate probability telemetry), #22 (dual-signal p(ALLOW) enforce hardening), and #41 (opt-in ask-only Action Gate). #41 ships with the maintainer safety hardening from #44.
- **@colibrishin** — reported #29 and authored the settings/context-engine/stats/ledger hardening series in #30, #31, #32, and #33.
- **@omarabdo516** — reported #35, the multiplexed-gateway ContextVar/secret-scope failure that led to the worker/shadow context propagation fix.
- **@HiroKws** — reported #39 and supplied the follow-up investigation that identified the unpublished HermesContextBus dependency and the correct public-facing setup failure mode.
- **@keeltrace** — maintainer integration, regression campaign reconciliation, completion/retry/progress hardening, release engineering, and final maintenance assembly.

AI-assisted implementation/review was used on parts of the maintenance work and is disclosed in the relevant PRs. Human-facing attribution above follows the public issue/PR authorship record.

Verification gate for the final release: Python 3.10–3.14 CI, release structural verification, Plugin Validate/Doctor, focused completion/retry/progress/profile regressions, package integrity, and exact release-branch review.

## 0.3.0 — profiles, modular runtime, and release hardening

- Add `nerve setup` with Full Configuration, Fat Cat, Operator, Lean, Marie Kondo, Custom, and explicit Legacy behavior.
- Make named profiles control the actual runtime surface: disabled modules omit their tools, hooks, optional provider paths, and context engines where applicable; headless Kanban workers remain schema-minimal.
- Add profile-aware Action Gate defaults, dependency pruning, runtime-disable truthfulness, and reason reporting so resolved capability state matches actual runtime behavior.
- Add Assistant Accountability with durable loop state, bounded audit/review flows, explicit operator-owned enable/disable authority, concurrency fencing, and fail-closed persistence recovery.
- Add optional HermesContextBus v0.2 Shared Context integration with compatibility/health checks, explicit replacement, symlink protections, rollback-safe installation, and coordination-only authority.
- Harden profile persistence and recovery: atomic primary/backup handling, exact rollback/snapshot restoration, stale-backup cleanup, same-profile advanced-setting preservation, and explicit reset semantics.
- Harden profile-home and report-path trust boundaries against nested/vendor/unmarked profile hijacking while preserving legitimate fresh and alternate-root profiles.
- Strip unrelated parent Kanban identity from local remote-runner subprocesses and make remote-execution regression fixtures independent of executable-/tmp sandbox policy.
- Add explicit setuptools package discovery so release wheels contain the intended `hermes_nerve*` and compatibility `hermes_jev*` packages only.
- Final release qualification: 310/310 pytest in clean and hostile Kanban environments, Python 3.10–3.14 GitHub CI, Hermes plugin validate/doctor, 69/69 structured release checks, and 6/6 deliberate regression mutations caught.

## 0.2.3 — provider-contract and gate hardening

- Fix provider cost visibility so missing provider-reported cost remains unknown instead of being silently reported as zero, while preserving explicit zero-cost responses.
- Resolve provider credentials through Hermes profile-aware secret scope so gateway and cron execution can use profile-local keys without borrowing ambient secrets; preserve secret-scope failures instead of masking host scoping bugs.
- Record bounded gate answer distributions (probabilities, top probability, and margin) with finite/range validation for operational tuning.
- Harden enforce-mode ALLOW decisions: automatic continuation now requires both the existing confidence threshold and p(ALLOW) >= 0.90 by default; low ALLOW probability escalates to the existing human-approval path. Missing or unusable distributions retain the shipped confidence-only fallback.
- Repair the deferred nerve_assess contract: instructions is required for every typed question, optional NOUL criteria must be exactly true/false, malformed payloads fail locally before provider work, and valid 16-question batches remain supported.
- Keep issue #19 open for the separately proposed ask-only enforce variant; v0.2.3 includes the telemetry and dual-signal ALLOW enforcement work but does not add that mode.
- OpenJev real 27B inference and hosted-Jev/OpenJev A/B validation remain explicitly untested and tracked in issue #10.
- Make stable GitHub releases immutable and fail-closed: rerunning an existing version is a no-op only on the exact same authoritative remote tag SHA; conflicting release/tag reuse or ambiguous API/tag lookup failures abort instead of deleting, retagging, or trusting stale local state.

Verification for the release candidate includes Python 3.10–3.14 CI, Hermes plugin validate/doctor, release structural verification, focused provider-contract regressions, and exact-SHA independent maintainer review.

## 0.2.2 — stable Nerve/Open-Reflex release

- Promote the validated dev17 RC to stable `0.2.2` after PR #9 release qualification.
- Keep Nerve/Reflex as watchdog + forecaster; the main Hermes orchestrator/reviewer retains final completion/change/block authority.
- Ship hosted Jev plus local/self-hosted Laya and OpenJev Reflex integrations, with Laya live-tested through the SSH transport path.
- OpenJev integration is included and unit/interface tested, but real 27B model inference and hosted-Jev vs OpenJev A/B remain explicitly untested and tracked in issue #10.
- Final RC evidence: 161-test suite green across Python 3.10–3.14, plugin validate/doctor pass, security scan safe, Remote regressions pass, and Jev/Laya live `ORCH_REVIEW` handoff without Nerve kill.


## 0.2.2.dev17 — release hardening + Laya/OpenJev validation

- **RC authority correction from live matrix evidence:** Nerve/Reflex no longer owns economic kill authority. At budget pressure it acts as a watchdog/forecaster and returns the final stop/continue decision to the Hermes LLM orchestrator through canonical `kanban_request_review`.
- Split correctness from spending policy: `DOD-BUDGET` remains visible telemetry when appended, but is non-required and cannot make otherwise-correct work impossible to complete.
- Add one bounded token-extension forecast: at 80% of the base target ask `YES | NO | MAYBE` whether +25% is likely to finish. A confident YES grants one extension; MAYBE reviews immediately; NO gets only a bounded checkpoint window and reviews by 90%; exhausting the granted extension always reviews.
- Retain the former 1.75x hard ceiling only as an emergency **review/pause** fence. No economic condition dispatches `kanban_block`; the orchestrator/reviewer owns COMPLETE / CHANGES / BLOCK.
- Matrix profiles route Nerve budget handoffs to the source orchestrator profile so the release test exercises the intended authority chain.
- Freeze dev16 controller-completion, token-budget estimator, tolerance, and Nerve thresholds as the policy baseline for backend validation.
- Add first-class local `openjev` Reflex backend and provider-neutral Jev-authoritative shadow selection (`laya` or `openjev`).
- Add secure loopback/HTTPS OpenJev client, optional served-identity pinning, `/v1/version` smoke, LOCAL_ONLY provenance, and token redaction.
- Update Laya operator path to the current published `laya==0.3.3` package and standalone `convaiinnovations/laya-typed-decisions` checkpoint while retaining sidecar compatibility with `system_one`/`predict`.
- Fix live A/B validity classification so Hermes `completed` is accepted alongside `done`.
- Replace fragile shell-cleanup/result-finalization behavior with a crash-safe Python model-matrix runner that flushes every arm, reaps lingering workers, preserves external emergency stops separately from Nerve kills, and emits explicit outcome taxonomy plus tail metrics.
- Make offline fake-headless verification quiet/not-applicable without weakening the real worker binding warning.
- Add setup/smoke/profile configuration tooling for Laya and OpenJev and a hosted-Jev/Laya/OpenJev release matrix.
- Document OpenJev's CC BY-NC 4.0 weights separately from its Apache-2.0 helper/serve code; dev17 does not redistribute model weights.


## 0.2.2.dev16 — token-budget DoD + nerve observer / kill switch

- Adds a local automatic budget estimator and locks the resulting target into every auto-bound Kanban Definition of Done as required `DOD-BUDGET`.
- Adds a default 10% deterministic completion tolerance to reduce estimate-boundary false positives while keeping budget authority controller-owned.
- Adds the zero-provider-cost nerve observer with CONTINUE/WATCH/REPLAN/KILL states and state-transition-only directives.
- Adds a conservative hard circuit breaker: default 1.75x target and at least 12 provider calls; crossing the estimate alone never kills a run.
- Adds an earlier corroborated kill only when overrun, repeated identical failures, and sustained high-context calls all agree.
- Adds controller-owned canonical `kanban_block` dispatch for confirmed runaway runs, with durable BLOCK fencing if the native transition fails.
- Prefers controller-owned `kanban_complete` over kill when a late provider call is observed after verified PASS.
- Prevents watchdog state from overwriting an already COMPLETED run.
- Makes auto-budget splitting exact instead of losing rounding tokens.
- Exposes live nerve state through `nerve_work_status`.
- Adds dev16 regression coverage for conservative estimation, false-positive guards, hard-runaway kill, canonical kill dispatch, and post-PASS completion recovery.

## 0.2.2.dev15 — controller-owned completion + Reflex/Laya integration

- Integrates the dev15b provider-neutral Reflex/Laya slice into the finalized dev15 package while keeping Jev as the default authoritative decision backend.
- Converts terminal completion into an explicit controller-owned lifecycle: `VERIFIED -> COMPLETING -> COMPLETED`, with durable `COMPLETION_RETRY` for native transition failures.
- Removes the dev14 post-PASS worker retry loop: when controller dispatch is available, a verified PASS never returns `action=continue` merely because native `kanban_complete` failed. Controller-local retries happen without another worker-model call.
- Converts worker/model completion intent into controller verification + native completion, including direct `kanban_complete`, `hermes kanban ... complete`, generic tool-search wrappers, `CanonicalKanbanAdapter.complete(...)`, generated `complete_task.py`, and direct Kanban lifecycle bypass attempts.
- Keeps worker-originated lifecycle mutation fenced while allowing the hook-owned `PluginContext.dispatch_tool("kanban_complete", ...)` re-entry through an internal ContextVar guard.
- Retries a verified-but-deferred native transition from `on_session_end` instead of re-running semantic completion verification or waking the worker.
- Audits any provider request observed after verified PASS as `completion_worker_call_after_verified`; the dev15 acceptance invariant is zero such calls.
- Adds bounded `work_completion_controller_attempts` (default 3) for controller-local native completion retries.
- Adds the Pair-3 regression suite proving CLI completion wandering is collapsed into one controller-owned terminal transition, including delegated/child-fenced worker conditions and dispatch re-entry.
- Preserves dev14 deterministic DOD-07 authority, dev13 deterministic-over-semantic precedence, dev12 stop-nudge suppression after explicit success, and the single canonical Hermes Kanban task/run authority.
- Includes `jev`, `laya`, and Jev-authoritative `shadow` Reflex modes, the dependency-free Laya client, preloaded sidecar, fail-open paired telemetry, LOCAL_ONLY Laya provenance, model-pin/auth checks, and optional `laya==0.3.5` extra.
- Leaves the Hermes community catalog unchanged.

## 0.2.2.dev14 — controller-owned DOD-07 behavioral proof

- Replace DOD-07 test-name inference with a controller-owned, in-memory behavioral probe that executes the locked retry/dead-letter/idempotency semantics directly against the implementation.
- Reproduce the live dev13 miss: the worker had a green 26-test suite while re-dispatching an already-dead event changed its attempt counter from 2 to 3.
- Return the exact deterministic counterexample in the completion RETRY directive so the worker sees the failing behavior rather than a generic `DOD-07 missing` message.
- Persist the deterministic failure reason in `completion_pre_verify` diagnostics.
- Extend hidden acceptance to require dead-event redispatch stability, no post-dead handler invocation, delivered-event stability, and retry-queue uniqueness.
- Add dev14 regressions proving a green visible suite can still be rejected for the live DOD-07 bug, that the counterexample is surfaced verbatim, and that the repaired short-circuit reaches harness-owned native completion with zero semantic completion calls.
- Preserve dev13 deterministic authority, controller-authored DOD-08 summary, dev12 stop-nudge suppression, native completion, and lifecycle bypass fences.

## 0.2.2.dev13 — deterministic completion authority

- Make controller-observed deterministic verdicts authoritative for machine-verifiable Definition-of-Done criteria; semantic completion verification can no longer overwrite a deterministic FAIL.
- Reproduce the live dev12 false-negative where correct retry/dead-letter/idempotency evidence still left DOD-07/DOD-08 unresolved and drove a 67-call completion-discovery spiral.
- Broaden DOD-07 focused-test recognition to equivalent behavioral names such as retry-then-success stability, never-redeliver, and dead short-circuit/no-reattempt.
- Remove dev12's extra retry-queue clause from DOD-07 when that clause is not actually present in the locked criterion.
- Make DOD-08 harness-owned: synthesize the canonical final completion summary from authoritative verdicts, including every DOD label and the exact full-suite command/result, instead of relying on model prose.
- Reuse the already-observed DOD-01 full-suite evidence when formatting the canonical summary, avoiding redundant suite executions.
- Add dev13 regressions proving the exact dev12 evidence shape completes with zero semantic completion calls and that deterministic failure cannot be semantically overridden.
- Preserve dev12 zero-turn native completion, stop-nudge suppression, session-end skip, terminal authority fences, and fail-closed behavior.

## 0.2.2.dev12 — zero-turn terminal unwind

- Reproduce the dev11 live release smoke: the frozen fixture reached 24/24 tests, `completion_pre_verify allow=true`, and `completion_native_dispatch ok=true`, but Hermes emitted one post-PASS provider turn because its kanban stop guard could not see the hook-owned native completion in conversation message history.
- After an explicit successful native `kanban_complete` dispatch, set the worker-local `HERMES_KANBAN_STOP_NUDGE=0` marker so Hermes' text-stop guard does not synthesize a redundant completion nudge.
- Suppression is armed only after explicit native success. Ambiguous/error dispatches remain fail-closed with `COMPLETE_READY` active and the native terminal path still required.
- Record `completion_stop_nudge_suppressed` diagnostics for live release forensics.
- Preserve dev11 deterministic completion evidence, authority fences, harness-owned native dispatch, and session-end skip behavior.

## 0.2.2.dev11 — harness-owned verified completion

- Reproduce the dev10 forensic failure: the worker reached 24/24 passing tests and machine-checkable 8/8 DoD evidence, but Jev still ended on `allow=false / ESCALATE`; the model then bypassed lifecycle authority with a worker-authored SQLite mutation and accumulated 80 worker API calls / 174 tool calls before exit.
- Use Hermes' supported `PluginContext.dispatch_tool()` from the `pre_verify` hook. A PASS now arms `COMPLETE_READY` and immediately dispatches the native `kanban_complete` tool with the owning worker's task/run/claim context; no extra model turn is required.
- Keep `COMPLETE_READY` durable before native dispatch so a native completion error leaves only the directly-listed `kanban_complete` exit available; repeated pre-verify attempts retry native dispatch without another Jev verification call.
- Fail closed on every non-PASS completion verdict, including `ESCALATE`; completion cannot be converted into a human-approval escape path.
- Add a worker authority fence that blocks terminal/CLI lifecycle mutation and worker-authored direct Kanban SQLite completion scripts while preserving the registered native Kanban tools.
- Expand deterministic completion evidence: regression-test counts, public Python signature parity against the frozen Git baseline, focused retry/dead-letter idempotency test clauses, and final-summary command/result/per-DoD evidence are machine-verified before semantic fallback.
- Skip session-end semantic re-verification after a terminal-ready PASS, preventing an already-verified run from spending another completion-batch call while unwinding.
- Add frozen-fixture integration coverage proving all eight benchmark DoD criteria reach `VERIFIED_PASS` deterministically and trigger one harness-owned native completion dispatch with zero semantic completion calls.

## 0.2.2.dev10 — terminal-ready fence after verified completion

- Reproduce the dev9 live lifecycle failure: all visible and hidden acceptance gates pass, completion is independently verified, yet the worker remains running and accumulates post-DoD Solar calls because the model misroutes `kanban_complete` through generic tool wrappers/searches and falls back toward direct DB mutation.
- Convert a passing dispatcher-owned `pre_verify` verdict into a durable `COMPLETE_READY` run control instead of returning silently to a free-form worker loop.
- While `COMPLETE_READY` is active, block every non-`kanban_complete` tool call with an explicit instruction to invoke the directly-listed native `kanban_complete` tool; this fences generic `tool_call`, `tool_search`, terminal/shell, file writes, and direct Kanban DB workarounds.
- Repeated finish attempts reuse the durable PASS latch and do not spend another Jev semantic-completion call.
- A direct `kanban_complete` remains the sole allowed terminal exit and is handed back to Hermes' native implementation; Hermes Kanban remains the sole canonical lifecycle authority.
- Preserve dev9 provider prompts, routing thresholds, completion batching, token accounting, child completion fencing, and zero model-visible Jev tools unchanged.

## 0.2.2.dev9 — fence completion authority and repair terminal handoff

- Fence semantic `pre_verify`, session-end completion auditing, and `kanban_complete` authorization to the dispatcher-owned Kanban worker. Delegate-task children may inherit Kanban environment variables, but they cannot spend Jev completion calls or mutate the parent's supervisory completion state.
- Evaluate a fresh locked-DoD completion verdict before applying outstanding trajectory control to `kanban_complete`. A stale WATCH/REPLAN/BLOCK can no longer prevent current completion evidence from being considered forever.
- When completion is verified, acknowledge older run control and return authority to Hermes' native `kanban_complete`; canonical task/run state remains exclusively owned by Hermes Kanban.
- Prefer canonical `HERMES_KANBAN_TASK_ID` / `HERMES_KANBAN_TASK` over hook-local task/session identifiers during fallback auto-binding, eliminating the observed `autobind_task_missing` identity noise.
- Add regression coverage for delegated-child completion fencing, child session-end isolation, child terminal denial, fresh completion superseding stale REPLAN, ordinary-tool control enforcement, and canonical fallback binding.
- Preserve dev8 routing thresholds, provider prompts, completion batching, token accounting, headless zero-tool worker surface, and A/B-tuned supervision behavior unchanged.

## 0.2.2.dev8 — activate headless Jev decisions and completion verification

- Fix the production decision-router bug proven by the Solar Pro 4 dev7 run: `SupervisionStore.events()` returns parsed event payloads under `row["payload"]`, while dev7's router looked for the removed `payload_json` field, so every failure fingerprint list was empty and repeated failures could never trigger Jev.
- Preserve failure memory across normal work: select the last N `TEST_FAILED` events rather than the last N general work events.
- Record per-test failure fingerprints for the complete failing-test set, so a persistent test can be recognized even as pytest failure order/cardinality changes.
- Mark an exact locked deterministic test criterion `FAIL` when its command fails, allowing the 40% checkpoint to see real negative progress instead of a falsely healthy UNKNOWN state.
- Wire Hermes' native `pre_verify` hook to the deterministic-first completion verifier and batched Jev semantic completion decision.
- Add `on_session_end` as a last-resort completion audit for lifecycle paths that bypass `pre_verify`.
- Enforce a cooldown after a trajectory call so repeated pytest failures do not cause one Jev spend per test run. Blocker/plan-change triggers remain urgent.
- Retain dev7's deterministic startup binding, exact run/claim fencing, package-relative imports, and zero model-visible Jev tools for headless workers.
- Add regression coverage reproducing the exact dev7 failure mode: interleaved events, changing pytest failure sets, checkpoint FAIL state, real trajectory provider invocation, cooldown suppression, semantic pre-verify provider invocation, and session-end completion fallback.

## 0.2.2.dev7 — deterministic headless Kanban binding

- Fix the production bootstrap failure discovered by the Solar Pro 4 A/B run: Hermes v0.21.3 moved DB connection helpers to `hermes_cli.kanban_db_connect`, while dev6 called the removed `kanban_db.connect()` symbol.
- Prefer the dispatcher-pinned `HERMES_KANBAN_DB` / `HERMES_KANBAN_BOARD` when reading the canonical task so another controller's selected board cannot redirect supervision.
- Bind the locked DoD and exact run locally during plugin startup, before the worker's first provider call; later hooks only refresh run/session context.
- Validate canonical `current_run_id` and `claim_lock` against dispatcher env before accepting a binding.
- Keep headless workers at zero model-visible `jev_*` schemas.
- Include the dev6 runtime relative-import hotfix in the packaged source.
- Add a split-Kanban compatibility regression test and startup-binding integration test.

## 0.2.2.dev6 — headless token-saving Kanban supervision

Dev6 is the A/B-tuned follow-up to dev5. It changes supervised Kanban from a worker-visible Jev toolkit into a headless decision coprocessor.

- Ordinary Kanban workers register **zero Jev tools**; controller/admin sessions retain all 16 public Jev tools.
- Work supervision is enabled by default for Jev-enabled profiles and defaults to advisory mode.
- A structured `## Definition of Done` is automatically compiled, locked, budgeted, and bound to the exact Kanban run before useful work begins.
- Budget accounting now runs per provider API request (`post_api_request`) and records input/output/reasoning/cache tokens idempotently by request ID.
- Default budget checkpoints are sparse: 40% and 70%.
- Deterministic evidence gets first refusal; safe tests/files/git invariants can reach VERIFIED_PASS without a Jev provider call.
- Completion batches any remaining semantic criteria into one Jev verification instead of one call per criterion plus another terminal call.
- A local ROI router suppresses Jev calls on healthy/low-signal work and enforces a supervisor token allowance.
- Repeated equivalent pytest failures use stable test-identity fingerprints and can trigger one hidden trajectory assessment.
- A live trajectory assessment asks Jev for both trajectory (`CONTINUE/WATCH/REPLAN/BLOCK`) and bounded next action in one System One call.
- High-confidence guidance is injected once, compactly, into the next tool result; the worker does not spend a separate turn deciding whether to ask Jev.
- Jev provider tokens are recorded separately from worker tokens, and status includes combined/economic telemetry.
- Exact-run fencing, canonical Kanban review/replan, remote execution, Git result refs, and the single-authority invariant are preserved.

## 0.2.2.dev5 — evidence-backed distributed work supervision

- Add immutable/versioned Definition of Done contracts with Jev preflight review.
- Add run-scoped work events, controller-observed evidence, independent criterion verification, deterministic progress/frontier projection, and terminal completion verification.
- Carry verified facts across successor runs while the locked DoD hash is unchanged.
- Add token allocations, explicit usage provenance, one-shot budget checkpoints, trajectory predictions, calibration telemetry, and shadow/advisory/enforce control modes.
- Add safe checkpoint packets and canonical Kanban review handoff for WATCH/REPLAN/BLOCK controls.
- Add durable admin-aliased SSH Hermes execution based on the Outpost security model.
- Preserve canonical run/claim identity end to end and fence stale remote workers/results.
- Preserve exit code 75 rate-limit semantics so canonical Kanban can requeue without charging a failure.
- Add remote run-control files over a second ordinary SSH invocation; no daemon or reverse tunnel.
- Add Git-native exact-HEAD + dirty/untracked workspace transport and result refs without modifying the controller checkout.
- Preserve the existing Jev typed decision, context, provenance, nervous-system, and gate surfaces in the unified plugin.
- Add North-Star recovery, Git isolation, remote execution, registration, trajectory, and supervision regression suites.

## 0.2.2.dev4

Upstream fixed point: paid OpenCode Zen provider plus runtime/repository hardening. See source provenance for the exact commit used as the dev5 baseline.
