# Dev15b Laya integration

This document records the dev15b Laya/Reflex integration slice that is now merged into finalized `0.2.2.dev15`.
It is intentionally **not** a Nerve product rename and it does **not** change the Hermes community catalog.

The invariant is simple:

> dev14 completion authority stays intact. Laya enters below `DecisionEngine`, never around deterministic authority.

## What this package adds

- provider-neutral `DecisionEngine` construction through `hermes_nerve.reflex`;
- three backend modes: `jev`, `laya`, and `shadow`;
- dependency-free `LayaClient` using the same `system_one(state, questions)` contract as the existing Jev provider seam;
- a fixed-model, preloaded Laya HTTP sidecar (`python -m hermes_nerve.reflex.laya_service`);
- Jev-authoritative / Laya-shadow paired telemetry;
- fail-open shadow behavior;
- correct `LOCAL_ONLY` provenance for Laya decisions;
- exact sidecar-model identity checking;
- optional `laya==0.3.5` dependency extra without adding torch/transformers to the core Hermes plugin;
- local paired shadow report (`hermes_nerve.reflex.telemetry.report`; see Shadow telemetry);
- compatibility aliasing for `work_estimated_jev_call_tokens` while introducing the provider-neutral `work_estimated_decision_call_tokens` key.

## Architecture

```text
Hermes supervisor code
        |
        v
DecisionEngine
        |
        v
Reflex provider factory
   |        |        |
   |        |        +-- shadow: Jev authoritative + Laya comparison
   |        +----------- laya: Laya authoritative
   +-------------------- jev: current behavior

Laya mode / shadow copy
        |
        v
stdlib HTTP client
        |
        v
preloaded Laya sidecar
        |
        v
convaiinnovations/laya : typed-decisions
```

The Hermes plugin stays dependency-free. `torch`, `transformers`, `safetensors`, and model weights live only in the sidecar environment.

## Backend modes

### `jev` (default)

Existing behavior. This is the default so merging dev15b does not silently alter authoritative decisions.

```yaml
plugins:
  entries:
    nerve:
      settings:
        reflex_backend: jev
```

### `shadow`

Jev remains authoritative. The same redacted `state` and typed `questions` are sent to Laya for comparison.
Laya failures never change the returned Jev result.

```yaml
plugins:
  entries:
    nerve:
      settings:
        reflex_backend: shadow
        reflex_laya_base_url: http://127.0.0.1:8765
        reflex_laya_model: convaiinnovations/laya:typed-decisions
        reflex_shadow_async: true
```

Paired telemetry defaults to:

```text
$HERMES_HOME/reflex/shadow.jsonl
```

Only hashes of the redacted state/questions plus decision outputs are recorded; the complete decision state is not copied into the shadow log.

Use synchronous shadowing for controlled benchmark/replay runs:

```yaml
reflex_shadow_async: false
```

### `laya`

Laya is authoritative for semantic `DecisionEngine` calls.

```yaml
plugins:
  entries:
    nerve:
      settings:
        reflex_backend: laya
        reflex_laya_base_url: http://127.0.0.1:8765
        reflex_laya_model: convaiinnovations/laya:typed-decisions
```

No OpenRouter/TypeSafe/OpenCode credential is needed merely to construct/use the Laya backend.

## Start the Laya sidecar

Create a separate Python environment. The core plugin does not install Laya automatically.

```bash
python3 -m venv ~/.venvs/hermes-reflex-laya
~/.venvs/hermes-reflex-laya/bin/pip install --upgrade pip
~/.venvs/hermes-reflex-laya/bin/pip install 'laya==0.3.5'
```

Start the preloaded typed-decisions checkpoint:

```bash
cd /path/to/hermes-nerve-v0.2.2.dev15-final
~/.venvs/hermes-reflex-laya/bin/python -m hermes_nerve.reflex.laya_service \
  --device cuda \
  --host 127.0.0.1 \
  --port 8765 \
  --model convaiinnovations/laya \
  --subfolder typed-decisions
```

The model is loaded once when the sidecar starts. A request cannot choose an arbitrary Hub repository or trigger another model load.

### CPU fallback

Omit `--device cuda` or set `--device cpu` if CUDA is unavailable.

## Remote GPU host

The sidecar intentionally defaults to loopback. For a GPU machine reachable by SSH, keep the Laya service bound to that machine's loopback interface and forward it:

```bash
ssh -N -L 8765:127.0.0.1:8765 gpu-host
```

Hermes can continue using:

```text
http://127.0.0.1:8765
```

This avoids exposing an unauthenticated inference endpoint on the LAN/Tailscale interface.

If you intentionally expose the sidecar on a non-loopback interface, configure `HERMES_REFLEX_LAYA_TOKEN`; the server refuses a non-loopback bind without a bearer token. The Hermes client permits non-loopback targets only through `https://`.

## Smoke the sidecar

```bash
python3 scripts/check_laya_sidecar.py
```

With an auth token:

```bash
HERMES_REFLEX_LAYA_TOKEN='...' python3 scripts/check_laya_sidecar.py
```

The smoke verifies `/healthz`, one typed `choice` request, model/provider identity, and transport metadata.

## Provenance semantics

Jev decisions retain live-provider provenance.

Laya decisions are recorded as:

```json
{
  "provider": "Laya",
  "transport": "laya-local-http",
  "provenance_status": "LOCAL_ONLY",
  "execution": {
    "live_provider_call": false
  }
}
```

The HTTP hop to a local/private sidecar is not reported as a paid/live third-party provider call. This keeps provider-call/cost accounting truthful.

## Deterministic completion authority

Dev15b does not alter dev14's authority hierarchy:

```text
deterministic FAIL
   -> cannot be overwritten by Jev PASS
   -> cannot be overwritten by Laya PASS

all required deterministic criteria VERIFIED_PASS
   -> COMPLETE_READY
   -> native kanban_complete
   -> no semantic completion call required
```

The semantic backend is only used where the existing `DecisionEngine` was already allowed to make a semantic judgment.

## Shadow telemetry

`nerve_stats` has no `reflex` section; earlier versions of this guide referred to one that the tool never served. Read the paired shadow report locally instead, from the plugin directory and with the same `HERMES_HOME` the agent uses:

```bash
python3 -c 'import json; from hermes_nerve.reflex.telemetry import report; print(json.dumps(report(recent_limit=5), indent=2))'
```

The report reads `$HERMES_HOME/reflex/shadow.jsonl`, the default location when `reflex_shadow_log` is unset. It includes:

- shadow record count;
- paired answer count;
- agreement/disagreement count and rate;
- per-question agreement counts;
- shadow errors;
- bounded recent records.

Recent records and errors can contain endpoint URLs and pinned identities, so read the report locally rather than through a model tool result.

Shadow telemetry is measurement, not ground truth. A disagreement means only that Jev and Laya chose different answers.

## Model/checkpoint choice

Dev15b targets the dedicated typed-decisions checkpoint:

```text
convaiinnovations/laya : typed-decisions
```

The sidecar fixes its model at startup and reports that identity on every response. The Hermes client rejects a response whose model identity does not match `reflex_laya_model`.

## Configuration reference

New settings:

```text
reflex_backend                 jev | laya | shadow
reflex_laya_base_url           default http://127.0.0.1:8765
reflex_laya_model              default convaiinnovations/laya:typed-decisions
reflex_laya_timeout_seconds    default 5.0
reflex_laya_token              optional bearer token
reflex_shadow_async            default true
reflex_shadow_log              optional JSONL override
work_estimated_decision_call_tokens  provider-neutral ROI estimate
```

Legacy setting retained for migration:

```text
work_estimated_jev_call_tokens
```

If the new provider-neutral estimate is not set, the old Jev-named value remains accepted.

## Offline verification

```bash
python3 -m pytest -q
python3 -m compileall -q .
python3 scripts/verify_release.py
python3 scripts/verify_dev6_benchmark.py
```

The Laya integration tests use a real loopback HTTP server with a fake in-memory decision agent, so the protocol, auth, model pin, provenance, shadow telemetry, and failure behavior are testable without downloading model weights.

## Live acceptance for the dev15 merge

The following remain deliberate runtime gates for dev15 live deployment rather than being faked by offline packaging:

1. install `laya==0.3.5` in the sidecar environment;
2. load the real `typed-decisions` checkpoint;
3. pass `scripts/check_laya_sidecar.py`;
4. run a known dev14 benchmark in `reflex_backend=shadow`;
5. confirm Jev remains authoritative and Laya shadow errors are zero;
6. inspect agreement/calibration/outcome data;
7. only then test `reflex_backend=laya` as an opt-in primary;
8. re-run dev14 deterministic completion acceptance unchanged.

No community-catalog update is included in dev15.
