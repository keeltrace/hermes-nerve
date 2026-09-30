# Dev17 — open-source/open-weight Reflex validation

Dev17 is a release-hardening build. It preserves dev16 controller-owned completion while applying one authority correction discovered by the live dev17 matrix: token budget is execution policy, not correctness DoD, and Reflex/Nerve is a watchdog rather than the final kill authority.

## Release question

Hold the worker model, frozen event-delivery task, DoD, Nerve policy, and Hermes runtime constant. Change only the Reflex backend:

- `jev` — hosted Jev reference path;
- `laya` — local `convaiinnovations/laya-typed-decisions` sidecar;
- `openjev` — self-hosted `openjev/openjev` helper exposing `/v1/systemone`.

The matrix is not another plugin-vs-no-plugin benchmark. Dev16 already established that control tail-risk. Dev17 asks whether the open backends preserve correctness, completion authority, token containment, and zero-post-PASS behavior.

## Dev16 live-test fixes carried into dev17

- Accept both Hermes successful task terminal values: `done` and `completed`.
- Persist result rows after every arm; interrupted runs remain analyzable.
- Use process-safe Python cleanup rather than shell trap variables that can be clobbered by loop variables.
- Reap a worker that remains alive after the board reaches a terminal state.
- Keep the independent harness emergency primary[REDACTED] separate from Nerve orchestrator-review handoffs.
- Classify outcomes as `SUCCESS`, `VERIFICATION_FAIL`, `IMPLEMENTATION_FAIL`, `LIFECYCLE_FAIL`, `ORCH_REVIEW`, legacy `NERVE_KILL`, or `HARNESS_KILL`.
- Emit success rate, median/max combined tokens, and matched backend comparisons rather than relying on averages.
- The offline release verifier explicitly marks fake headless binding as not-applicable instead of producing a live-run warning.

## Laya

Current dev17 default:

```text
convaiinnovations/laya-typed-decisions
```

The Hermes plugin remains dependency-free. The sidecar venv pins the current published `laya==0.3.3` package and keeps the model resident. The setup script first uses a working stdlib `venv`; if the host lacks `ensurepip`/`python3-venv`, it uses an already-installed trusted `uv` executable (or `UV_BIN`) to provision Python 3.12. It deliberately does not download and execute an installer automatically.

```bash
bash scripts/setup_laya_dev17.sh
USE_TF=0 ~/.venvs/hermes-reflex-laya/bin/python -m hermes_nerve.reflex.laya_service \
  --device cuda --host 127.0.0.1 --port 8765 --model convaiinnovations/laya-typed-decisions
python3 scripts/check_laya_sidecar.py
```

If the default port is already occupied, pick another (for example `8766`) consistently for the sidecar and tunnel. For a GPU on another host, bind Laya to that host's loopback and SSH-forward it:

```bash
ssh -N -L 8765:127.0.0.1:8765 gpu-host
```

Laya weights are Apache-2.0. Do not silently route to the typed-decisions specialist outside the workflows you intend to test; dev17 selects it explicitly for reproducibility.

## OpenJev

OpenJev uses a Jev-compatible `/v1/systemone` request/response surface. The helper's response `model` field is a detailed served identity, not the free request label, so dev17 can optionally pin an expected identity substring rather than incorrectly requiring `response.model == "openjev"`.

The setup script fails closed before downloading weights if no NVIDIA GPU can be probed or if the largest detected GPU has under 30 GB VRAM. It recognizes ordinary `nvidia-smi` and the WSL NVIDIA path. Operators can override those guards only for explicitly unsupported experiments.

Published primary serving recipe:

- `openjev/openjev`, 27B;
- vLLM 0.29.0;
- online FP8 quantization;
- measured on one H100;
- BF16 repository is about 54 GB; published FP8 checkpoint is about 29 GB.

On a suitable GPU host:

```bash
bash scripts/setup_openjev_dev17.sh
~/models/openjev/start-hermes-openjev.sh
```

Keep both vLLM and the helper on loopback. From the Hermes host:

```bash
ssh -N -L 3000:127.0.0.1:3000 openjev-gpu-host
python3 scripts/check_openjev_sidecar.py
```

If the remote helper itself is intentionally exposed rather than tunneled, use its `SHIM_TOKEN` and TLS; do not expose the unauthenticated vLLM backend.

OpenJev weights are CC BY-NC 4.0. The helper/serve code is Apache-2.0. Nerve dev17 ships integration code only, not model weights.

## Configure a profile

```bash
~/.hermes/hermes-agent/venv/bin/python scripts/configure_reflex_profile.py PROFILE --backend laya
~/.hermes/hermes-agent/venv/bin/python scripts/configure_reflex_profile.py PROFILE --backend openjev
```

The script writes `plugins.entries.nerve.settings`: `--backend` plus only the options you pass. Settings you omit keep their current values. Settings with no value anywhere use Nerve's defaults and `HERMES_REFLEX_*` environment fallbacks when the plugin loads. Pass an option explicitly, for example `--laya-base-url` or `--openjev-expected-identity`, to change it.

Shadow mode keeps hosted Jev authoritative while recording a selected open backend:

```bash
~/.hermes/hermes-agent/venv/bin/python scripts/configure_reflex_profile.py PROFILE \
  --backend shadow --shadow-backend openjev --shadow-sync
```

## Release matrix

Create/install a clean dev17 source profile first, then:

```bash
python3 scripts/run_dev17_model_matrix.py \
  --source-profile abtest-jev-dev17 \
  --pairs 2 \
  --arms jev,laya,openjev
```

The runner rotates arm order across pairs. Each arm gets a fresh profile, board, repository and task, while the fixture/worker/DoD policy stay fixed.

Per-arm release gates:

1. terminal task/run state is `done` or `completed`;
2. full visible suite passes;
3. hidden acceptance passes;
4. Git is clean;
5. no external emergency stop;
6. zero post-PASS worker calls is expected for a clean controller-completion path;
7. any Nerve orchestrator-review handoff is reported separately with its YES/NO/MAYBE forecast and granted extension; legacy pre-RC Nerve kills remain visible only as historical evidence.

Recommended push gate:

- all hosted-Jev arms valid;
- all Laya arms valid;
- all OpenJev arms valid;
- zero Reflex-owned economic kills; orchestrator review owns the final stop decision;
- no open backend introduces a lifecycle failure;
- model-specific token/latency differences are reported, not hidden by aggregate averages.

## RC budget-authority policy

The first dev17 Jev/Laya matrix exposed a policy trap: correct, test-green work could exceed the required `DOD-BUDGET`, making completion impossible and eventually forcing a Nerve kill. The RC therefore separates correctness from spending authority.

Default flow:

```text
65% base target -> WATCH
80%             -> Reflex forecast: will +25% likely finish? YES / NO / MAYBE
YES >= 0.60     -> grant one +25% extension; no scope expansion
MAYBE           -> canonical kanban_request_review immediately
NO              -> checkpoint-only window; canonical review by 90% base consumption
extension spent -> canonical kanban_request_review immediately
1.75x emergency -> canonical review/pause if earlier routing somehow failed
```

`kanban_request_review` is the authority transfer. The configured main reviewer/orchestrator then chooses canonical completion, changes/replan, or block. Nerve never dispatches `kanban_block` for economic reasons.