# Nerve v0.4.0 — Controlled Canary, Fleet Rollout and Rollback

**Status: procedure for the internal v0.4.0 release.** Every step that touches a
live Hermes home requires explicit operator authorization and a completed
backup. Nothing in the live environment is modified by this document.

## Verified plugin-override mechanism (proven 2026-10-21 with a real `hermes` session)

Hermes discovers plugins from bundled, user (`$HERMES_HOME/plugins`), and — when
`HERMES_ENABLE_PROJECT_PLUGINS=1` — the PROJECT directory (`$PWD/.hermes/plugins`)
with **project > user > bundled** precedence
(`hermes_cli/plugins_discovery.py::collect_directory_manifests`).
The earlier documented `HERMES_PLUGIN_PATH_OVERRIDE` variable DOES NOT EXIST —
do not use it. A project-plugin session loads the trial build while the
installed plugin, `config.yaml`, and `profile.json` stay byte-identical.

## Artifacts

- Source tree: `/home/user/Documents/LargeProjects/nerve-v0.4.0` (branch `release/v0.4.0`)
- Plugin package: built wheel/ZIP (see `RELEASE_MANIFEST_v0.4.0.sha256`)
- Version: `plugin.yaml` and `pyproject.toml` both `0.4.0`
- Hosted Reflex: Jev 1.14 pinned — OpenRouter `typesafe/jev-1.14`,
  TypeSafe direct `jev-1.14.0` (OpenCode stays an explicit 1.13-only lane)

## Phase 1 — Session-only canary (no durable changes)

1. Stage the exact tested plugin into a trial project:
   `mkdir -p /tmp/nerve-canary/.hermes/plugins`
   then copy the source tree (excluding `.git`, caches) into
   `/tmp/nerve-canary/.hermes/plugins/nerve/`.
2. Launch a REAL session from the trial directory:
   `cd /tmp/nerve-canary && HERMES_ENABLE_PROJECT_PLUGINS=1 hermes`
   (interactive) or `HERMES_ENABLE_PROJECT_PLUGINS=1 hermes -z "..."` (one-shot).
3. Verify the loaded version reports 0.4.0 and Nerve hooks register
   (gate events carry v0.4-only vocabulary the old install never writes).
4. Exercise: one routine action (no prompt), one consequential action
   (explained approval appears; deny is respected), `nerve ui` setup and
   diagnostics, ContextBus health.
5. Rollback: delete `/tmp/nerve-canary` — the live installation was never touched.

## Phase 2 — Durable single-home canary

1. Back up the target home completely:
   `tar -C <home> -czf <backup>/nerve-backup-$(date -u +%Y%m%dT%H%M%SZ).tgz plugins config.yaml nerve`
2. Record pre-state SHA256 of every `plugins/nerve` file, `config.yaml`,
   `nerve/profile.json`, and the approval allowlist.
3. Install the exact tested artifact (copy wheel/ZIP contents into
   `<home>/plugins/nerve/`). NEVER `rsync --delete` against a live home.
4. Remove ONLY obsolete over-broad Nerve approval keys
   (e.g. `plugin_rule:nerve:low-confidence`) from `config.yaml` after backing it
   up; leave every unrelated entry intact.
5. Restart the session/gateway for that home only; verify loaded 0.4.0,
   effective profile unchanged, UI + ContextBus health checks pass.
6. Rollback: stop the session, restore the backup tarball and backed-up
   `config.yaml`, restart, verify the pre-state hashes.

## Phase 3 — Fleet staging

- Census every active Hermes home on j2 and remote win4060 first (including the
  WhatsApp-hardened gateway). Record per home: host, profile, pre-version,
  active processes/services.
- Roll out incrementally in small batches AFTER the durable canary is accepted;
  never restart an uninterruptible session; keep per-home backups and rollback
  handles. Any unreachable or unauthorized target is reported as PENDING, never
  assumed complete.
- Verify post-install: loaded version, registered hooks, service health, and
  one Nerve-driven harmless decision per representative profile.

## Acceptance gates

- Loaded version == 0.4.0 on every reachable target (not merely copied files).
- Routine work flows without needless prompts; consequential work gets useful,
  effect-scoped approvals; denial is respected.
- ContextBus A→B exchange
  works with C denied; bus-down fallback safe.
- Jev responses report a pinned 1.14 model id on the configured transport.
- Zero GitHub publication, zero Paperclip coupling, zero unapproved downloads.
