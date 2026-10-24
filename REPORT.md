# Nerve 0.4.12 — Unified Profile & Configuration Repair

**Candidate:** `release/v0.4.11-config-repair` branch, release identity versioned **0.4.12** (from installed `a040a67` 0.4.11)
**Date:** 2026-10-23 · **Status: DONE — tested upgrade candidate, NOT deployed**

## What changed (product, 4 files)

| File | Change |
|---|---|
| `hermes_nerve/web/app.js` | Removed the hardcoded 11-module `visibleSimple` set. Feature visibility is now server-driven (`status().views`): per-profile primary modules + "available with setup" lists, per-module **Why** reasons, dependency-pruning notices, honest Configured/Effective/Running/Restart states strip (profiles/features/review pages), legacy explicit-migration button. |
| `hermes_nerve/web/styles.css` | `.states-strip` styling. |
| `hermes_nerve/control_panel.py` | New `profile_views()` (server-computed per-profile views; no JS rules) and `config_states()` (Configured/Effective/Running/Pending-restart; Running only claimed inside the Hermes host, `pending_restart` from sidecar-mtime vs host load; unavailable ⇒ explicit message). `status()` exposes views+states; `preview()` exposes full reasons + states. |
| `hermes_nerve/cli.py` | `nerve setup --show` prints the same states; CLI is a separate process ⇒ Running honestly "verification unavailable", Pending "unknown (query the in-Hermes panel)". Per-module specific reasons in the module list. |

**No defaults were changed.** Phase-6 audit (locked by `test_profile_defaults_matrix.py`):
canonical sets already match profile roles — evidence/completion modules (receipts,
verified_experience) ON everywhere; shadow_testing/shared_context never default-ON;
fat_cat's remote_workers default is resolver-pruned without `remote_hosts` with the
reason surfaced. Resolver remains the single authority behind CLI and Panel.

## Tests

- **Full suite: 1338 passed, 0 failed, 6 skipped** (baseline 1315/0/6 + 23 new; release-identity pins updated to 0.4.12 where they assert the real version: `_version.py`, `plugin.yaml`, catalog yaml, `scripts/verify_release.py`, egg-info metadata).
- New permanent tests:
  - `test_profile_views_states.py` — views contract per profile, no-hardcoded-JS-rules guard, states honesty (standalone unavailable, pending-restart flip via mtime, fake-host in-process path), preview reasons, corrupt-sidecar loud refusal.
  - `test_cli_panel_equivalence.py` — CLI ⇄ Panel equivalence (select/override/reset), override preservation on same-profile reselect, backup + restart flag on apply.
  - `test_profile_defaults_matrix.py` — Phase-6 matrix with invariants.
  - `test_control_panel_browser.py` — **real browser** (headless Chrome, dependency-free stdlib CDP/WebSocket client) against the *served* panel: states strip honesty, fat_cat view + `Why: no remote_hosts configured` + OFF switch, explicit-override why, advanced = all 18 modules, marie_kondo distinct minimal view, screenshot evidence.
  - `test_module_absence.py` + profile-switch activation test (operator → fat_cat → operator re-registration changes the feature surface via the sidecar).
- Existing profile/panel/setup/absence suites unchanged and green.

## Candidate artifacts (`artifacts/`)

- `hermes_nerve-0.4.12-py3-none-any.whl` (sha256 `9b34eb88d8de1d691f32a94bba2cbbecedf092ba9a6132c3120e3530a373665e`)
- `native-source/` — 147 files; product diff vs deployed release = the 4 files above + version identity (`_version.py`, `plugin.yaml`, catalog yaml, `scripts/verify_release.py`); payload sha256 `1e0e0e247e211aaa8919412b93694167cce4788987e926f470c28f9497a95181`
- `validation.json` — **stock Hermes validator on the 0.4.12 payload: 15/15 checks, ok:true, 0 critical findings**
- `profile-inventory.json` — all 36 registered profiles: 33 legacy (no sidecar, untouched), 3 fat_cat (`freebrain`, `nerve-ab-apply`, `nerve-ab-stock`); 0 errors, 0 overrides lost
- `../browser-evidence/browser-panel-marie-kondo.png`

## Not done (by design)

- **Not deployed.** The installed 0.4.11 (4236aa2c) is untouched; deploying the candidate is a separate owner decision. Contract honored: `--force` never used; no DANGEROUS verdicts encountered; no Hermes core changes; nothing published.

## Acceptance criteria

1. Distinct defaults per profile ✓ (matrix test + browser views)
2. Switching visibly changes configuration ✓ (browser + activation tests)
3. All 18 modules accessible ✓ (advanced view asserted = 18 in real browser)
4. `nerve setup` ⇄ Control Panel agree ✓ (equivalence tests, both directions)
5. Saved vs runtime not confused ✓ (states contract; no fake running-claims)
6. Overrides survive reselection ✓
7. Reset restores canonical defaults ✓
8. No silent setting/security loss ✓ (inventory: 36/36 clean; corrupt config refused loudly)
9. Real browser + integration tests pass ✓
