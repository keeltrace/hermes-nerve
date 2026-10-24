# Nerve 0.4.14 — sanitized release

0.4.14 = 0.4.13 + release sanitization; no behavior change.

- Observatory roots derive from HERMES_HOME/user home (no hardcoded absolute paths)
- Internal planning material and stale source snapshots excluded from the public tree
- See CHANGELOG.md for 0.4.12/0.4.13 (sidecar authority, save-path repairs, CLI/panel parity)
- Security disclosure: see RELEASE_NOTES_0.4.13.md (stock scanner flags 3 deliberate gated capabilities; --force documented)
- Quality gates: 1,339 passed / 0 failed / 6 skipped; release verifier PASS; secrets sweep clean
