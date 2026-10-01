# Nerve v0.3.1 — final maintenance release

Nerve 0.3.1 is the final bug-fix, hardening, and quality-of-life release for the 0.3.0 profile-aware line.

It promotes the validated `v0.3.1rc1` maintenance baseline and adds the final public-facing setup correction for Shared Context. It deliberately does **not** pull 0.3.2 feature work into the maintenance release.

## What this release fixes

### Configuration and context-engine correctness

- Hermes settings are documented and consumed from the real plugin namespace, `plugins.entries.nerve.settings`.
- Legacy `plugins.entries.hermes-nerve` settings are detected and reported instead of being silently treated as active.
- The unsupported context-engine directory-loader path is refused rather than constructing an unconfigured Nerve engine.
- Unknown context-engine modes fail safe to shadow behavior; a typo cannot silently enable destructive apply mode.
- `nerve_stats` advertises only sections its handler actually serves.
- Context-ledger evidence uses the hook-supplied Hermes session ID instead of incorrectly relying on a process-global session.

Primary contributor series: #29, #30, #31, #32, #33.

### Multiplexed gateway / secret-scope propagation

The NervousSystem worker and asynchronous Reflex shadow execution now preserve the enqueuer's `contextvars` context across thread hops. This prevents profile-scoped provider credentials from being lost under multiplexed Hermes gateways.

Reported in #35; fixed through the #36 maintenance work.

### Completion integrity

Machine-checkable Definition-of-Done criteria are re-evaluated against **current** workspace state on each completion attempt.

A stale earlier PASS can no longer survive:

- deletion of a required file,
- a test that later becomes failing,
- newly dirty or untracked Git state.

The validated clean maintenance re-land is represented by #49.

### Retry-loop suppression

Deterministic non-retryable failures now trigger local replanning rather than spending another identical provider/tool cycle.

Examples include stable policy, permission, schema, and path failures. Transient failure behavior remains retryable.

The clean maintenance reconciliation is represented by #50.

### Task-relative progress and environment-thrash recovery

Nerve no longer treats arbitrary activity as implementation progress.

The maintenance behavior now:

- ignores scratch/repro-only mutations as completion progress,
- recognizes native mutation aliases such as `patch`,
- requires persisted task-relevant changes for implementation completion,
- requires successful verification when verification/tests were explicitly requested,
- accepts ordinary zero-failure test summaries,
- detects repeated package/venv/test-environment setup failures and redirects toward repository-native recovery instead of endless setup churn.

The validated clean reconciliation is represented by #50.

### Shared Context public-install behavior

Issue #39 correctly identified that Operator and Fat Cat default-enabled Shared Context even though HermesContextBus had no public install source.

For 0.3.1:

- Shared Context is OFF by default in every named profile.
- Explicit module opt-in remains supported.
- `nerve setup --install-shared-context /path/to/HermesContextBus` remains supported.
- `nerve setup --explain` no longer reports a missing HermesContextBus dependency while Shared Context is disabled.
- Explicitly enabling Shared Context still reports the missing dependency when it is unavailable.

This is the conservative maintenance fix. Public HermesContextBus distribution remains separate work in #40.

### Public source hygiene

Historical machine-specific absolute filesystem paths in public planning/evidence material were replaced with generic equivalents before the final release source tag. This is documentation/evidence hygiene only and does not alter runtime behavior.

See #54.

## Explicitly excluded from 0.3.1

The following remain separate development/feature tracks:

- Deep Research Kit — #37
- block-review judge — #38
- public HermesContextBus packaging/publication — #40
- ask-only Action Gate feature and follow-up — #41 / #44
- other 0.3.2 development work

This boundary is intentional: 0.3.1 is a maintenance release, not a feature aggregation release.

## Attribution

### @colibrishin

Reported **#29** and authored the four-part hardening series:

- #30 — real plugin settings namespace + directory-loader guard
- #31 — fail-safe context-engine mode handling and documentation
- #32 — truthful `nerve_stats` schema
- #33 — correct Hermes hook session attribution

These changes materially improved configuration correctness and runtime truthfulness.

### @omarabdo516

Reported **#35**, including the concrete multiplexed-gateway `UnscopedSecretError` failure and ContextVar/thread-hop diagnosis. That report drove the worker/shadow context propagation fix represented by #36.

### @HiroKws

Reported **#39** and followed it with a detailed implementation/history investigation. The report identified that the Shared Context integration had been shipped without a public HermesContextBus source and distinguished the runtime integration from the distribution defect. That work directly drove the 0.3.1 opt-in correction and the separate #40 publication track.

### @keeltrace

Maintainer integration, release engineering, regression-campaign reconciliation, retry/completion/progress hardening, public-source hygiene, and assembly of the final 0.3.1 maintenance line.

## AI-assistance disclosure

Parts of the implementation and review process used coding/reviewer agents. Relevant public PRs contain their own disclosures where applicable. Contributor credit in this document follows the human-visible GitHub issue/PR authorship and reporting record; AI assistance is not substituted for human contributor attribution.

## Final release gates

This draft should not be promoted until the exact final branch passes:

- Python 3.10, 3.11, 3.12, 3.13, and 3.14 CI
- full repository test suite
- `scripts/verify_release.py`
- Hermes Plugin Validate
- Hermes Plugin Doctor
- focused settings/context/session-scope regressions
- focused stale-completion/retry/progress regressions
- Shared Context profile/setup regressions
- package build and release-asset structural verification
- final diff review confirming no 0.3.2 feature ancestry entered the branch

The release tag must be created from the exact reviewed/verified final commit.
