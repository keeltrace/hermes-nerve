# Selective Context: model-bound integration

This implementation builds on `95a35291f9ebe3854f402184040f6eb79d5522c8`.

## Behavior

- **ON/APPLY:** an enabled surface applies its selection automatically.
- **OFF:** no selection or injection for that surface.
- **SHADOW:** explicitly requested observation/planning only; stock payload is unchanged.
- Parent module/enable switches and per-surface switches remain authoritative.
- Existing explicitly configured `shadow` values are not silently overwritten.

The plugin now registers supported Hermes `llm_request` middleware and a
`pre_llm_call` memory adapter. No Hermes source changes or monkeypatches are
required. Settings are resolved for each callback, not cached at registration.
Legacy/unconfigured profiles retain legacy behavior.

### Three surfaces

1. **Memory:** retrieve profile/user-scoped Verified Experience candidates,
   validate their home/session/user scope, select them, and render selected
   evidence as text **before** Hermes's ordinary hook injection. Do not pass
   a structured dict to a host that would simply stringify the entire candidate
   pool. Mandatory negative/disproven knowledge survives discretionary caps.
   Canonical personal memory and other providers' unstructured context are never
   parsed or removed. Verified Experience must itself be enabled as the source.
2. **Tools:** use Hermes's own deferrability classification. Retain all core and
   unknown schemas. Non-core schemas may defer only when the complete existing
   search/describe/call bridge is available. Hermes normally defers schemas
   before request middleware runs, so also select the bridge's catalog context:
   retain relevant descriptions, demote other descriptions to names, and add a
   selected HOT hint for compact names-only catalogs. All deferred names and
   stock discovery instructions remain present. Group-only catalogs with no
   tool names stay unchanged; never invent schemas or read a global registry to
   infer session grants. Selection does not execute tools or change approvals.
3. **Skills:** apply the existing index selector to the actual outgoing
   system/developer prompt, including Anthropic `system` and Responses
   `instructions` layouts. Keep relevant descriptions and every deferred skill
   name; preserve mandatory/pinned entries, existing loaded bodies, and all
   text outside the known `<available_skills>` section. This selects index
   context; it does not execute skill preprocessing or eagerly load skill bodies.

Transformations are request-local copies. Persisted history and the stock
session prompt are not edited. Adaptive request prefixes can affect prompt-cache
reuse; no cache-savings or end-model quality claim is made here.

## Verification

Run the native integration test with the installed **Hermes interpreter** from
the repository root:

```sh
PYTHONPATH="$PWD:$PYTHONPATH" python tests/test_selective_context_request.py
```

It uses the real installed Hermes plugin manager/context, `build_api_request`,
request sanitization, request middleware, execution dispatcher, memory hook
collector, tool describe dispatcher, and skill loader. Only provider kwargs
construction and final transport are fixture adapters. Selectors and dispatch
are not replaced with simulated selections. No model/network call is made.

Set `NERVE_CONTEXT_EVIDENCE_DIR` to a directory outside the checkout to retain
synthetic-fixture stock/model-bound request JSON captures from real execution.
The native suite explicitly skips when Hermes is absent; a skipped run is NOT
integration qualification.

Observed local results:

- Native model-bound integration: **10 tests passed, zero skipped**.
- Activation, consumers, planner, registration and module regressions:
  **45 passed** with `/usr/bin/python3 -m pytest` on the five affected suites.
- ON changes the outgoing skill and tool context; relevant scoped memory reaches
  the captured model-bound user context, irrelevant candidate memory is omitted,
  mandatory negative knowledge is retained.
- OFF and SHADOW yield exactly unchanged request payloads; memory is not injected.
- A demoted tool still describes through native Tool Search, and a demoted skill
  still loads through native `skill_view`.
- The actual plugin entrypoint registers the integration, and parent disable is
  honored on a subsequent request without re-registering callbacks.

The original fixture-only qualification above is supplemented by real-session
qualification below. No production gateway was upgraded or restarted.

## Real Hermes sessions and follow-up repairs

The continuation from `8252965` found and repaired two real integration gaps:

- Provider-bound Tool Search is named `hermes_tool_search`, while the pre-provider
  bridge schemas use `tool_search`. Both must preserve the complete discovery
  bridge and apply catalog selection.
- Selection must use the **original user goal**, not the assembled user message:
  injected memory metadata otherwise diluted ranking enough to demote even the
  relevant tool. The pre-LLM callback now supplies a bounded, lock-protected,
  exact-session/turn goal map to request middleware. No cross-session fallback.

Three isolated native `AIAgent.run_conversation` sessions used real existing
Codex-subscription OAuth and model `gpt-6.1-sol`. Native plugin discovery,
tool/skill assembly, memory retrieval, middleware and provider transport ran
without monkeypatches or fabricated responses. Each session completed with `OK`
and two real provider responses. Only synthetic fixture context was sent.

The ordinary `pre_api_request` observer preview truncates long instructions.
The qualification helper therefore captures the complete payload in supported
`llm_execution` middleware, then calls `next_call(request)` unchanged and records
its actual response ID. Authorization headers are not captured.

Observed before/after excerpts from the first real outgoing request:

```text
OFF and SHADOW skill index:
    - github-review: Review GitHub pull request code
    - recipes: Cook dinner from recipes

ON skill index:
    - github-review: Review GitHub pull request code
    - recipes

OFF and SHADOW deferred tool catalog:
- fixture_github_review: Review GitHub pull request code
- fixture_recipes: Cook dinner from recipes

ON deferred tool catalog:
- fixture_github_review: Review GitHub pull request code
- fixture_recipes
```

Memory sentinel assertions on those same requests:

- `ORCHID-47` (relevant evidence): present only ON.
- `PINNED-18` (mandatory negative evidence): present only ON.
- `RECIPE-92` (irrelevant candidate): absent in every mode.
- The mandatory user instruction survives all modes.
- The core terminal schema is identical in all modes.
- All visible tool names and deferred catalog/skill names remain available.
- OFF and SHADOW skill blocks and tool-catalog descriptions match exactly.

Full requests and response IDs stay local; they are not published. A local proof
receipt stores request SHA-256 values, response IDs, final results, and assertions.
The reproducible real-session helper is:

```sh
python scripts/verify_selective_context_session.py --mode off --output /path/to/new-off
python scripts/verify_selective_context_session.py --mode apply --output /path/to/new-on
python scripts/verify_selective_context_session.py --mode shadow --output /path/to/new-shadow
```

This helper deliberately requires an existing Codex subscription. It never
copies personal config/memory/auth files into the isolated fixture homes.

### Integrity objection

The progress guard classified tracked source under any ancestor named `scratch`
as disposable. The initial Selective Context worktree used exactly such a path.
It now requires actual local Git tracking evidence before treating an ordinary
source filename under scratch as progress. Untracked probes, explicitly named
probe files (even when tracked), and Git lookup failures still do not qualify.
The classifier change does not waive mutation, path-relevance, or verification
requirements. The original serving gateway is not modified by this source fix.

### Native admission and CI

The inherited CI scanner-exception wrapper was removed, not repaired or expanded.
CI now runs unmodified native validation against pinned official Hermes commit
`1c0cd667746d3395fe9e1b6c74f8f85952486f18`, plus the native request integration
suite. Its runtime job uses Python 3.14: the official pinned Hermes metadata
allows 3.11 for updater bootstrapping but gates runtime dependencies on >=3.14.
The first native CI attempt on 3.11 failed before validation with missing
`ruamel.yaml`; correcting the runtime job leaves the full Nerve 3.10–3.14
compatibility matrix intact. CI builds the pinned runtime through upstream's
frozen `pm.build_env` workflow, not raw pip: upstream's lock intentionally
selects security-fixed Pillow 13 despite browser-harness's older exact pin.
No upstream source or dependency/security policy is rewritten for CI.
Release verification uses two parallel test processes to avoid CPU contention
in runtime-identity hashing; all test files and the 75-second per-file deadline
remain intact. The locally exercised `plugin_guard.py`,
`plugin_guard_context.py` and
`skills_guard.py` were SHA-256 compared to that exact official revision: all
three matched byte-for-byte. Native validation passed with **caution**, not a
DANGEROUS override, and native doctor passed. No Hermes core source was edited.

The blanket test-temp-directory error suppression and unused deadline fixture
inherited from the PR were removed; tests must expose real cleanup failures.

The CI fixture follow-up preserves the original safety assertions: descendant
state reads catch only `FileNotFoundError` from an actually vanished process;
live descendants and permission errors remain failures. Browser startup writes
to a fixture-owned log instead of undrained pipes, discovers Chrome's own
`DevToolsActivePort`, and cleans up its own process group even on startup
failure. A noisy synthetic child reproduced the old pipe deadlock before the
repair; this is harness regression evidence, not a replacement for the required
real-browser DOM test. The real Chrome startup deadline remains 25 seconds.

Final local verification after integration:

- **12** native request integration tests passed, zero skipped.
- **67** targeted activation/planner/registration/progress/security regressions passed.
- **129** core/profile/experience/nervous/deadline regressions passed.

These checks do not claim a fleet deployment, a merge, or a new release.
