# Provider setup

Nerve v0.2.2.dev4 supports three System One transports behind one decision contract.

## OpenRouter

- Credential: `OPENROUTER_API_KEY`
- Endpoint: `https://openrouter.ai/api/alpha/decisions`
- Default model: `typesafe/jev-1.13`
- Provenance transport: `openrouter-decisions`

```bash
export OPENROUTER_API_KEY='...'
hermes config set plugins.entries.nerve.settings.jev_provider openrouter --force
```

## TypeSafe direct

- Credential: `TYPESAFE_API_KEY`
- Endpoint: `https://api.typesafe.ai/v1/systemone`
- Default model: `jev-latest`
- Provenance transport: `typesafe-system-one`

```bash
export TYPESAFE_API_KEY='...'
hermes config set plugins.entries.nerve.settings.jev_provider typesafe --force
hermes config set plugins.entries.nerve.settings.typesafe_model jev-latest --force
```

## OpenCode Zen

- Credential: `OPENCODE_API_KEY`
- Endpoint: `https://opencode.ai/zen/v1/systemone`
- Supported model: `jev-1.13` (paid only)
- Provenance transport: `opencode-zen-system-one`

```bash
export OPENCODE_API_KEY='...'
hermes config set plugins.entries.nerve.settings.jev_provider opencode --force
hermes config set plugins.entries.nerve.settings.opencode_model jev-1.13 --force
```

Nerve rejects `jev-1.13-free` for the OpenCode provider because that tier does not work with Hermes.

Only the selected provider's credential is required. OpenCode is implemented as a native System One transport, not an OpenAI-compatible chat endpoint.

This is a source-repository development build. `packaging/hermes-catalog/jev.yaml` intentionally remains on v0.2.1.2 until a later reviewed stable release.
