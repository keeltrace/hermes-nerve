# Nerve Operator default and modular 0.3.2.2

## User-facing default

New setups that use the interactive `nerve setup` selector choose **Operator** when Enter is pressed. Explicit `nerve setup --profile operator` saves the Operator choice. Existing installations without an explicitly selected Nerve profile still enter Legacy to avoid silent upgrades that drop historical hooks or tool schemas. This remains a migration boundary, not a claim that all installed Hermes profiles automatically switch.

## New independent feature switches

The following module IDs are selectable by Full Configuration or via `nerve_modules` overrides:

- `verified_experience`: enable local evidence-backed experience collection
- `selective_context`: master enable for context admission planning
- `selective_context_memory`: memory surface (depends on selective_context)
- `selective_context_tools`: tool surface (depends on selective_context)
- `selective_context_skills`: skill surface (depends on selective_context)

All named presets inherit previously active Verified Experience and Selective Context default behavior. Their subordinate surfaces default to shadow unless advanced settings choose otherwise. A switch can disable a feature without resetting existing data. Disabled master modules prune dependents. Fat Cat, Operator, Lean, and Marie Kondo preserve their prior tool schema registrations.

To disable selective skills in Operator, for example, configure:

```yaml
plugins:
  entries:
    nerve:
      settings:
        nerve_profile: operator
        nerve_modules:
          selective_context_skills: false
```

Do not use `plugins.entries.hermes-nerve`: Hermes ignores that namespace.

## Compatibility gates

- Explicit Legacy stays on its original registration path; do not infer new module behavior from named-profile flags in the Legacy path.
- A missing profile sidecar and missing explicit profile still use Legacy until the operator elects a profile.
- The named profiles retain their existing 0.3.2.2 feature defaults, unless the operator explicitly toggles modules.
- Worker headless registration, dependencies, and remote-host conditional behavior remain unchanged.
- This development change does not install into live Hermes. Observatory and new A/B harness worktrees still require separate feature integration and testing.
