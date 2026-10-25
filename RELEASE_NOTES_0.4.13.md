# Nerve 0.4.13 — Release Notes

**Version:** 0.4.13 (fleet-deployed 2026-10-24)
**Payload:** git `9be8c22983a1ae2a5d375b9dd007cf26a1a0f451` (parent `b4acb54` = 0.4.12)
**License:** MIT · **Requires:** Hermes Agent (plugin API current as of 0.4.13)

Nerve is a supervisory layer for Hermes: reflex review, nervous-system turn
admission, receipts, shadow testing, token trajectory, work supervision, a
local learning loop, a web control panel, and optional remote-worker
delegation. Profiles: Legacy, Lean, Operator, Marie Kondo, Fat Cat, Full
Configuration (custom).

## What's new since 0.4.11

### 0.4.12 — unified profile/config repair
- Single source of truth for resolved configuration: Nerve profile sidecar
  (`nerve/profile.json`) and Hermes `config.yaml` settings merge through one
  resolver (`config_resolver.py`) used identically by the CLI, the panel, and
  the runtime.
- Control panel rewritten to be server-driven: panel state always reflects the
  live resolved configuration; module switches, profile selection, preview and
  apply/reset flows share the same authority as the CLI.
- Panel served from the plugin web assets with cache-busting; stale UI no
  longer shows ghost state after upgrades.

### 0.4.13 — sidecar authority + save-path + honest UI
- **Sidecar is authoritative** when present: a stale `nerve_profile` pin in
  Hermes `config.yaml` is surfaced as an explicit conflict instead of silently
  overriding the saved profile.
- **Panel save unblocked:** `preview()` no longer throws on a conflicting
  config pin; conflicts are reported and the save proceeds with the resolved
  state.
- **CLI↔panel parity:** `nerve setup` now reads Hermes config.yaml through the
  same shared loader (`host_settings.py`) as the panel; both show the identical
  effective configuration and the same conflict note.
- **Honest switch state:** module cards show the saved choice and the actual
  runtime reason for the effective state — no more false "disabled by
  dependency pruning" when the real cause was a config-store conflict.
- 14 new regression tests covering sidecar authority, conflict surfacing,
  shared host settings, and panel save/preview behavior.

## Quality gates (all green before release)

- Full test suite: **1,351 passed, 0 failed, 6 skipped** (277 s).
- Stock Hermes plugin validator: **15/15 checks pass** on the candidate.
- Release verifier (`scripts/verify_release.py`): **PASS version=0.4.13** —
  tool catalogs, 9 hook names, single authority, controller completion, and
  reflex backends (laya/openjev/systemone/d1) all asserted; profiles
  REGISTERED_AND_ASSERTED.
- Fresh-install smoke on a disposable profile: official installer → registered
  `enabled 0.4.13` → `nerve setup --show` clean in a virgin home.
- Fleet proof: deployed and verified across 28 local Hermes profiles
  (driver-verified install hashes, single registration, security probes,
  enabled-state preservation, auto-rollback guard).

## Security disclosure (read before installing)

The stock Hermes security scanner flags **3 critical findings** on the install
path, all in `hermes_nerve/risk.py`:

- `sudoers_mod` — Nerve's work-supervision can request sudoers edits through
  its gated action path.
- `system_passwd_access` — references to system credential stores in the
  guarded filesystem surface.
- `format_filesystem` — unbounded filesystem formatting capability behind the
  action gate.

These are **deliberate supervisory capabilities**, not injection bugs. They sit
behind Nerve's own action gate (HITL/approval modes) and are documented in the
security-validation reconciliation report shipped in the repo. Installing with
the stock scanner requires the documented `--force` confirmation or opting out
of `plugins.scan_on_install` — review the findings yourself and install only
if you accept them. Nerve is local-first: no telemetry leaves the machine.

## Artifacts

| Artifact | Contents |
|---|---|
| `hermes_nerve-0.4.13-py3-none-any.whl` | wheel |
| `hermes_nerve-0.4.13.tar.gz` | sdist |
| `nerve-0.4.13-plugin.zip` | plugin payload (git-archive of `9be8c22`) |
| `nerve-payload-0.4.13.bundle` | full payload git history for offline install |

Install (git payload, official installer):

```
hermes plugins install file:///path/to/nerve-payload-0.4.13.bundle-clone \
  --ref 9be8c22983a1ae2a5d375b9dd007cf26a1a0f451 --yes-deps --force
hermes plugins enable nerve
```

## Upgrade notes

- Upgrades preserve enabled-state, config.yaml settings, and the profile
  sidecar; the 0.4.13 resolver reconciles them and reports any conflict in the
  panel and CLI instead of guessing.
- If your panel shows a "config pin ignored" note, your `config.yaml` still
  carries an old `nerve_profile:` pin — Nerve now prefers your saved sidecar
  choice and tells you. Remove the pin to silence the note.

— KeelTrace community
