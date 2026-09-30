# Nerve

Nerve is an asynchronous System-1 supervisory layer for Hermes Agent. `0.3.1rc1` is a maintenance release candidate for the profile-aware `0.3.0` line: bug fixes, hardening, and QOL improvements only. It preserves the existing profile/module surface and does not add the pending Shared Context publication, policy-memory, Deep Research Kit, or ask-only gate features. Nerve/Reflex remains a watchdog and forecaster; the main Hermes orchestrator/reviewer owns final stop/continue authority. The core plugin remains dependency-free and model runtimes stay in sidecars.


## Dev17 — open backend release matrix

Dev17 adds first-class `reflex_backend=openjev`, generalizes Jev-authoritative shadowing to either Laya or OpenJev, updates the Laya sidecar to the current standalone typed-decisions checkpoint, and packages a crash-safe Jev/Laya/OpenJev matrix runner. It also carries forward the live dev16 benchmark fixes: `completed` is a successful terminal state, results flush after every arm, lingering workers are reaped, external emergency stops remain distinct from Nerve orchestrator-review handoffs, and final summaries survive cleanup errors. See [`docs/DEV17_OPEN_SOURCE_VALIDATION.md`](docs/DEV17_OPEN_SOURCE_VALIDATION.md).

## Dev15b — Reflex + Laya integration

Dev15b introduces a provider-neutral backend seam below `DecisionEngine` without changing the existing Kanban authority model. `reflex_backend=jev` preserves current behavior, `reflex_backend=shadow` keeps Jev authoritative while logging paired Laya decisions, and `reflex_backend=laya` selects the local Laya sidecar for semantic decisions. Laya receipts are correctly marked `LOCAL_ONLY`, and shadow failures never change the authoritative result.

The plugin does not import torch or transformers. The optional Laya runtime lives in a separate process started with `python -m hermes_nerve.reflex.laya_service`, preloading `convaiinnovations/laya` / `typed-decisions` once. See [`docs/DEV15B_LAYA_INTEGRATION.md`](docs/DEV15B_LAYA_INTEGRATION.md) for installation, SSH tunneling, configuration, telemetry, and live-acceptance gates.

## Dev14 controller-owned DOD-07 proof

Dev14 closes the live dev13 gap without weakening the deterministic completion gate. In the dev13 smoke, the worker reached a clean commit and a green 26-test visible suite, but direct repeated dispatch of an already-dead event still mutated attempts from 2 to 3. Dev13 correctly held DOD-07 at deterministic FAIL, but its evidence path still depended on worker-authored focused-test names and did not surface the concrete counterexample early enough.

Dev14 executes the DOD-07 semantics itself inside the controller. The probe is in-memory, network-free, and does not modify the workspace. It proves: dead-event redispatch is stable, delivered-event redispatch does not redeliver, transient retry→success→duplicate is stable, and retry-queue work remains unique. When a probe fails, the exact observation is placed in the RETRY directive (for example, `dead-event redispatch changed attempts 2->3`).