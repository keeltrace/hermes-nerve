# Liquid D1 and generic System One integration

This development line adds a provider-neutral HTTP seam for native decision models that expose the TypeSafe-compatible `POST /v1/systemone` contract. Liquid `d1-3B` is a preset over that seam rather than a special Nerve runtime.

## Authority stays unchanged

D1 does not become the Hermes orchestrator and does not own canonical Kanban state. Nerve still collects evidence, tracks the locked Definition of Done, forecasts trajectory, and emits bounded supervisory decisions. Hermes owns the final stop/continue/review decision and canonical task lifecycle.

The intended adoption path is:

```text
Hermes worker
  -> deterministic Nerve checks
  -> Jev authoritative + D1 shadow
  -> compare decisions / calibration / latency on real Nerve traffic
  -> optionally promote D1 to local Reflex backend after acceptance gates
  -> Hermes remains final authority
```

## Backends

`reflex_backend` now accepts `jev`, `laya`, `openjev`, `systemone`, `d1`, or `shadow`. `reflex_shadow_backend` accepts `laya`, `openjev`, `systemone`, or `d1`.

The core plugin remains dependency-free. Model runtimes live outside Hermes, normally in a local sidecar such as llama.cpp.

## D1 shadow first

For the first live comparison, keep Jev authoritative:

```yaml
plugins:
  entries:
    nerve:
      settings:
        reflex_backend: shadow
        reflex_shadow_backend: d1
        reflex_d1_base_url: http://127.0.0.1:8080
        reflex_d1_model: LiquidAI/d1-3B
        reflex_shadow_async: true
```

The shadow request is fail-open and does not change the authoritative Jev answer. Existing shadow telemetry records provider/model/latency, answer probabilities, agreement, and errors without storing the raw state.

After measured acceptance, D1 can be selected directly with `reflex_backend: d1`. Local D1 results use Nerve `LOCAL_ONLY` provenance and do not count as hosted provider calls.

## Generic System One

A custom native decision model can use `reflex_backend: systemone` with `reflex_systemone_base_url`, `reflex_systemone_path`, `reflex_systemone_model`, and `reflex_systemone_provider`. Leave the model blank when a single-model server does not require a request model label.

Plain HTTP is accepted only on loopback. A non-loopback endpoint must use HTTPS; an SSH tunnel is the preferred remote-GPU path.

## Multimodal decisions

The generic provider and D1 preset carry `images` as data URLs. `DecisionEngine.assess`, `decide`, `rank`, and `verify` accept optional `images=` input and forward it only to providers that explicitly advertise image support.

```python
engine.decide(
    state={"action": "click Delete"},
    instructions="Choose the required control action.",
    choices=["ALLOW", "APPROVAL", "BLOCK"],
    images=["data:image/png;base64,..."],
)
```

This enables independent visual supervision of browser/computer-use flows, rendered CAD, confirmation dialogs, and other UI state while retaining the same typed decision and authority boundary.

## Smoke check

```bash
python scripts/check_systemone_sidecar.py --d1 --base-url http://127.0.0.1:8080 --model LiquidAI/d1-3B
```

The probe makes one `noul` request and prints the served model, transport, latency, answer, and usage.

## Matrix

The backend matrix accepts D1:

```bash
python scripts/run_dev17_model_matrix.py --arms jev,d1 --pairs 2 --d1-url http://127.0.0.1:8080 --d1-model LiquidAI/d1-3B
```

Do not promote D1 on generic benchmark scores alone. Nerve acceptance should be based on its own labeled or replayed workload, especially trajectory decisions, completion verdicts, gate false-ALLOW rate, calibration, disagreement rate, p50/p95 latency, and combined supervisor cost.

Decision probabilities are model outputs, not guaranteed calibration for Nerve distribution. Existing Nerve confidence/probability thresholds must be revalidated before D1 is allowed to make consequential enforce-mode decisions.
