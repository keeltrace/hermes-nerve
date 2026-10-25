# Profile UI is a release requirement

Owner clarified: "I just want it to be tested and ready for release and I want profiles and stuff to be in that". Release qualification includes the existing profile presets, modular runtime, local Control Panel wizard, overview, safe apply/restore, diagnostics and installation UI. These are not optional extras and are not evidence of a live multi-profile operations dashboard.

## Required acceptance

- Fresh setup selects Operator; existing Legacy remains untouched until explicitly changed.
- All named presets and Full Configuration resolve their actual tools, hooks, module dependencies and advanced settings correctly.
- Wizard profile/features/backend/review navigation and the overview work in a real browser.
- Profile, selected features, backend route and pending changes agree with configuration; decorative telemetry and unverified runtime identity are not presented as live proof.
- Preview precedes apply; stale preview is rejected; backup/restore preserves exact settings and private values are not exposed to browser responses.
- HTTP token/origin/host restrictions and local-only binding are enforced.
- Backend reachability is distinct from verified inference; changing route invalidates stale results. Real provider/model calls need their own applicable authorization.
- GUI installation validates exact staged bytes before replacing an existing installation or attempting enable. Rejection preserves prior state. Successful native install/enable and full native PM recovery still require isolated runtime qualification.
- Profile UI assets and contributor documents ship in final-current artifacts. Current GUI credit/payload parity and native-PM transaction behavior need explicit verification; shell-installer evidence cannot qualify the separate GUI installer.

## Current evidence (not full acceptance)

- Added six structural bundled-HTML tests with negative controls for missing/duplicate wizard and overview elements, misleading live telemetry, missing assets and remote frontend dependencies. These are not browser tests.
- Existing Node VM UI test passed: backend selection changes invalidate stale inference results; real-test/setup/tutorial controls are present. It uses a synthetic DOM, not a real browser or model.
- A five-file focused selection passed37 tests plus27 subtests in2.47s after the initial GUI admission fix. It includes profile resolution, capability matrix, migrations and the new UI/admission tests. Later admission CLI availability guard does not change profile modules; final affected-path evidence is below. Counts overlap and must not be added.
- New GUI installer tests were RED (two assertion failures) when install called enable without staged validation. The GUI now validates while old installation is intact and enables those same staged bytes. A separate disappearing-CLI case was RED before adding a fail-closed availability guard.
- Final affected-path check:9 tests passed in0.033s after the production changes:3 tiny fixture-only installer admission tests plus6 structural UI tests. No actual native install/enable was invoked.
- Updated the old enable-failure regression so admission succeeds first and the original enable rollback path is still required, rather than silently becoming an admission-rejection test. This updated old regression remains unverified: a broader39-case attempt was terminated by SIGTERM without output.
- The earlier combined full Control Panel/profile selection hit its12-second cap with only progress dots and no final summary. It is NOT a passing suite. A separate fresh-Operator status test passed in0.198s, so the timeout is not established as a defect in that individual test. Do not blindly repeat the broad run.

## Still open

Real-browser journeys; complete Control Panel HTTP/security/apply/restore tests; current GUI artifact/credit parity; full installer failure/rollback/native PM recovery; native security admission; real candidate runtime behavior; the98-row release matrix; current-source artifacts; and a fresh authorized final full-suite run on the frozen candidate.

Status: PARTIAL, NOT RELEASE-READY. No publication, live installation, installed-core patch, gateway restart, paid calls or dependency/model downloads are authorized by the owner clarification. Heavy qualification needs an explicit capped resource grant; the previous single full-suite grant is consumed.
