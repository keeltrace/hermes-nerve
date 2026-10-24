## Unreleased — Selective Context activation semantics

- Enabling the parent Selective Context module now means enabled memory, tool
  and skill surfaces default to **apply**. Explicit shadow/off settings remain.
  Legacy and disabled modules preserve unmodified Hermes context.
- The Nerve consumer now honors the resolved profile sidecar, Hermes settings,
  advanced overrides and module dependency pruning instead of incorrectly
  reading only raw plugin settings.
- A new structured-memory hook offers evidence-backed verified/disproven
  experience from the active Hermes profile, with user-scoped retrieval and
  Hermes-side home/session/user validation. Existing unscoped experience
  remains profile-scoped.
- Compatible Hermes host adapters can now apply tool HOT hints and skill index
  description demotion by default. Without those host adapters, Nerve does not
  claim that it has changed model-visible tool or skill context.
- Added focused regression tests for on/apply, explicit shadow, disabled modules,
  tool/skill selection, and verified-memory scope isolation.
- This is an unmerged candidate change, not a claim of native-install or
  full-release qualification.

## 0.4.14 — release sanitization

- Observatory roots (`fleet_monitor`, `server`, `session_index`) now derive
  from `HERMES_HOME`/user home instead of hardcoded absolute paths.
- Internal planning material and the stale native-source snapshot excluded
  from the public tree. No behavior change; fleet deployments remain on
  0.4.13 (byte-identical runtime behavior).

## 0.4.13 — sidecar authority, save-path, CLI/panel parity

- The Nerve profile sidecar (`nerve/profile.json`) is authoritative when
  present; a stale `nerve_profile` pin in Hermes `config.yaml` is surfaced as
  an explicit conflict (panel + CLI) instead of silently overriding the saved
  profile. Shared host-settings loader (`host_settings.py`) gives the CLI and
  the control panel one identical view of Hermes `config.yaml`.
- Control-panel saves no longer blocked by a conflicting config pin; module
  cards show the saved choice plus the true runtime reason for the effective
  state. 14 new regression tests (sidecar authority, conflict surfacing,
  panel save/preview). Full suite at release: 1,351 passed, 0 failed,
  6 skipped.

## 0.4.12 — unified profile/config repair

- Single resolver (`config_resolver.py`) for sidecar + Hermes config.yaml used
  by CLI, panel, and runtime; server-driven control panel with cache-busted
  web assets. Maintainer-side work; external contributions for the underlying
  profile/UI surface remain credited in the section below.

## Unreleased — 0.4.11 candidate: external contributions

Native-admission repair (policy data as auditable plaintext tables): the protected system locations, disk-destroyer command names, and unbounded-executable command names moved from inline Python literals into `hermes_nerve/policy/*.txt` loaded once at import by `hermes_nerve/policy_tables.py` (missing/empty/duplicate tables fail the import loudly). Every protective entry is preserved exactly — pinned byte-set by `tests/test_policy_tables.py` against the frozen pre-refactor content — with decision equivalence proven over a 280-record probe. Completion-integrity hardening: a leading BLOCKED/PARTIAL/FAILED status label in a model-authored summary can never satisfy the legacy completion-summary gate (`tests/test_blocked_handoff_completion_gate.py`), keeping truthful blocked handoffs unverified-but-expressible.

Eight external PRs have been integrated locally, with original authors and upstream provenance retained:

- **Phuong Lambert (@phuongvm)** — [#63](https://github.com/keeltrace/hermes-nerve/pull/63): filter URLs and numeric metrics from requested paths; [#64](https://github.com/keeltrace/hermes-nerve/pull/64): bypass mutation gates for read-only introspection, not writes.
- **@rharmanca** — [#67](https://github.com/keeltrace/hermes-nerve/pull/67): observe-only shadow completion; [#68](https://github.com/keeltrace/hermes-nerve/pull/68): prevent hook-local task IDs from polluting process environment; [#66](https://github.com/keeltrace/hermes-nerve/pull/66): structured terminal test results, empty-test rejection and V4A paths; [#72](https://github.com/keeltrace/hermes-nerve/pull/72): stacked launcher recognition, reconciled after #66 rather than double-applied; [#69](https://github.com/keeltrace/hermes-nerve/pull/69): TypeSafe-only provider selection and OpenRouter 402 guidance; [#71](https://github.com/keeltrace/hermes-nerve/pull/71): deterministic admission supervision floor, including provider-failure and obfuscation cases.

**Configuration change (#69):** the `jev_provider` schema default is now empty/unset rather than `openrouter`. Explicit settings retain precedence; TypeSafe is auto-selected when it is the sole configured key. Both keys retain the OpenRouter default. Set the provider explicitly if routing must not depend on available keys. Current 1.14 model defaults are preserved.

Integration also repairs launcher/quoted-code false positives and the HTTPError transport path while preserving existing sensitive-search, queue-bound, policy-memory and strict result-envelope protections. See [the integration ledger](docs/PR_INTEGRATION.md) and [contributor credits](CONTRIBUTORS.md).

This is a **local candidate**, not a publication or live installation.

### Community qualification update

- Hardened risk-policy operands against custom string callbacks while preserving the original policy members. Hardened runtime identity observation against custom constant, descriptor, and metaclass callbacks; rejected values produce an unverifiable identity rather than a false match.
- The complete current-code suite passed: **1,289 passed, 1 skipped**, on the tested Python 3.14 runtime. The skip is the removed Von backend. This is not certification of every supported Python version or optional model backend.
- A real native installation from an immutable local Git source into a fresh credential-free Hermes home was **rejected before installation** by native security validation. No dependencies were installed, no plugin was enabled, and no serving profile was changed. The five critical policy-literal findings remain an installation compatibility blocker.
- Clean installed-runtime configuration/decision/disable/uninstall acceptance is therefore **not passed**. Private scanner experiments are not included in the release or required by its installation instructions. The candidate is **not release-ready** while this supported-installation gate remains unresolved.

## 0.4.2 — Selective HITL, effect-aware catalog, Git index repair

### Phase 0 — Git worktree/index
- Diagnosed the "dirty after release" report: the worktree index at
  `.git/worktrees/nerve-v0.4.0/index` was owned by `mega-mcp` with no ACL entry
  for the repository user, so reads failed and the staged state was a stale
  index written by a concurrent service — not repository corruption. Commits
  during 0.4.1 had gone through a temporary index, which left the real index
  behind HEAD.
- Repaired without `reset --hard`, `clean`, repository recreation, chmod 777 or
  tag changes: snapshot (source tarball, HEAD, tags, ACLs, gitdir listing),
  moved the unreadable index aside, rebuilt it with `git read-tree HEAD`, and
  added narrow ACLs (`u:mega-mcp:rw` on the index, default `u:j:rwx` on the
  worktree gitdir) so a service write cannot lock the owner out again.
- `build/` and `*.egg-info/` removed from tracking and ignored.

### Phase 1 — effect-aware classification
- Unknown plugin/MCP tools are no longer classified by name alone. Every call is
  screened across the tool name, argument keys, argument values and path-shaped
  arguments for credential targets (`get_secret`, `read_private_key`,
  `fetch_sensitive_config`, `*_access_key`, field names such as `password_hash`).
  A signal escalates to CONSEQUENTIAL with the concrete reason; a clean read
  shape is recorded as *inferred bounded* (`basis: mcp-verb-screened`), never as
  verified-safe.
- `operation_effect()` no longer treats an MCP verb as proof of read-only, so no
  name-based bypass exists for unknown remote tools.
- Deferred dispatch (`tool_call`) is classified by its inner tool and arguments.
- Native reads screen nested/alternate argument shapes (`uri`, nested objects,
  lists) for sensitive targets, and the read-only terminal bypass does the same.
- `printenv`, `env`, `set`, `export`, `declare`, `typeset`, `compgen` are
  classified as environment disclosure (CONSEQUENTIAL) and are no longer
  read-only bypasses; `ps e`/`ps eww` and `/proc/*/environ` are excluded too.
- Gate-failure approvals use a scoped, single-use rule key
  (`nerve:hitl:gate-failure:<tool>:<args-fingerprint>:<nonce>`) instead of one
  shared key, so a session/permanent grant cannot authorize an unrelated call.
- Recursion protection is unchanged: only the model primitives
  (decide/verify/assess/rank) keep the named internal allowance.

### Phase 2 — selective HITL and autonomy
- New bounded-uncertainty policy: an unrecognized but plainly local command is
  auto-allowed with telemetry in ask-only/enforce instead of prompting. Every
  pipeline segment is checked; shells/interpreters running inline code or script
  paths, wrappers (`timeout`, `env`, `xargs`, ...), network egress
  (`curl`, `wget`, `ssh`, `rsync`, ...), orchestration/publishing
  (`docker`, `kubectl`, `gh`, `npm`, `terraform`, ...), host/service control,
  privilege escalation, destructive recursive flags and force operations remain
  unbounded and keep escalation.
- Consequential approvals use a concise consequence message:
  `Do this -> ...` / `Skip -> ...` (no percentages, no essays). The native
  four-choice Hermes menu is unchanged.
- `browser_console` is routine page inspection (no host access).
- Live-registry audit: `scripts/audit_catalog.py --hermes` reads
  `hermes tools list`, maps toolsets, reports unknown toolsets and unresolved MCP
  servers, and separates verified-safe from inferred-bounded coverage.
  Measured 54/54 registered tools classified (23 verified safe, 12 verified
  bounded, 19 consequential, 0 unclassified, 0 unreviewed toolsets).
- `scripts/benchmark_workflow.py`: 100 ordinary calls at 0 approval
  interruptions, max hook latency 0.9 ms, with 14/14 adversarial calls still
  protected (secrets, private keys, force-push, curl|bash, kubectl delete, ssh,
  env dumps, protected writes, unknown MCP verbs, nested dispatch).

### Verification
- Full suite: 978 passed, 1 skipped.
- Real Hermes canary on the candidate build: 5 consecutive tools, 0 approval
  prompts, max hook chain 2.0 ms.

## 0.4.1 — P0 Hook Timeout Fix + Operation-Aware Safe Catalog

### P0 — pre-tool hook reliability (root cause: synchronous advisory inference)
- Advisory model evaluation moved OFF the synchronous `pre_tool_call` path onto a
  supervised daemon worker with a bounded queue (`hermes_nerve/advisory.py`).
  Verdicts are telemetry-only and can never retroactively authorize or block.
- Real per-evaluation deadline: each evaluation runs on a tracked daemon thread
  and the worker joins it for `NERVE_ADVISORY_CALL_TIMEOUT` (default 8s). A hung
  provider can no longer occupy the worker; abandoned evaluations are capped
  (`NERVE_ADVISORY_MAX_ABANDONED`, default 2) and new work is refused instead of
  spawning unbounded replacement threads.
- `enforce`/`ask-only` keep a single bounded synchronous attempt (8s budget,
  request-scoped timeout via `client.timeout_scope`) then fail to the native
  human approval boundary — no double retry, no 30s hook overrun.
- Circuit breaker only opens on genuine stalls/timeouts; instant failures
  (missing credential, refused connection) never suppress later decisions.
- Composite `pre_tool_call` chain is instrumented per callback with sanitized
  timing records (`nerve/hook-trace.jsonl`); arguments are never recorded.
- Explicit callback failure semantics: telemetry failures never block tools,
  advisory supervisions (remote/work/nervous) continue on error, and a crashing
  **action gate** fails to human approval instead of silently permitting.
- Worker lifecycle: test reset and session-end teardown join the previous worker
  before replacement; repeated initialization cannot accumulate threads.

### P1 — operation-aware safe catalog (`hermes_nerve/catalog.py`)
- Classifies tools by OPERATION (tool identity + effect-bearing arguments),
  not by name: skills, Nerve tools, memory, Kanban, browser, cron and MCP.
- Removed the blanket `nerve_`/`jev_` namespace bypass. Only the model
  primitives that would recurse (decide/verify/assess/rank) retain a narrow,
  named anti-recursion allowance.
- `skill_manage` is operation-aware (view/read = safe, create/update = project,
  delete = consequential, unknown = uncertain).
- `tool_call` (deferred dispatch) is never auto-allowed.
- MCP/plugin tools are classified by operation verb; unknown verbs stay
  UNCERTAIN.
- Sensitive-path reads keep escalating even for cataloged read tools.
- Added `scripts/audit_catalog.py` for ongoing coverage auditing (100% coverage
  across the 44 known native/Nerve tools) and catalog telemetry in
  `gate.report()` (`catalog_read_only_bypasses`, `interruption_ratio`).

### Verification (this release)
- Fix-before/after evidence: the advisory hook blocked 80.0s against a 30s
  budget before; after, the same path schedules asynchronously in <2ms.
- Real Hermes sessions with the candidate build: consecutive tool calls, max
  hook chain 4.8ms, zero 30s timeouts, zero approvals for cataloged read-only
  work, async provider failures recorded as telemetry only.
- Full suite: 947 passed, 1 skipped.

## 0.4.0 — Internal Release

- Added complete Operator University integration:
  * Student-facing interactive web UI at `/learn` (hermes_nerve/web/learn.html)
  * Command-line `nerve university` with list/status/show/check/capstone subcommands
  * Real-time progress tracking, disposable-lab fixtures, and capstone verification in the Control Panel UI
- Implemented HermesContextBus acceptance demo (N-33) as `hermes_nerve.integrations.contextbus_demo`, proving two-profile publishing/receiving, unauthorized profile rejection, and adapter fallback when bus is unavailable.
- Added regression tests for the five HITL v2.2 security defects:
  * Path-qualified executable guard
  * Credential directory nesting detection (including ~/.docker/, .env/, nested ~/.config/gh)
  * Git push effect identity (fixed --all/--tags/--mirror binding)
  * Unresolved consequential effect re-approval prevention
  * Multi-file authorization for reads over eight sensitive paths
- Verified all existing test suites (chunked runs due to host memory pressure) and closed the remaining unit test blockers (127 + 103 + 53 + 98 + 45 tests).
- Created formal packaging artifacts:
  * Updated plugin.yaml to version 0.4.0
  * Added CHANGELOG.md entry for this release
  * Generated RELEASE_MANIFEST (SHA‑256 checksums of plugin.yaml, hermes_nerve/ and docs/ artifacts)
  * Produced a local ZIP distribution `nerve‑v0.4.0.zip` with hermes_nerve/, docs/ and plugin.yaml
- Produced release verification evidence:
  * `docs/evidence/...` collection (re‑produced from HITL v2.4 snapshot and new university/capstone evidence)
  * Final completion‑integrity verifier `scripts/verify_completion.sh` (updated with new checks and university/capstone validation)
  * Real‑Hermes‑session plugin override test (`HERMES_ENABLE_PROJECT_PLUGINS=1`) to confirm v0.4.0 loads and preserves the live installation
- Documented activation and rollback procedure (docs/ACTIVATION.md) for a single‑session, controlled Nerve v0.4.0 rollout.

## Unreleased — Operator-first Control Panel

- Add a local-only, browser-based setup wizard and staged Hermes plugin installer, accessible via `nerve ui` and `nerve setup --web`.
- Preselect Operator on fresh setup; first-time GUI installs initialize Operator, while existing Legacy installations remain untouched until explicitly changed.
- Make Verified Experience and selective memory/tool/skill admission independently selectable with runtime dependency pruning and existing tool-schema compatibility.
- Add exact profile backup/restore, preview integrity checking, manifest-typed advanced settings, provider-free local connection tests, and source/wheel installation paths.
- Protect the browser API with ephemeral tokens, origin and host checks, strict CSP and nested credential redaction. Bundled HTML/CSS/JS requires no frontend build step.
- Add UI, rollback, installation-failure and compatibility regression tests. Source and wheel installations require a Hermes restart to activate changes.


## 0.3.2.1 — Selective Context

- Add a dependency-free, provider-neutral selective-context admission controller for memory, tools, and skills.
- Add task fingerprints and stable admission leases to reduce per-turn churn and preserve prompt-cache-friendly working sets.
- Add PIN / ADMIT / DEFER / SUPPRESS actions with mandatory-item protection and fail-open behavior.
- Add adapters for v0.3.2 Verified Experience, Hermes tool schemas, and skill metadata.
- Keep unselected tools and skills discoverable via DEFER; memory suppression is turn-local and never deletes provider state.
- Add bounded local token accounting and selective-context telemetry through nerve_stats.
- Default all surfaces to shadow mode and keep semantic reranking disabled by default.

See docs/RELEASE_0.3.2.1.md.

## 0.3.2 — Verified Experience

- Add a dependency-free local Verified Experience store for typed `observed`, `inferred`, `verified`, and `disproven` claims with evidence IDs, confidence, scope, temporal validity, supersession, conflict state, provenance, and outcome counters.
- Persist first-class negative knowledge from existing repeated/deterministic failure fingerprints so unchanged failed paths are not rediscovered as ordinary prose memory.
- Promote existing work checkpoints into successor-run capsules carrying verified DoD criteria, failed paths, discoveries, artifacts, evidence references, unresolved frontier, and the next move.
- Link controller-verified DoD facts to reusable experience while keeping the canonical work/Kanban store authoritative.
- Add deterministic scope/recency/confidence/usefulness/token-cost retrieval, token-aware memory admission, and explicit `why_retrieved` score metadata.
- Aggregate successful failure-fingerprint recoveries into bounded local recovery playbooks.
- Link claim/recovery outcomes back to decision/receipt provenance and expose bounded evidence graphs plus run/task/project/global summaries.
- Add provider-free `nerve_context_explain` for named context-governor profiles and `nerve experience status|search|explain|summary|graph|reconcile` CLI inspection.
- Add advisory-only offline reconciliation across verified experience and the existing receipt/outcome ledgers. Recommendations are review-required and can never self-mutate profile/configuration or canonical task state.
- Preserve the Legacy 16-tool contract, zero Nerve schemas in headless workers, dependency-free core, Python 3.10+, and hard-OFF schema behavior for Lean/Marie Kondo defaults while allowing their existing work-supervision path to preserve bounded local experience.

See `docs/RELEASE_0.3.2.md`.

## 0.3.1 — stable maintenance, hardening, and QOL

Stable promotion of the qualified `v0.3.1rc2` tree.

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