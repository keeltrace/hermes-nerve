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

This is isolated local integration qualification, **not** a claim that a running
Discord/CLI/desktop gateway was upgraded or restarted, nor a live-provider test.
No full Nerve suite rerun, publication, push, merge or deployment is included.
