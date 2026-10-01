# Nerve development setup (v0.3.2.dev0)

## Install

Install the plugin with the normal Hermes plugin flow, then validate registration:

```bash
hermes plugins doctor . --ci
```

Expected Legacy public surface: 16 tools, 9 hook names, and the optional `jev` ContextEngine. Named profiles intentionally register smaller module-specific surfaces.

## Where Nerve settings live

Hermes gives a plugin only the settings in its own entry, `plugins.entries.<plugin id>`, and Nerve's plugin id is `nerve`. Within that entry, Hermes reads `settings` first and falls back to the older `config` subtree. Every `hermes config set` command below writes to `plugins.entries.nerve.settings`.

Earlier docs wrote `plugins.entries.hermes-nerve.settings.*`. Hermes never reads that block, so those settings silently had no effect: for example, `reflex_backend: laya` left decisions on the default `jev` backend. Nerve now logs a warning at startup when the old block is present. Move its keys under `plugins.entries.nerve.settings`, then remove it with `hermes config unset plugins.entries.hermes-nerve`.

## Provider

OpenRouter (live-tested historically in v0.1.x):

```bash
export OPENROUTER_API_KEY='...'
hermes config set plugins.entries.nerve.settings.jev_provider openrouter --force
hermes config set plugins.entries.nerve.settings.jev_model typesafe/jev-1.13 --force
```

Direct TypeSafe (wire-tested in v0.2.1.2; independently live-smoked on v0.2.1.1):

```bash
export TYPESAFE_API_KEY='...'
hermes config set plugins.entries.nerve.settings.jev_provider typesafe --force
hermes config set plugins.entries.nerve.settings.typesafe_model jev-latest --force
```

OpenCode Zen:

```bash
export OPENCODE_API_KEY='...'
hermes config set plugins.entries.nerve.settings.jev_provider opencode --force
hermes config set plugins.entries.nerve.settings.opencode_model jev-1.13 --force
```

OpenCode access in Nerve is paid-only. The `jev-1.13-free` tier is not supported because it does not work with Hermes.

Only the selected provider's credential is required.

## Nervous system defaults

```bash
hermes config set plugins.entries.nerve.settings.nervous_enabled true --force
hermes config set plugins.entries.nerve.settings.nervous_turn_admission true --force
hermes config set plugins.entries.nerve.settings.nervous_mode correct_next --force
hermes config set plugins.entries.nerve.settings.nervous_challenge_confidence 0.86 --force
hermes config set plugins.entries.nerve.settings.nervous_call_threshold 0.58 --force
```

The old synchronous pre-tool gate remains `off` by default. It is compatibility/special-purpose behavior, not the recommended v0.2 decision architecture.

## Manual structured events

Hermes/runtime integrations may call `nerve_nervous_event` to expose explicit accountable decisions. The event can include choices and `hermes_decision`, or a control-state event such as `RECOVERY`, `STRATEGY_CHANGE`, or `COMPLETION_CANDIDATE`.

## Telemetry

Inside Hermes:

```text
Call nerve_stats and return the nervous section.
```

Local files are profile-scoped under `$HERMES_HOME/jev/`, including nervous-event and decision-outcome JSONL ledgers.

## Personality/module setup

The new profile layer is deliberately separate from existing provider credentials and low-level tuning.

```bash
nerve setup --show
nerve setup --profile lean
nerve setup --profile operator
nerve setup --profile fat_cat
nerve setup --profile marie_kondo
```

Bare `nerve setup` opens the five-choice interactive selector. Full Configuration allows module-by-module selection and then offers an `Advanced configuration? [y/N]` editor. The advanced editor is keyed from the plugin manifest: use `list` to inspect setting names, select a setting to write a typed override, `clear <name>` to remove one, and blank input to save. The sidecar is written atomically under the active Hermes home at:

```text
$HERMES_HOME/nerve/profile.json
```

If the sidecar is absent and no `nerve_profile` plugin setting exists, Nerve preserves v0.2.3-compatible Legacy registration behavior.

The module layer is resolved before plugin registration. A disabled module does not register its owned Hermes tool schemas or hooks. Important initial profile choices are:

- **Fat Cat:** Assistant + context/QoL features; Shared Context ON; remote workers only when hosts are configured.
- **Operator:** direct interactive Hermes; context/action supervision ON; Kanban/Assistant/remote-worker features OFF by default; Shared Context ON.
- **Lean:** work supervision + token trajectory + nervous/reflex core; QoL/context/remote/Assistant/Shared Context OFF pending evidence. Lean also avoids provider-based turn admission by default, caps nervous provider calls at 12 per turn, uses smaller nervous event windows, and exposes only the compact `nerve_decide` manual Reflex surface; explicit advanced settings can override the runtime limits.
- **Marie Kondo:** Reflex + evidence/DoD/work completion + minimum receipts; most other modules OFF.

### Shared Context

Nerve only manages the external HermesContextBus plugin boundary.

```bash
nerve setup --explain
nerve setup --install-shared-context /path/to/HermesContextBus
```

After installation, run the printed Hermes Plugin Doctor command. Nerve does not integrate or own the WhatsApp bridge.

### Assistant module

When `assistant_loops` is enabled, Nerve reuses the already-declared `nerve_nervous_event` transport for `assistant.status`, `assistant.add_loop`, `assistant.update_loop`, `assistant.complete`, and `assistant.drop_loop`. There is no extra Assistant tool schema, and the model cannot install or disable its own accountability. Operator-only install/disable controls live under `nerve setup`. The `assistant_audit` module adds recurring Reflex accountability advice; turning audit off preserves persistent loops without per-turn audit calls.

### Profile reset and compatibility semantics

`nerve setup --profile <name>` selects a profile and preserves explicit module **and advanced** overrides when that same profile is already selected. `nerve setup --reset <name>` deliberately reapplies that profile's canonical defaults and clears both module and advanced overrides. Profile selection does not persist an Assistant enable/disable override; only `--assistant-install` / `--assistant-disable` own that operator-level state.

Legacy remains a compatibility mode: existing v0.2.3 advanced settings such as an explicit `reflex_backend: shadow` are honored exactly. Named profiles treat shadow execution as an explicit `shadow_testing` module cost and force the normal Reflex backend when that module is off.

Invalid or unsupported profile-sidecar versions are never guessed or auto-migrated. Nerve attempts the `.json.bak`; if neither current-format document validates, it logs a warning and fails open to Legacy while leaving the files intact.
