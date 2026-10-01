# Nerve Setup, Profiles, Factory Audit, and Optional Modules

**Status:** canonical implementation plan for the post-v0.2.3 direction  
**Date:** 2026-09-25  
**Base:** fork/main at Nerve v0.2.3  
**Product direction:** power users first; factory-hostile quality-of-life features remain useful but optional.

## 1. Destination

Turn Nerve from one large bundle of always-available capabilities into a configurable nervous-system platform with:

1. a simple `nerve setup` selector;
2. one full manual configuration path;
3. four named personalities/presets;
4. a factory-first audit of every existing subsystem;
5. module-level hard OFF semantics so disabled features cost effectively nothing;
6. the existing HermesContextBus available as a Shared Context module;
7. the Assistant Accountability work retained as an optional module, not merged into the core until the module substrate exists and its current review findings are fixed.

Central rule:

> If a feature is useful but adds schema, prompt, provider, runtime, storage, or maintenance cost that does not earn its keep in autonomous factory work, it must be optional and default OFF for factory-oriented profiles.

This implements the direction stated in GitHub Discussion #26: prioritize power users, preserve token-lean factory behavior, and make quality-of-life/digital-assistant capabilities easy to opt into.

## 2. Non-goals

- Do not integrate the WhatsApp bridge into Nerve in this plan.
- Do not rewrite HermesContextBus inside Nerve; integrate/control the existing implementation.
- Do not expose every low-level tuning key in the first setup screen.
- Do not merge the current Assistant Accountability feature branch before the module/profile substrate and review blockers are addressed.
- Do not silently change existing user profiles on upgrade.

## 3. UX: nerve setup

Primary command:

```text
nerve setup
```

Also support:

```text
python -m hermes_nerve setup
```

First screen must fit a normal 720p terminal, with one-line descriptions:

```text
NERVE SETUP

1. Full Configuration - Pick every Nerve module and advanced option yourself.
2. Fat Cat            - Max assistant quality; spend more tokens for capability.
3. Operator           - Direct Hermes power use: tools, coding, agents, context.
4. Lean               - Factory-first nervous system with token-heavy extras off.
5. Marie Kondo        - Minimum Nerve: only features that clearly earn their cost.
```

Menu order is authoritative.

### 3.1 Full Configuration

Full Configuration is the manual path, not a preset.

```text
NERVE FULL CONFIGURATION

[ON ] Reflex core          Cheap typed review through Jev/Laya/OpenJev.
[ON ] Work supervision     DoD, evidence and completion checks for workers.
[ON ] Token trajectory     Watch worker budget, repetition and likely completion.
[OFF] Action gate          Review individual tool actions before execution.
[OFF] Context governor     Curate/recover context and protect important state.
[OFF] Remote workers       Delegate and control Hermes workers on other hosts.
[OFF] Shared Context       Share bounded handoffs across Hermes agents/surfaces.
[OFF] Assistant loops      Persist goals, dependencies, triggers and follow-through.
[OFF] Assistant audit      Reflex-check active goals during normal Hermes turns.
[OFF] Shadow testing       Compare Reflex backends for development/calibration.
[ON ] Receipts             Keep bounded evidence for supervisory decisions.

Advanced configuration? [y/N]

When enabled, the editor is manifest-driven: `list` shows supported advanced keys, selecting a key prompts for a typed value, `clear <key>` removes an override, and blank input saves.
```

Full Configuration must show current resolved state. Advanced exposes thresholds, models, checkpoint fractions, confidence floors, context percentages, backend URLs, and timeouts.

## 4. Presets / personalities

Initial target values are hypotheses subject to the factory audit.

### 4.1 Fat Cat

Intent: maximum useful digital-assistant capability.

- Reflex core: ON
- Work supervision: ON
- Token trajectory: ON
- Action gate: ON
- Context governor: ON
- Remote workers: ON only when configured
- Shared Context: ON
- Assistant loops: ON
- Assistant audit: ON
- Shadow testing: OFF
- Receipts: ON
- Local learning/quality telemetry: ON where bounded

### 4.2 Operator

Intent: direct Hermes power users using desktop, CLI, Discord, coding tools, direct subagents, Codex/harnesses, or similar surfaces.

- Reflex core: ON
- Work supervision: OFF unless explicitly enabled
- Token trajectory: OFF unless supervising spawned workers
- Action gate: ON
- Context governor: ON
- Remote workers: OFF by default
- Shared Context: ON
- Assistant loops: OFF
- Assistant audit: OFF
- Shadow testing: OFF
- Receipts: ON
- Local learning/quality telemetry: ON where bounded

Transport/gateway does not define this personality.

### 4.3 Lean

Intent: candidate factory/power-user default. Preserve nervous-system value while removing quality-of-life features and recurring token taxes that are not justified by factory outcomes.

- Reflex core: ON
- Work supervision: ON
- Token trajectory: ON
- Action gate: OFF or selective only if audit proves value
- Context governor: OFF until benchmark evidence justifies it
- Remote workers: OFF unless explicitly required
- Shared Context: OFF initially, pending factory A/B benchmark
- Assistant loops: OFF
- Assistant audit: OFF
- Shadow testing: OFF
- Receipts: minimal/bounded ON
- Local learning: only if measured cost is negligible and useful

Lean is the candidate fresh-install default after the factory audit.

### 4.4 Marie Kondo

Intent: only features that materially improve Hermes.

- Reflex core: ON only to support surviving verification paths
- Definition of Done supervision: ON
- Evidence verification: ON
- Completion review: ON
- Token/repetition trajectory: ON only if benchmarks prove clear value
- Action gate: OFF
- Context governor: OFF
- Remote workers: OFF
- Shared Context: OFF until it clears a strong evidence bar
- Assistant loops: OFF
- Assistant audit: OFF
- Shadow testing: OFF
- Receipts: minimum required for evidence/provenance
- Nonessential telemetry: OFF

## 5. Module model

Create one registry for every user-facing subsystem.

Suggested IDs:

```text
reflex
work_supervision
token_trajectory
action_gate
context_governor
remote_workers
shared_context
assistant_loops
assistant_audit
shadow_testing
receipts
local_learning
```

Each module entry declares:

- stable module ID;
- one-line CLI name and description;
- dependencies/conflicts;
- cost classes;
- default state by profile;
- tools/hooks/context engines it owns;
- provider calls it can make;
- storage/background/network work it owns;
- advanced settings;
- validation/doctor callback;
- benchmark IDs that justify Lean/Marie Kondo enablement.

### 5.1 Hard OFF contract

OFF means absent, not merely dormant.

A disabled module must not:

- register its tool schemas;
- register its hooks;
- inject prompt/context text;
- initialize provider clients solely for that module;
- issue provider calls;
- scan/read its state every turn;
- start a sidecar/background service;
- poll;
- open network connections;
- create recurring telemetry events;
- impose avoidable storage/runtime work.

Tiny startup resolution cost is acceptable; per-turn cost is not.

### 5.2 Cost taxonomy

Audit every module for:

1. Schema tax
2. Prompt tax
3. Provider tax
4. Runtime tax
5. Storage tax
6. Network tax
7. Maintenance tax

## 6. Configuration resolution

One resolver only:

```text
Code-safe defaults
        + selected profile
        + explicit module overrides
        + advanced setting overrides
        = ResolvedNerveConfig
```

Persist:

- `nerve_profile`: `fat_cat | operator | lean | marie_kondo | custom`
- `nerve_modules`: explicit overrides; missing means inherit profile
- existing advanced settings remain valid

Full Configuration writes `custom` plus explicit module states.

### 6.1 Migration behavior

Existing users must not be silently flipped to a new personality.

- existing config -> `custom/legacy` until user chooses a profile;
- fresh install -> post-audit factory-safe default (candidate: Lean);
- setup shows current effective config before change;
- `nerve setup --reset <profile>` reapplies intentionally;
- setup writes atomically and backs up prior config;
- use Hermes supported config APIs where possible.

## 7. Full audit of current Nerve

This is release-blocking engineering work.

Current v0.2.3 systems that require justification include:

- nervous system / turn admission;
- local learning;
- context ledger;
- ContextEngine registration in shadow mode;
- work/Kanban supervision;
- provider decisions for work supervision;
- token trajectory observer;
- full controller/admin tool surface;
- remote-worker schemas for non-headless sessions;
- Reflex shadow infrastructure;
- receipts/telemetry.

The existing headless-Kanban behavior is the model: hide schemas that create fixed token cost and self-supervisor behavior.

### 7.1 Audit questions per module

For every module answer with evidence:

1. Does factory worker/orchestrator need this?
2. Is it critical-path, asynchronous, or local?
3. What fixed tokens does it add?
4. What provider calls can it create?
5. What percentage of calls change a decision?
6. Does it reduce total worker/orchestrator tokens?
7. Does it improve completion or reduce false PASS?
8. Does it reduce duplicated work/recovery cost?
9. Does it add latency/failure modes?
10. Can same value be obtained locally/cheaply?
11. If disabled, is runtime cost actually zero?
12. Which profiles should own it by default?

### 7.2 Benchmark matrix

At minimum benchmark:

- one headless Kanban worker;
- four parallel workers;
- recursive/subagent fan-out;
- long coding task;
- repeated failure/replan task;
- evidence/DoD completion task;
- restart/resume/handoff;
- multi-agent task with duplicated discovery pressure.

Compare:

1. current v0.2.3 defaults;
2. all optional modules hard OFF;
3. proposed Lean;
4. proposed Marie Kondo;
5. one-module-at-a-time ablations.

Measure tokens, schema footprint, injected prompt tokens, provider calls/cost, latency, completion rate, false PASS, useful interventions, duplicated work, recovery time, and meaningful disk/network activity.

## 8. Shared Context module

### 8.1 Existing implementation

Do not rebuild.

Current implementation:

```text
<HermesContextBus worktree path>
```

- pre-LLM bounded context injection;
- post-LLM bounded durable handoff;
- explicit post/send/inbox/thread/ack tools;
- stable agent/surface identities;
- acknowledgement only for delivered messages;
- MegaMCP bb.* adapter when policy allows;
- SQLite/WAL fallback;
- immutable board entries;
- durable inboxes/threads/acks;
- atomic shared-context.md projection;
- sticky backend choice;
- 28-test implementation suite;
- persistence, immutability, policy-failover and concurrent-writer tests.

Host deployment remains unverified. Do not claim production readiness until Plugin Doctor and live probes pass.

### 8.2 Nerve integration model

Treat Shared Context as an external Nerve-controlled module.

Nerve setup should:

- detect whether HermesContextBus is installed for active profile;
- install idempotently when requested;
- enable/disable plugin entry where supported;
- run/print Doctor status;
- show active backend (MegaMCP or SQLite);
- preserve durable data when disabled unless user explicitly removes it;
- guarantee no Shared Context tool/hook/prompt cost when disabled.

Shared messages remain coordination data, never authority.

### 8.3 Profile defaults

- Fat Cat: ON
- Operator: ON
- Lean: OFF pending factory A/B evidence
- Marie Kondo: OFF pending stronger evidence

Benchmark whether bounded handoffs save more rediscovery tokens than they inject. If yes, Shared Context can move ON in Lean.

### 8.4 WhatsApp boundary

HermesContextBus may understand a WhatsApp surface identity, but Nerve does not integrate or own the WhatsApp bridge in this plan.

## 9. Assistant Accountability module

### 9.1 Product placement

Assistant Accountability is optional, primarily for Fat Cat.

Split into at least:

- `assistant_loops`: durable goals/open loops and explicit completion review;
- `assistant_audit`: recurring per-turn Reflex follow-through checks.

This allows durable goals without recurring audit cost.

### 9.2 Current branch status

Prototype branch:

```text
feature/assistant-accountability-loop
```

Reviewed candidate reached:

```text
1eb28220b55ca8731f12da131a8abf4921db06ca
```

Not ready to merge. Remaining findings to fix during rebase:

1. agent-written standing rules must not become trusted persistent authority;
2. explicit persisted disable must win across processes/restarts;
3. concurrent completion reviews need reserved review generations so newest review wins regardless of return order;
4. provider audit payloads need independent hard size bounds;
5. cross-process and reverse-order race tests;
6. Windows lock failure/contention tests if Windows is supported.

Do not add more product features before module substrate lands.

### 9.3 Authority rule

- Main Hermes/user remains authoritative.
- Reflex may verify completion and recommend CONTINUE/NUDGE/REPLAN/ESCALATE.
- Reflex does not grant new user permissions.
- Agent-authored persistent content is untrusted coordination data.
- Only explicit user-controlled grants may create standing authority in a future release.
- Consequential actions remain subject to Hermes/user approval policy.

## 10. Remote workers

Make remote worker capability a real module.

When OFF:

- do not register `nerve_remote_*` tools;
- do not initialize SSH worker state;
- do not advertise remote capability.

Fat Cat may enable when hosts configured. Others should not pay schema tax unless enabled.

## 11. Context governor

Separate context governance from Shared Context.

`context_governor` owns:

- context ledger;
- curation;
- rehydration;
- Nerve ContextEngine registration;
- shadow/apply modes.

Shared Context owns cross-agent/surface coordination.

Factory audit decides whether current context shadow/ledger behavior earns default cost.

## 12. Reflex, nervous system, and action gate

Keep separate in setup even if they share provider machinery.

- `reflex`: provider-neutral typed decision primitive;
- core `nervous`: decision/event supervision;
- `action_gate`: synchronous/selective pre-tool approval/blocking.

Audit real call frequency. `nervous_max_provider_calls_per_turn=96` is a ceiling, not evidence of 96 calls, but routing/leases/batching must prove no call storms.

## 13. Implementation phases

### Phase A - baseline and inventory

1. Freeze current fork/main as audit baseline.
2. Generate machine-readable inventory of tools, hooks, provider calls, stores, config keys by module.
3. Record fresh-install schema footprint.
4. Record per-turn idle cost for controller/headless contexts.
5. Map every setting to module/advanced/deprecated/internal.
6. Produce one authoritative inventory.

### Phase B - module registry and resolver

Create:

```text
hermes_nerve/modules.py
hermes_nerve/profiles.py
hermes_nerve/config_resolver.py
```

Requirements:

- registry owns metadata/profile defaults;
- resolver produces `ResolvedNerveConfig`;
- registration consumes resolved states;
- disabled-module negative tests;
- legacy settings continue to resolve.

### Phase C - nerve setup

Add:

```text
hermes_nerve/cli.py
hermes_nerve/__main__.py
```

Implement:

- five-item first menu;
- one-line descriptions;
- profile preview;
- Full Configuration;
- Advanced submenu;
- atomic save/backup;
- `--show`, `--explain`, `--reset`, noninteractive `--profile`;
- Doctor summary;
- no provider/network call merely to run setup.

Exit: profile/module changes in about 15 seconds.

### Phase D - hard OFF retrofit

Priority:

1. remote workers;
2. context governor;
3. action gate;
4. work supervision/token trajectory split;
5. nervous/local-learning extras;
6. shadow testing;
7. receipts/telemetry depth.

Each OFF state gets negative tests for tools/hooks/provider calls.

### Phase E - Shared Context integration

1. Verify current HermesContextBus tests.
2. Install to disposable/test Hermes profile.
3. Run Plugin Doctor.
4. Live-test Desktop + CLI + Discord or available non-WhatsApp surfaces.
5. Test concurrent Hermes processes against SQLite/WAL.
6. Test MegaMCP blackboard allowed/denied paths where policy permits.
7. Add Nerve setup adapter/detection.
8. Add ON/OFF absence tests.
9. Add factory A/B benchmark.

### Phase F - Assistant salvage/rebase

After module isolation:

1. persistence -> `assistant_loops`;
2. recurring Reflex review -> `assistant_audit`;
3. fix trusted-rule boundary;
4. persisted explicit-disable precedence;
5. review generation ordering;
6. hard-bound provider payloads;
7. cross-process/reverse-order/Windows-support tests;
8. schemas/hooks absent when OFF;
9. loops ON + audit OFF makes no per-turn provider call;
10. independent exact-SHA review.

### Phase G - factory audit/profile tuning

Produce:

```text
planning/nerve-setup-profiles-factory-audit/FACTORY_AUDIT.md
planning/nerve-setup-profiles-factory-audit/PROFILE_EVIDENCE.md
```

Record for every profile/module pair: enabled state, evidence, known cost, quality benefit, unresolved gap.

### Phase H - release/migration

1. update README/setup docs;
2. document migration;
3. add release verifier assertions;
4. test clean install and upgrade from v0.2.3;
5. test all profiles through Plugin Doctor;
6. verify CLI entrypoint/dependencies;
7. two consecutive independent CLEAN exact-SHA reviews;
8. publish as standalone reviewable release/PR.

## 14. Tests and CI gates

Add at least:

```text
tests/test_profiles.py
tests/test_module_registry.py
tests/test_module_absence.py
tests/test_setup_cli.py
tests/test_profile_migration.py
tests/test_shared_context_adapter.py
tests/test_factory_profile_smoke.py
```

Required invariants:

- deterministic profile resolution;
- Full Configuration round-trip;
- disabled modules expose zero owned tool schemas;
- disabled modules do not inject context;
- disabled modules cannot make provider calls;
- headless Kanban remains schema-minimal;
- v0.2.3 config migrates without surprise enablement;
- Shared Context disabled means no footprint;
- Assistant disabled means no Assistant hooks/provider/prompt/storage footprint and no additional tool schema; enabled Assistant actions reuse the existing nervous-event transport;
- profile changes cannot grant new action authority;
- setup performs no remote model/provider work.

## 15. Proposed file ownership

Nerve repo:

```text
hermes_nerve/modules.py
hermes_nerve/profiles.py
hermes_nerve/config_resolver.py
hermes_nerve/cli.py
hermes_nerve/__main__.py
hermes_nerve/integrations/shared_context.py
planning/nerve-setup-profiles-factory-audit/IMPLEMENTATION_PLAN.md
planning/nerve-setup-profiles-factory-audit/FACTORY_AUDIT.md
planning/nerve-setup-profiles-factory-audit/PROFILE_EVIDENCE.md
```

HermesContextBus remains separate at:

```text
<HermesContextBus worktree path>
```

## 16. Dependency graph

```text
A Baseline/inventory
        |
        v
B Module registry/resolver
        |
        +------------------+
        |                  |
        v                  v
C nerve setup          D hard-OFF retrofit
        |                  |
        +---------+--------+
                  |
          +-------+-------+
          |               |
          v               v
E Shared Context     F Assistant salvage
          |               |
          +-------+-------+
                  |
                  v
         G Factory audit/profile tuning
                  |
                  v
         H Migration/release gate
```

## 17. Decisions already made

1. Power-user/factory direction has priority over casual convenience defaults.
2. Useful factory-hostile QoL features are optional rather than deleted.
3. `nerve setup` is the main selector.
4. Menu option 1 is Full Configuration; then Fat Cat, Operator, Lean, Marie Kondo.
5. OFF means absent as far as practical, especially per-turn/model-context cost.
6. WhatsApp bridge is excluded for now.
7. Shared Context is included and defaults ON for Fat Cat + Operator.
8. Shared Context defaults OFF for Lean until factory evidence exists, and OFF for Marie Kondo until stronger evidence exists.
9. HermesContextBus remains coordination data, not an authority channel.
10. Assistant Accountability is optional and split into persistence vs recurring audit cost.
11. Current Assistant feature branch is a prototype, not a merge candidate.
12. Existing users are not silently migrated.
13. Fresh-install default is chosen after factory audit; Lean is the current candidate.

## 18. Open decisions requiring evidence

1. Does Shared Context save enough duplicate/recovery work to turn ON in Lean?
2. Does context governor/ContextEngine shadow mode earn factory-default status?
3. Does action gating improve factory outcomes enough to justify cost?
4. Which local learning/telemetry belongs in Lean and Marie Kondo?
5. Is token trajectory supervision essential enough for Marie Kondo?
6. Which controller/admin tools can be hidden outside Full Configuration?
7. Should remote workers auto-enable when hosts exist or require explicit ON?
8. What provider-call ceiling is appropriate after real workload measurements?

## 19. Definition of done

Implemented when:

- `nerve setup` presents the five agreed choices cleanly in a 720p terminal;
- profile resolution is deterministic/documented;
- each major subsystem has one module owner;
- module OFF behavior has negative tests;
- factory/default profile is benchmark-supported;
- current defaults have been audited for schema/prompt/provider/runtime/storage/network cost;
- Shared Context is integrated as optional external module and live-tested outside WhatsApp;
- Assistant Accountability is rebased behind optional modules and current review blockers are closed;
- existing configs migrate without silent expansion;
- fresh install pays no cost for disabled QoL modules;
- exact final SHA receives normal independent review gate.
