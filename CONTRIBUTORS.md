# Nerve release contributors

This release credits the following external contributions. Integration and verification status is tracked in `docs/PR_INTEGRATION.md`; attribution is not a claim that an open GitHub PR has been remotely merged.

## Phuong Lambert (@phuongvm)

- [#63](https://github.com/keeltrace/hermes-nerve/pull/63): case-insensitive URL stripping and numeric-metric filtering in requested paths. Reviewed head: `6e76f63d1f383849643a5dcedd8135cff95f1090`.
- [#64](https://github.com/keeltrace/hermes-nerve/pull/64): selective bypass for read-only Kanban and skill introspection, retaining gates for writes. Reviewed head: `e98675015ac75a110b7d39ed860a3d423048c2ab`.

## @rharmanca

- [#67](https://github.com/keeltrace/hermes-nerve/pull/67): observe-only shadow completion behavior and metrics. Reviewed head: `19d479dcda7343ff1aeec857fdfe4c8386a0e071`.
- [#68](https://github.com/keeltrace/hermes-nerve/pull/68): dispatcher-only canonical task-ID environment pinning; fixes hook-local autobind environment pollution. Reviewed head: `1c96c4edf5b1d100d65359a17b9331053beed903`.
- [#66](https://github.com/keeltrace/hermes-nerve/pull/66): progress-result envelope parsing, real-test-pass checks, command recognition, V4A paths and relative-path decoy protection. Reviewed head: `21302cef6112c7e99ac9704280498585d2741701`.
- [#72](https://github.com/keeltrace/hermes-nerve/pull/72): environment-launcher test recognition, stacked on #66. Reviewed head: `d2b69641b670a8ed538dcd263da194b716fe7a56`. Shared #66 changes must be integrated once; both PRs retain explicit credit.
- [#73](https://github.com/keeltrace/hermes-nerve/pull/73): additional test-runner recognition, whole-word failure parsing and blocking masked test exit statuses. Reviewed head: `a2a19de841b4d4ce94e64fb791085373a8df7031`; integrated/adapted in v0.4.15 commit `eb4c696` with original adversarial fixtures.
- [#69](https://github.com/keeltrace/hermes-nerve/pull/69): TypeSafe-only credential auto-selection, OpenRouter402 guidance and provider-default documentation/schema correction. Reviewed head: `1164a9b7d07ea51ef2d7b950323e93c882978475`.
- [#71](https://github.com/keeltrace/hermes-nerve/pull/71): deterministic raise-only admission supervision floor, provider-failure fallback, adversarial coverage, configuration and metrics. Reviewed head: `f3aa3c69dcc59cd786a6d0356dfe585a1a04276f`.

Thank you to both contributors for the code, reproductions and regression tests. Their PR descriptions disclose AI assistance; attribution remains with the submitting contributors and original commit authors. Local integration adaptations are the release maintainer's responsibility.

## Maintainer provenance

- [#59](https://github.com/keeltrace/hermes-nerve/pull/59) (repository maintainer @keeltrace): harness instruction and internal-path protections. Its original KEE-804 replay was adapted in v0.4.15 commit `40b5faa` into a public portable fixture, without including private local agent paths.
