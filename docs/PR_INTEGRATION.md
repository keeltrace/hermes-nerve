# External PR integration — Nerve 0.4.11 candidate

All eight requested external PRs are incorporated into the **local** release branch `release/v0.4.11-internal`. No remote PR merge, push, release publication or installed-plugin cutover is implied. Original contributor author identities and upstream PR/commit provenance are retained in the local integration commits; see [CONTRIBUTORS.md](../CONTRIBUTORS.md).

## Integration ledger

| PR | Contributor | Local integration commit | Result |
|---|---|---|---|
| [63](https://github.com/keeltrace/hermes-nerve/pull/63) | Phuong Lambert (@phuongvm) | c049746 | URL/metric filtering integrated; prior source-list/harness-path guards preserved |
| [64](https://github.com/keeltrace/hermes-nerve/pull/64) | Phuong Lambert (@phuongvm) | e1e97a4 | Read-only introspection bypass integrated; write tools and sensitive-search protection retained |
| [67](https://github.com/keeltrace/hermes-nerve/pull/67) | @rharmanca | e7843b0 | Observe-only shadow completion, logging and metric integrated |
| [68](https://github.com/keeltrace/hermes-nerve/pull/68) | @rharmanca | 916f2a2 | Dispatcher-only canonical task-ID environment propagation integrated |
| [66](https://github.com/keeltrace/hermes-nerve/pull/66) | @rharmanca | 84a8aed | Envelope/no-test/zero-failure behavior, V4A paths, direct test commands and decoy rejection integrated |
| [72](https://github.com/keeltrace/hermes-nerve/pull/72) | @rharmanca | 52098a3 | Launcher delta integrated after #66; shared #66 commit applied once and credited to both PRs |
| [69](https://github.com/keeltrace/hermes-nerve/pull/69) | @rharmanca | 4cf5912 | TypeSafe-only auto-selection, schema/documentation default and 402 guidance integrated |
| [71](https://github.com/keeltrace/hermes-nerve/pull/71) | @rharmanca | 979ca87 | Raise-only admission floor, failure fallback, flags, metrics and adversarial tests integrated |

Saved exact upstream heads, diffs, authors and empty comment/review snapshots are in the local qualification directory `../nerve-release-gauntlet-20261022/pr-integration/` relative to the repository's parent. Diffs were re-fetched against immutable base/head comparisons. Git blob hashes were checked before three-way conflict resolution. No required-review protection was bypassed.

## Reconciliation and additional regression coverage

- Preserved gauntlet safety checkpoint `fea0264` before integration.
- #64 retains the newer catalog's `catalog-read-only` telemetry reason for cataloged introspection; its read-only fallback remains for uncatalogued Kanban introspection. The post-integration full suite exposed an initial reason-contract conflict, repaired without changing the existing catalog assertion or removing PR #64's read/write coverage. Cataloged sensitive reads now stop before the generic name-based fallback.
- #66 retained stricter existing integer exit-code validation, malformed-response rejection and GNU timeout recognition; did not import a direct-test regex that could treat `python -c` decoys as tests.
- #72's generic long-option parser could consume `echo` after a flag and falsely classify `uv run --offline echo pytest`. Reproduced and fixed with explicit value-taking options. Shell segmentation now respects quotes, so a semicolon inside `python -c` cannot fabricate another test command.
- #69 retained current1.14 model defaults instead of reverting the older PR's1.13/jev-latest context. Repaired its static transport's undefined `self` reference on HTTPError and retained response-body nondisclosure. Added a real urllib-error-path regression with a fake HTTPError, not a live provider call.
- #71 retained newer policy-memory settings and bounded queue behavior while wiring the new setting into both registration paths.
- Original contribution tests were imported without weakening assertions. New integration regressions live in `tests/test_gauntlet_pr_integration.py`.

## Verification receipts so far

These selections overlap; do not sum them into a fictitious distinct-test total.

- Initial imported non-admission selection:29 failures,14 passes, demonstrating missing behaviors on the pre-integration candidate.
- First four PRs plus existing sensitive-search/queue/path regressions:64 passed plus8 subtests in0.74s.
- #66 plus existing progress regressions:37 passed plus8 subtests in0.45s.
- #72 plus direct/quoted-code/launcher safety regressions:48 passed plus13 subtests in2.35s.
- #69 provider/default/client plus integration regressions:22 passed in0.26s.
- #71 admission/nervous/shadow/queue plus integration regressions:48 passed plus20 subtests in1.27s.

A broader 33-file combined selection collected 320 tests. Its run timed out at 25s after 41 progress dots. Collection order suggests the control-panel wire tests as the boundary, but this was not a verbose execution trace and is not conclusive localization. A subsequent diagnostic subset was terminated by SIGTERM (exit -15) before producing output; neither attempt is a passing receipt.

Final bounded verification on the integrated code:

- **32-file combined contribution/dependency selection: 308 passed plus 66 subtests in 12.43s, exit 0.** This includes all eight PRs, current core/provider/registration behavior, sensitive-search protection, async progress, command identity, shadow, queue and admission regressions. It excludes `test_control_panel_real.py` from this selection, without altering or disabling that file.
- **Complete control-panel wire file, separately: 12 passed plus 6 subtests in 4.24s, exit 0.** The originally suspected health-only case also passed independently in 0.70s. These use loopback HTTP fixtures and mocked hosted transport, not live provider proof.

The selections are disjoint: 320 tests plus 72 subtests passed across these two final runs. The all-33-files-in-one-process attempt is still not a passing receipt; an order-dependent or transient issue has not been conclusively ruled out. Full post-integration suite remains unverified; the earlier 1093-test full suite predates these runtime changes and cannot qualify this combination.

## Configuration migration note (#69)

`jev_provider` schema default changes from `openrouter` to empty/unset. Unset selects TypeSafe only when its key is the sole configured provider key; explicit provider settings, environment override and explicit-key compatibility retain precedence. If both keys exist, OpenRouter remains the default. Operators should set a provider explicitly for deterministic routing. This code integration is not permission to make billable provider calls.

## Attribution delivery follow-up

A subsequent packaging audit found a real remaining gap: contributor credits existed in Git but were absent from both distribution data-files and the runtime installer's explicit root allowlist.

- Added `CONTRIBUTORS.md` and `CHANGELOG.md` to both payload declarations; retained private-file exclusion and preflight-before-deployment behavior.
- Added `tests/test_gauntlet_contributor_delivery.py`. Before the repair, both tests failed: metadata omitted the files, and an actual isolated runtime payload omitted `CONTRIBUTORS.md`. The terminal envelope unexpectedly reported exit 0 despite the two failed assertions; the failure output, not that envelope status, is the RED evidence.
- After repair: **27 passed in 4.19s**, including contributor-delivery, installer-safety and release-verifier tests; explicit pytest return code and tool exit code were both 0.
- Built a wheel and sdist offline with the installed `setuptools.build_meta` backend. `python -m build` was unavailable despite import discovery finding a local namespace named `build`; no package was downloaded or installed to work around it.
- The first wheel inherited 113 bytecode/cache entries from the existing build directory. It was not accepted. Rebuilt using a fresh `build --build-base` directory through the backend's global-option setting.
- Inspected both final archives: **all 113 runtime Python source files match the current checkout byte-for-byte**, both attribution documents match the current checkout, and neither archive contains bytecode/cache or `.env`/`.git` path members. This is artifact-content evidence, not live activation or full-suite qualification.
- Verified artifacts and machine-readable evidence are retained under the qualification directory's `pr-integration/attribution-artifacts/`.

## Post-integration full-suite reconciliation

The freshly approved full suite at candidate `6a067a9` exited 1: **1 failed, 1148 passed, 1 skipped, 4 warnings and 128 subtests passed** in 137.25s (coordinator 138.36s). The fingerprint `f2c53f9699f7ddcf7ece915f17bcf47104e0cee1f9e9bd85e9c48c997dbdf883` was unchanged across execution. This consumed that separate one-run approval.

- Failure: `tests/test_catalog.py::test_read_only_operations_are_recognized`; PR #64 integration returned `read-only-tool` instead of the established `catalog-read-only` reason for `skills_list`.
- RED: seven targeted failures reproduced the reason conflict and three sensitive `skill_view` reference paths incorrectly receiving a generic bypass (`/etc/shadow`, `/etc/sudoers`, `~/.ssh/id_rsa`). These tests perform classification only; no protected file was read.
- Repair: restore catalog-first safe-read classification and stop a non-safe cataloged read before the generic fallback. Keep PR #64's read-only allowlist additions and write-tool rejection.
- Updated only the newly imported PR #64 test's incidental reason expectations to explicit canonical catalog/fallback reasons. Existing catalog assertions and all strict write/sensitive-path assertions remain intact.
- GREEN: **193 passed in 22.61s**, explicit pytest and tool exit codes 0, covering catalog precedence, core, sensitive search/path binding, integration regressions and HITL v2.
- Four existing multiprocessing/fork warnings from the full run and the legacy backend skip remain recorded. No whole-suite pass is claimed for the repaired source. Earlier artifacts predate this runtime change until rebuilt and byte-compared again.

## Scope boundaries

The request's eight reviewed external PRs are included. The separately mentioned owner queue (#59, #37, draft#40, #38 and#55) is not silently merged: it was process context, including explicitly unready/draft work.

Overall release status remains **PARTIAL**: combined verification, real gateway acceptance, current artifacts and the independent plugin security-validation gate still require qualification. Scanner findings remain release-blocking; no scanner bypass, pattern obfuscation or unapproved core modification is justified by a contribution merge.
