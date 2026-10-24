# Nerve 0.3.2.3: Local backend setup and real inference

The Control Panel separates HTTP health checks from actual model decisions.

## Beginner walkthrough

1. Run nerve ui (or nerve -p freebrain ui for a specific Hermes profile).
2. Open Connect Reflex and choose Liquid D1 or Laya.
3. Click Check service only. An HTTP health response is NOT a passing inference test.
4. Click Run real decision test. It submits one fixed non-private duplicate-charge question to POST /v1/systemone, validates the typed choice, and reports model, latency, and usage metadata. A valid typed response does not prove answer accuracy or independently attest model identity.
5. If no backend is running, click Review one-click setup. Inspect packages, disk space, Python compatibility, and third-party code/download warnings.
6. Click Install dependencies & start only after reviewing the confirmation. Setup runs asynchronously in an isolated venv and persists progress even if the Control Panel closes.
7. When ready, run your real decision test, apply the Nerve profile separately, and restart Hermes to load its configuration.

The Control Panel has a visual walkthrough at /guide.

## Status semantics

| Status | Evidence |
|---|---|
| Not tested | No model inference request |
| HTTP only | A local /healthz, /health, /v1/version, or /v1/models responds |
| Real inference passed | A genuine /v1/systemone POST returns a valid typed decision and model label |
| Inference failed | No valid typed answer, missing credentials, incorrect endpoint, or model not ready |

Backend, provider, local URL, shadow comparator and profile changes clear previously displayed results. Stale in-flight responses are ignored.

Hosted Jev via OpenRouter, TypeSafe or OpenCode may bill usage. Real hosted tests require explicit confirmation and use Hermes secret scope or process environment, without sending keys to the browser. Shadow mode tests Jev and the local comparator; both must pass.

## Liquid D1

Official source: https://huggingface.co/LiquidAI/d1-3B

One-click install uses a dedicated Python environment under HERMES_HOME/nerve/local-backends/d1/venv, installs transformers>=5.14,<6, torch, torchvision and pillow, downloads official LiquidAI/d1-3B weights, starts the loopback System One service at 127.0.0.1:8080 and runs genuine inference before reporting ready.

The model file is approximately 6.25 GB and torch/dependencies require additional disk. Unquantized CPU inference needs substantial RAM and may run slowly. The installation requires at least 16 GiB available disk; this is not a RAM or GPU guarantee.

CRITICAL: D1 model loading uses trust_remote_code=True, executing code supplied with the Hugging Face model. Review its LFM1.0 license and source code. The UI requests explicit download and remote-code consent. No sudo or system-wide installation is used.

If a compatible local service already occupies port 8080, use it. The one-click path refuses to overwrite or stop existing services. SSH-forwarded remote GPU models appear as loopback connections and can be tested without downloading local weights.

## Laya

Official source: https://huggingface.co/convaiinnovations/laya-typed-decisions

One-click install uses HERMES_HOME/nerve/local-backends/laya/venv, installs pinned laya==0.3.29 via PyPI, launches the local server on 127.0.0.1:8765, fetches the model on first launch and runs genuine inference. The checkpoint/dependency download size varies by platform.

## OpenJev and Generic System One

OpenJev requires a separate hardware-specific setup (often vLLM and substantial GPU memory). It is NOT automatically downloaded through this wizard. Follow https://huggingface.co/openjev/openjev/blob/main/serve/SERVE.md. Expose its helper on an explicit HTTP loopback address with POST /v1/systemone, default 127.0.0.1:3000, then run the real test.

Generic System One is a protocol, not an installable model. Bring your own compatible local server and test the typed response.

## Managing installations

Each managed backend has:
- HERMES_HOME/nerve/local-backends/<backend>/venv (isolated packages)
- status.json (phase and managed-process identity)
- install.log (dependency installation errors)
- server.log (model startup/inference errors)

Only an explicitly reviewed Install dependencies & start action initiates downloads. The user can stop a managed model through the UI; only the exact verified Linux process owned by the user may be stopped, never unrelated or external services.

The server binds to loopback, requires a random session token, enforces same-origin writes, and disables proxies and redirects for local checks. It is not a public web service.

Existing legacy profile settings and provider credentials are preserved. The wizard avoids copying Nerve onto the source directory from which it is running.

## Regression tests

Run node --check hermes_nerve/web/app.js.

Run python3 -m unittest tests.test_control_panel tests.test_control_panel_real tests.test_profiles tests.test_profile_capability_matrix tests.test_profile_migration -q.

For all tests run python3 -m unittest discover -s tests -p 'test_*.py' -q.

CI uses a local HTTP fixture for real System One POST contract testing and mocks PyPI/Hugging Face downloads and hosted Jev. Passing CI is not equivalent to testing large model inference on your hardware. A genuine local sidecar acceptance pass requires the Run real decision test action against a running model.
