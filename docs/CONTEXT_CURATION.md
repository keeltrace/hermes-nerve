# Jev context-value governor — v0.1.5.5

## Objective

The feature is not optimized for "delete the most tokens." Its objective is to maintain the smallest sufficient, highest-signal working context that still lets Hermes continue the task correctly.

Jev supplies uncertain semantic judgments. Deterministic local code owns recoverability, hard retention rules, lifecycle leases, and the final action.

## Pipeline

```text
ordered evidence
  -> deterministic metadata/recoverability
  -> bounded redacted previews
  -> Jev semantic noul questions
  -> local policy
  -> KEEP_EXACT | PIN | ANCHOR | DROP
  -> optional local REHYDRATE
```

For each eligible item Jev estimates:

1. `needed_again`
2. `exact_required`
3. `superseded`
4. `conflict`

Four candidates fit in one Jev request because the Decisions API accepts up to 16 typed questions.

## Deterministic actions

### KEEP_EXACT

Original content is retained. User/assistant text is always protected from model-written replacement.

### PIN

Original content is retained because a lifecycle condition is active, the caller explicitly pinned it, it is in a protected tail, or Jev detects a likely unresolved contradiction.

Built-in lease behavior:

- user/assistant text: `task_lifetime`
- nonrecoverable failure evidence: `until_verification_pass`
- caller override: `metadata.lease`

A real `nerve_verify` result updates the lifecycle state. `PASS` releases `until_verification_pass` for later curation.

### ANCHOR

Bulk content is replaced with a deterministic anchor containing:

- stable evidence id
- kind
- original character count
- content hash prefix
- deterministic recoverability flag
- tool name when known
- recovery pointer
- bounded original-content prefix

No model-generated summary is inserted.

### DROP

Explicit `nerve_context_curate` may omit an item only when local code already marks it recoverable and policy thresholds indicate low future need, low exactness need, high supersession, and no material unresolved conflict.

The automatic `NerveContextEngine` does **not** physically remove tool-result messages. A DROP proposal becomes a minimal anchor so OpenAI-format assistant tool calls retain their required tool-result partner.

### REHYDRATE

`nerve_context_rehydrate` looks up the evidence id in the local evidence ledger. It does not call Jev or OpenRouter.

Rehydration works only when `context_ledger_detail=sanitized`; hash mode intentionally lacks content to restore.

## Shadow mode

`mode=shadow` returns all original evidence while recording each `proposed_action` and proposed savings. Use it to tune policy against real work before enabling destructive apply behavior.

```bash
python3 scripts/context_shadow_report.py
python3 scripts/jev_report.py
```

Useful metrics include:

- proposed action counts
- proposed saved characters
- compacted unique evidence
- rehydrated compacted evidence
- recovery-demand rate

Recovery demand is not automatically a failure: an anchor can be working correctly if it is later rehydrated cheaply. A true false-forget analysis also considers task outcome and whether rehydration/re-execution caused harmful delay or loss.

## ContextEngine integration

Current Hermes exposes a public `ContextEngine` ABC and `ctx.register_context_engine()`. Nerve registers an engine named `jev`, but Hermes does not activate plugin engines automatically.

Activation is explicit:

```bash
hermes config set context.engine jev
```

`jev` is the engine's registered name. It is unrelated to the `jev` Reflex decision backend, which only `reflex_backend` selects: with `context.engine: jev` and `reflex_backend: laya`, curation decisions go to Laya.

Do not set `context.engine: nerve`. That value names the plugin directory, so Hermes' context-engine directory loader imports the plugin without access to its settings. Nerve now refuses that path and logs a warning pointing to `jev`; Hermes then falls back to its built-in compressor. Before that change, the loader silently built an unconfigured engine that ignored every Nerve setting.

While `jev` is the active engine, Hermes no longer uses the settings that tune its built-in compressor, such as `compression.threshold`, `compression.protect_first_n`, `compression.protect_last_n` and `compression.target_ratio`. Agent-level settings still apply: `compression.enabled` still decides whether compaction runs at all, and `compression.model_thresholds` still reaches the engine. Nerve's own settings decide when and how it compacts: `context_engine_threshold_percent` (default `0.72`, clamped to `0.30`–`0.95`), `context_engine_protect_first_n`, and `context_engine_protect_last_n` (default `6`; the built-in fallback keeps at least 6).

Modes (`context_engine_mode`; any value other than `apply` or `shadow` falls back to `shadow`):

- `shadow`: builds non-mutating Jev plans while delegating actual pressure-triggered compaction to Hermes' built-in compressor when fallback is available.
- `apply`: at the configured threshold, Jev evaluates eligible old tool results and anchors safe candidates.

`select_context()` intentionally remains a no-op in v0.1.5.5 to avoid changing the prompt-cache prefix every turn. Curation happens at compaction boundaries.

### Built-in fallback

If no eligible tool-result evidence exists, or Jev safely decides to reclaim nothing, the Jev engine can delegate that boundary to Hermes' built-in `ContextCompressor`. This prevents text-heavy conversations from getting stuck behind an engine that only knows how to govern evidence.

Disable only for experiments:

```bash
hermes config set plugins.entries.nerve.settings.context_engine_fallback_builtin false --force
```

## Policy defaults

```text
drop_max_needed       0.20
drop_max_exact        0.20
drop_min_superseded   0.75
anchor_max_needed     0.55
anchor_max_exact      0.45
conflict_pin_min      0.70
```

These are policy defaults, not claims of calibrated optimality. Shadow telemetry and continuation-fidelity evaluation should drive changes.

## Invariants

- No user/assistant text is generated into a summary by this feature.
- Unrecoverable evidence is never DROPped by deterministic policy.
- Active unresolved conflict tends toward PIN.
- Nonrecoverable failure evidence is leased until verification PASS.
- Automatic tool-result compaction preserves tool-call/result protocol structure.
- Jev sees redacted, bounded previews rather than unrestricted raw evidence.
- Local recovery never masquerades as a live provider call.

## Profile-aware telemetry (0.1.5.5)

Named Hermes profiles run with a profile-scoped `HERMES_HOME`. The reporters now infer the profile when launched from its plugin directory, and runtime storage resolves through Hermes' own home resolver when available. For the least ambiguous check, ask the running agent to call `nerve_stats`.
