# Nerve

Nerve is a supervisory nervous system for Hermes Agent with provider-neutral
Jev, Laya, and OpenJev Reflex backends, evidence-backed Kanban supervision,
token-budget trajectory control, context governance, and durable SSH Hermes
workers. Hermes remains the authority for canonical task state and policy; the
core plugin is dependency-free and model runtimes stay in isolated sidecars.

- Plugin name: `nerve` (see [`plugin.yaml`](plugin.yaml))
- Current version: see [`CHANGELOG.md`](CHANGELOG.md)
- Documentation index: [`docs/`](docs/)

## What Nerve does

- **Kanban worker supervision.** Workers start with zero Nerve tool schemas.
  Nerve observes token spend, tool results, and failure fingerprints through
  hooks, and spends supervisor calls only when a worker looks unhealthy
  (repeated failure, or an unhealthy 40%/70% trajectory checkpoint).
- **Verified completion.** Mechanical DoD criteria are proved deterministically
  in-process; only unresolved semantic criteria reach the decision engine.
  Completion fails closed: a non-PASS verdict returns the exact missing
  criteria, and a PASS dispatches the native `kanban_complete` in-process.
- **Token-budget trajectory control.** A per-task budget is auto-estimated and
  recorded as non-blocking `DOD-BUDGET` telemetry. Budget pressure cannot
  invalidate correct work; only the orchestrator/reviewer may stop or block.
- **Durable SSH workers.** Admin-defined remote hosts with workspace/profile
  allowlists, stdin-only task transport, cancellation, timeouts, and
  exact-run fencing. Controller credentials are never forwarded over SSH.
- **Verified Experience and Selective Context.** Evidence-backed typed claims
  (verified/disproven) replace conversation retention, and context curation
  plans the smallest useful working set across memory, tools, and skills.
  Optional; see [docs/DEVELOPMENT_HISTORY.md](docs/DEVELOPMENT_HISTORY.md).
- **Reflex backends.** A provider-neutral seam below `DecisionEngine`:
  `reflex_backend=jev` (default), `shadow` (paired logging), or local sidecar
  backends (`laya`, `openjev`, D1 via `/v1/systemone`). The plugin never
  imports torch/transformers; model runtimes run in separate processes.

### Worker supervision flow

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

Design invariant: **Jev must earn every token it spends.** The permanent
healthy-run benchmark target is <=2% fixed overhead versus plain Hermes, with
<=0.5% as the stretch target.

### Selective Context activation

When Selective Context and its individual Memory / Tools / Skills modules are
enabled, their default mode is now **apply**, not shadow. Turning a module off
leaves that surface unchanged. Operators can explicitly choose shadow to
compare decisions without changing model-visible context.

On compatible Hermes hosts, this applies: relevant tool HOT hints in Tool
Search (without removing any core tools), demoted descriptions for less
relevant skills (names remain discoverable), and verified/disproven Nerve
experience through a structured, scope-checked memory hook. The memory path
never calls a model or imports another profile's store. Explicitly scoped user
claims are not shared with other users.

**Integration requirement:** tool and skill application needs the Hermes host
consumer bridges. Nerve cannot rewrite provider tool schemas through its
plugin API alone. Hosts without these bridges retain stock behavior; merely
selecting APPLY is not proof of prompt-token savings. Validate the actual
Hermes turn before claiming a successful rollout. Legacy remains unchanged.

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

Nerve deliberately does **not** own canonical task status, dependencies,
retries, queues, or completion state. Those remain in Hermes Kanban.

## Install

Install from the Hermes plugin catalog:

```bash
hermes plugins install nerve
```

Or configure and install from a source checkout with the local control panel:

```bash
python3 -m hermes_nerve ui
# or, after installing the Python package:
nerve ui
```

The control panel lets you select Operator (the new-install default), toggle
modules, choose a Reflex backend, preview the resolved behavior, and
install/enable the plugin in a selected Hermes home with one-click rollback.
No frontend build tools or cloud login are needed, and no downloaded model or
hosted Jev call occurs automatically on setup. Existing unconfigured
installations retain Legacy until explicitly changed. Runtime state is not
claimed until Hermes restarts. See the
[Control Panel guide](docs/CONTROL_PANEL.md) and the
[local backend tutorial](docs/LOCAL_BACKEND_GUIDE.md).

## Setup and profiles

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

Profile state is persisted atomically at `$HERMES_HOME/nerve/profile.json`.
Full Configuration includes a manifest-driven advanced editor for typed
sidecar overrides; re-selecting the same profile preserves those overrides,
while `--reset` clears them deliberately. Advanced Hermes plugin settings can
also be supplied through the existing Hermes configuration surface. Existing
unconfigured installations retain **Legacy** behavior until a profile is
explicitly selected. `nerve_auto_kill` is compatibility state only; economic
stop authority remains with the orchestrator/reviewer.

### Hard-OFF modules

For selected profiles, disabled modules are omitted from the Hermes
model/runtime surface where practical: their tool schemas are not registered,
their hooks are not installed, and their optional context engines/provider
paths are not activated. Headless Kanban workers continue to expose zero
Nerve tool schemas.

### Shared Context

Shared Context is an optional external integration. Nerve does **not** vendor
HermesContextBus. Because HermesContextBus is not currently published through
a public install source, Shared Context is OFF by default in all named
profiles. Users with an explicit local HermesContextBus checkout can opt in
and install it manually.

```bash
nerve setup --explain
nerve setup --install-shared-context /path/to/HermesContextBus
```

Shared-context messages are coordination data, never tool authority or user
permission.

### Assistant Accountability

Fat Cat enables optional persistent Assistant loops plus a per-turn
accountability audit. Loops carry a next move, owner, dependencies,
trigger/deadline, and Definition of Done. Completion is review-gated; generic
updates cannot mark a loop done.

Assistant loop content is treated as untrusted coordination data. It cannot
create standing permission. The audit is independently switchable so durable
loops can be used without recurring provider calls. Profile selection never
writes the persistent Assistant operator override; only the explicit
Assistant install/disable CLI actions may do that. See
`planning/nerve-setup-profiles-factory-audit/IMPLEMENTATION_PLAN.md`,
`FACTORY_AUDIT.md`, and `PROFILE_EVIDENCE.md` for design and evidence.

## Tools and hooks

Supervision tools:

- `nerve_supervise_card` — bind/amend DoD, bind run, status, verify criteria,
  record budget, assess trajectory, checkpoint, label outcomes, request
  canonical review.
- `nerve_work_event` — worker progress/checkpoint events.
- `nerve_work_status` — compact progress/frontier/control view; exposes
  worker tokens, supervisor tokens, combined tokens, estimated avoided tokens,
  and estimated net savings.
- `nerve_remote_delegate_task` — durable SSH delegation.
- `nerve_remote_worker_status` / `nerve_remote_worker_result` /
  `nerve_remote_worker_cancel` / `nerve_remote_worker_control`.

Decision and context tools: `nerve_decide`, `nerve_rank`, `nerve_verify`,
`nerve_assess`, `nerve_context_curate`, `nerve_context_rehydrate`,
`nerve_context_explain`, `nerve_stats`, `nerve_nervous_event`.

Hooks: `pre_tool_call`, `post_tool_call`, `pre_llm_call`, `post_llm_call`,
`transform_tool_result`, `pre_verify`, `post_api_request`,
`api_request_error`, `on_session_end`.

## Remote workers

Configure `remote_hosts` through the Hermes plugin configuration mechanism as
a mapping. Each host is an admin-defined alias; callers cannot inject
arbitrary SSH options. A host can constrain `workspace_root`,
`profile` / `allowed_profiles`, `max_turns`, timeout, model/provider/toolsets,
and optional `git_cache_root` / `control_root`. Remote task text is sent over
stdin; controller provider credentials are not forwarded through SSH.

## Verification

The canonical release gate is `bash scripts/verify_dev17.sh`; it runs the
release verifiers and every `tests/test_*.py` file under pytest. See
[CONTRIBUTING.md](CONTRIBUTING.md) for the development setup and pre-PR
verification commands.

```bash
bash scripts/verify_dev17.sh
python3 scripts/verify_dev6_benchmark.py
python3 -m compileall -q .
python3 scripts/verify_release.py
```

## Release status and archive policy

The v0.4.14 candidate passes the full local test suite and the stock Hermes
security scan with verdict **caution** (zero critical findings; the remaining
high findings are Nerve's own blocking-policy tables, which quote the exact
patterns the engine refuses to execute — they are data, disclosed here, and a
caution verdict installs through Hermes' standard confirmation, not a bypass).
Native validation that reports **dangerous** is never overridden.

Remaining acceptance before a release is published from this branch:

- completely green CI on the release branch (Python 3.10–3.14 and plugin validation),
- maintainer review and merge of PR #75 by the repository owner,
- a clean install / upgrade / rollback / uninstall rehearsal against the exact
  release SHA.

Publication is an explicit manual action: the release workflow runs only via
`workflow_dispatch` and never fires automatically from a push to `main`.
Merging is a code decision; publishing is a separate release decision.

### Archive content policy

Release archives are built from the tracked tree at the release ref
(`git archive`). The tracked public tree intentionally excludes internal
planning, handoff/evidence material, build outputs, and internal reports —
v0.4.14 removes those from the public tree. The installed plugin payload is
additionally filtered by `hermes_nerve.runtime_manifest` (Python modules plus
declared assets only); `scripts/` development tooling is repository content,
not shipped payload. If engineering history is ever published on purpose, it
is reviewed for disclosure first.

### Community release support and limitations

- A supported release must install and register its tools/hooks on an **unmodified Hermes version**. Private scanner patches are not a supported dependency, and bypassing native security validation is not an installation method.
- The ordinary configuration/control-panel/decision path, backend-error behavior, approval handling, and disable/uninstall lifecycle require a real clean-profile smoke test. Unit tests, simulated installers, and an HTTP health response are not substitutes.
- Optional Jev/Laya/OpenJev/System One/D1 and shadow integrations are **experimental until the particular backend and workflow have been exercised live**. Listing a configuration option or passing a mocked transport test does not certify that integration. Model downloads and hosted calls are opt-in, not release-qualification prerequisites for every optional backend.
- The final source revision must pass the complete existing suite; the release package must include its license, configuration guidance, changelog, and contributor credits, and exclude credentials and private development files.
- Nerve is a community supervisory plug-in, **not a security sandbox or a guarantee that arbitrary third-party Python code is safe**. Hermes retains execution and approval authority. Do not rely on static scan results alone as a proof of safety.

## Development history

The per-release development notes (dev5 foundation, Dev9/Dev11/Dev14/Dev15b/
Dev17, the v0.3.2.x lines, the generic System One / Liquid D1 line, and the
dev17-era install notes) have moved verbatim to
[`docs/DEVELOPMENT_HISTORY.md`](docs/DEVELOPMENT_HISTORY.md). The dev5
foundation started from the last publicly verifiable fixed point,
`0.2.2.dev4` commit `a3aeedc0006797c244ef29d7e085616a5253e627`; see
`docs/DEV_0.2.2.md` and [`SOURCE_PROVENANCE.md`](SOURCE_PROVENANCE.md).
