# Nerve Control Panel (local installer and configuration UI)

This is an opt-in browser front end for the existing Nerve profile resolver, CLI,
and atomic profile sidecar. It adds no Python runtime dependencies, cloud accounts,
frontend package managers, external CDNs, or Docker requirements.

## Open the Control Panel

From a Nerve source checkout (Python 3.10 or later):

    python3 -m hermes_nerve ui

Or after installing the Nerve Python package:

    nerve ui

To copy a URL instead of opening a browser, use:

    nerve ui --no-browser

The local browser URL is printed to the terminal. The URL is for the computer
running the process, not a network service. The server binds to 127.0.0.1 on
a randomly selected port; Ctrl+C stops it. It is intentionally not accessible
from another machine.

A second setup entrypoint is provided:

    nerve setup --web

## Beginner setup

1. Choose a Nerve profile. Operator is preselected for fresh configurations.
2. Review available features. Every module has an actual runtime-backed switch.
3. Select the Reflex backend. Non-billable checks for local sidecars are optional.
4. Preview the resolved changes, then click Apply profile. A rollback snapshot is
   stored before any profile is changed. Restart Hermes to activate new settings.

Use Diagnostics & install to review the installation target. Only pressing
Install Nerve into Hermes copies the current source checkout to that location
and asks the Hermes CLI to enable it. An old installation is kept as a separate
backup. Failed enable attempts restore the previous plugin files.

The GUI can be launched from the source checkout before installation. Packaged wheels include both the browser assets and the Hermes plugin registration manifest needed for installation. A fresh GUI installation selects Operator automatically; upgrading an existing installation preserves its current profile. It does not silently replace the running Hermes plugin.

## Existing users and backward compatibility

The absence of a profile sidecar and explicit Hermes nerve_profile setting
continues to mean Legacy runtime behavior. A first-time installation through the graphical wizard initializes Operator. The first graphical setup suggests
Operator for fresh installations but does not rewrite existing Legacy installs
without the user's Apply click. Existing profiles remain unchanged unless
explicitly selected.

The GUI uses the same:
- MODULES declarative registry;
- resolve_config capability/dependency resolver;
- versioned profile.json validator;
- atomic primary/backup writer and exact rollback snapshot.

If a Hermes config.yaml explicitly selects a Nerve profile, that setting takes
precedence. The GUI refuses conflicting changes and shows an actionable warning. To remove a pin explicitly, use `hermes config unset plugins.entries.nerve.settings.nerve_profile` after reviewing your settings.
Existing Hermes settings are inspected through the PyYAML parser if available.
Without PyYAML, the UI still runs, but it refuses to edit a home with an unreadable
config.yaml rather than lying about effective settings.

Profile snapshots are stored in owner-only files under:

    $HERMES_HOME/nerve/control-panel-backups/

Profile settings are saved under:

    $HERMES_HOME/nerve/profile.json

The GUI does not restart Hermes or claim it can observe live process state.
Its readout explicitly distinguishes proposed configuration from runtime state.

## Advanced controls

The advanced toggle reveals all registered modules and manifest-defined typed
settings. New 0.3.2.2 modules can be toggled independently:

- verified_experience
- selective_context (master)
- selective_context_memory
- selective_context_tools
- selective_context_skills

Disabling the master also prunes subordinate context surfaces. Legacy uses its
existing compatibility registration path. Shadow Reflex mode automatically
requests shadow_testing for named profiles. When Selective Context is enabled, the Memory / Tools / Skills modules
now default to apply, not shadow. You can explicitly set a surface to shadow
for measurement or off to disable it. Tool and skill changes require a Hermes
host with the Selective Context consumer bridges; where those adapters are
absent, Hermes keeps its complete stock tool/skill access. Memory selection
is applied only to structured, scope-authorized Verified Experience candidates.
Legacy mode remains unchanged. A successful toggle is not, by itself, proof
that a specific Hermes host actually applied filtering.

Only manifest-declared settings are accepted; current same-profile overrides are preserved. Private
token/password/secret fields are never exposed by the browser settings catalog.
Provider keys stay in Hermes secret handling or the existing environment.

## Connection checks

- Jev / shadow: report whether a relevant environment variable is visible, but
  do not contact a provider or claim profile-scoped credentials are missing.
- Laya / OpenJev / SystemOne / D1: optionally GET a local /health or /v1/version.
  The backend must be localhost or loopback with an explicit port, redirects and
  proxies are disabled, and no model inference request is made.
- A successful health check means only that a service responded, not that its
  model identity or decision schema passed validation.

## Security notes

The server requires a short-lived random token for all APIs, checks the Host
header to stop DNS rebinding, and checks the Origin header for writes. Tokens
are passed in a URL fragment rather than HTTP query strings. Strict
Content-Security-Policy, no-store responses, local-only binding, and no
cross-origin API access are enforced.

No TLS is used because connections are restricted to the local loopback.
Do not forward or publicly expose this server without an independent, reviewed
authentication and transport layer.

## Verification

Run:

    python3 -m unittest tests.test_control_panel tests.test_profile_capability_matrix tests.test_profiles tests.test_profile_migration tests.test_setup_cli -q
    node --check hermes_nerve/web/app.js

Optional full suite:

    python3 -m unittest discover -s tests -p "test_*.py" -q

Two historical test modules require PyYAML or pytest; missing developer dependencies affect full
test coverage, not the zero-dependency runtime path. Install the explicit developer
requirements in `CONTRIBUTING.md` in your own test environment. Nerve deliberately
has no project extras: Hermes otherwise includes them in normal update dependency resolution.