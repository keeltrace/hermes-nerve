# Nerve HITL v2 — Risk-Aware Human-in-the-Loop

Version: 0.4.0.dev0 (branch `feature/hitl-v2-risk-aware-gate`, v2.3 integrity pass)
Status: implemented and verified; **not installed** into the live Hermes
environment. Security redesign is FROZEN after this pass. Controlled
single-session activation + rollback: `docs/ACTIVATION.md` (not executed).

## v2.3 final authorization-integrity pass (round 4)

Fixed test-first (`docs/evidence/round4-pre-fix.txt`: 12 failures pre-fix;
`tests/test_hitl_v2_round4.py`: 27 fixtures, all passing):

- **D1 path-qualified executables** — `./ls`, `./cat`, `/tmp/pytest` are never
  trusted by basename; explicit-path invocations go to the model band
  (uncertain). Bare trusted commands stay fully automated.
- **D2 nested sensitive-home + .env** — sensitive-home matching is
  segment-exact at any depth (`~/.config/gh/hosts.yml`,
  `~/.config/gcloud/*` now caught); `~/.docker` and `~/.kube` added;
  `.pem/.key/.p12/.pfx/.keystore` extensions added to sensitive names
  (scope-independent); reading any `.env` escalates (secrets enter context)
  while WRITING a project `.env` stays routine project work.
- **D3 push effect variants** — `--all`/`--tags`/`--mirror` bind to the full
  resolved ref set (`for-each-ref`), multiple refspecs bind individually, and
  the remote URL is part of the effect (same commits to a different URL
  re-approve).
- **D4 non-reusable keys** — consequential effects without a secure binder
  (API POSTs, ssh, chmod, installs) get per-invocation nonce keys: an
  `[a]lways` can never authorize a repeat. Securely identifiable effects
  (writes: path+content; sensitive reads: path set) keep stable keys.
- **D5 multi-file keys** — read_file/read_many_files shapes bind EVERY path
  (`paths` up to 128 + `paths_sha256` over the full list); a 10-path request
  differing only in the last file gets its own approval. Terminal argv was
  already complete (verified with a 12-file read).

Real-CLI proof (`docs/evidence/cli-round4.txt`): repeated POST re-approves
(keys differ); `push --all` re-approves after a new branch; `./ls -la`
prompts; 10-path read prompts; project `.env` read prompts.

## v2.4 final focused corrections (round 5 — activation gate)

Fixed test-first (`docs/evidence/round5-pre-fix.txt`: 9 failures pre-fix via
HEAD-archive run; `tests/test_hitl_v2_round5.py`: 15 fixtures):

- **D1 separators** — lone `&`, newline and CR are command separators
  (`&&`, `||`, `&>`, `>&` preserved); the read-only prefilter also rejects
  any separator command outright. `ls & rm -rf …` and newline-chained
  commands can no longer ride a read-only head. (Examples are classified
  only — the gate never executes them.)
- **D2 preflight path resolution** — the read-only prefilter resolves
  relative targets against the claimed execution directory before bypassing:
  `cat sshd_config` in `/etc/ssh` and `cat hosts.yml` in `~/.config/gh`
  escalate instead of bypassing. Routine relative reads still bypass.
- **D3 `--mirror`** — conservatively re-approves on every invocation
  (heads+tags resolution cannot prove the full `refs/*` publication).
  `--all`/`--tags` keep full-ref binding; identical effects stay stable.
- **#4 real-session override verification** —
  `docs/evidence/session-verification.md`: a REAL live-home `hermes -z`
  session with `HERMES_ENABLE_PROJECT_PLUGINS=1` + project
  `.hermes/plugins/nerve` loaded v2.3 (live-ledger escalation row carries
  v2.3-only markers); live plugin/config/profile hashes identical; no
  allowlist changes. `ACTIVATION.md` Phase 1 corrected
  (`HERMES_PLUGIN_PATH_OVERRIDE` never existed).

Full suite: **632 passed, 1 skipped, exit 0** (including the git-workspace
tests after repairing `.git` object damage caused by an external process
race — repaired by re-hashing the worktree; `git fsck --full` now clean).

## v2.2 round-3 blockers (second independent review)

Fixed test-first (`docs/evidence/round3-pre-fix.txt`: 21 failures pre-fix;
`tests/test_hitl_v2_round3.py`: 31 fixtures, all passing post-fix):

- **R1 workdir authorization** — sensitive-home locations and protected
  system paths (`/etc/ssh`, `/etc/sudoers`, `/etc/shadow`, `/etc/cron*`,
  `/etc/ssl/private`, `/etc/systemd/system`, `/root`, ...) take precedence
  over ANY claimed working directory. Relative command targets resolve
  against the claimed execution directory (that is where commands actually
  run) while scope authorization comes only from `valid_scope_root`, which
  rejects home/home-ancestors/system/sensitive claims. Probed: `~/.ssh`,
  `/etc/ssh`, sibling projects. Routine system reads (`/etc/os-release`)
  still flow.
- **R2 destructive git** — `git stash clear|drop`, force branch deletion
  (`-D`, `-B`, `--delete --force`), `git config --global|--system` writes and
  `git worktree remove` are CONSEQUENTIAL approvals, never reversible project
  work. Global config READS and safe deletes (`-d`) still auto-run.
- **R3 effect-bound approvals** — consequential keys bind to the distinct
  EFFECT: `git push` keys carry the resolved commit SHAs (`git rev-parse`,
  bounded read-only), so an approved push authorizes exactly those commits —
  new commits force a new approval. Deletions bind to target filesystem
  identity (a recreated file is a distinct effect); uploads bind source
  identities. Unresolvable effects get a per-invocation nonce (re-approve
  every time). Same effect keeps a stable key.
- **R4 read_file bypass closed** — `read_file`/`read_many_files` with
  credential or protected-system paths no longer bypass as "read-only-tool";
  they are classified (routine → safe auto-allow with telemetry, sensitive →
  consequential approval with explanation).

Real-CLI proof (`docs/evidence/cli-round3.txt`, real Hermes pty gate, REAL
commits in a temp repo): push commit A + `[a]lways` → key `b413677f…`;
new commit B → same command re-prompts with key `4f70b027…`; re-push of B
keeps the stable key. `git stash clear` and global config writes prompt;
`read_file ~/.ssh/id_ed25519` prompts with an explanation.

## v2.1 release-blocker round (first independent review)

Five defects found by review were fixed test-first (each proven failing before
the fix — `docs/evidence/blockers-pre-fix-finaltests.txt`, 19 failures — and
passing after):

- **B1 pipeline key collisions** — pipeline approval keys now embed every
  segment's full shape material (was a segment COUNT: `ls | head` and
  `curl evil | head` shared one key).
- **B2 non-shell tool key collisions** — keys now carry an arguments
  fingerprint with per-value digests for sensitive fields (was the tool name
  alone: every `browser_navigate` shared one key).
- **B3 numeric-target collisions** — positional numbers keep identity
  (`kill -9 1234` ≠ `kill -9 1`, chmod modes differ); only inert flag VALUES
  normalize.
- **B4 file-write content blindness** — write shapes carry a content digest;
  approving one write no longer authorizes different content to the same path.
- **B5 git read-safety** — read-only classification is argument-aware:
  `git config K V`, bare `git stash` (== push), `git tag v1`, `git branch b2`,
  `git remote add URL`, `git worktree add` were riding the safe-read path.
- **B6 workdir trust** — a caller-supplied workdir is verified (realpath,
  must exist, not / / home / ancestor-of-home / system root); boundary checks
  use `os.path.commonpath` (the sibling-prefix bug: `/x/proj-secret` used to
  match workdir `/x/proj` via `startswith`); lying or missing claims fall back
  to the process cwd.
- **B7 weak-ALLOW escalation** — an uncertain-band ALLOW with p(ALLOW) below
  the floor, confidence below minimum, or missing/invalid calibration now
  ESCALATES with an honest uncertainty note ("the model leaned toward allowing
  this, but its evidence was weak…"). It never silently runs and never invents
  a danger. Recognized routine work still never reaches the model: the
  original 0.82-vs-0.90 scenario (pytest) remains deterministic-safe.

Real-CLI authorization-reuse proofs (`docs/evidence/cli-reuse-v2.1.txt`,
driven through the actual Hermes `request_tool_approval` pty flow in a
sandbox): after `[a]lways` on `kill -9 4242`, `kill -9 1` re-prompts with a
different key; after `[a]lways` on `curl A|head`, `curl B|head` re-prompts;
after `[a]lways` on a benign file write, different content to the same path
re-prompts; `git config K V`/`git stash` auto-run as PROJECT (never SAFE);
`workdir=/` grants nothing (`rm /etc/hosts.allow` hard-blocked with an
explanation); `push main` still prompts after `[a]lways` on a feature push.

## Where live interruptions actually come from

`docs/evidence/live-interruption-origins.md`: the live gate runs in
**ask-only** mode — `profile.json advanced.gate_mode: ask-only` (written Oct 9)
overrides the `advisory` value in `config.yaml` via the plugin's
`profile_default` resolution order. October live telemetry: 25,111 evaluated
rows, 12,174 (48.5%) matching old-gate interrupt rules (9,745 low-confidence —
auto-approved by the stale `plugin_rule:nerve:low-confidence` allowlist entry;
1,917 below-floor prompts; 512 BLOCK escalations) plus 5,685 provider-error
fail-to-human prompts (peak 2,419 on Oct 17). Nerve IS the interruption source;
the Hermes core approval gate (`tools/approval.py`) is the renderer, and other
plugin rules exist but are not the volume driver.

## Problem

The v0.3.2.3 gate interrupted the operator whenever model output looked
numerically weak, without explaining any actual danger:

- `ALLOW` verdicts with `p(ALLOW) < 0.90` interrupted with
  `"Nerve p(ALLOW) 0.820 is below 0.900; human approval required."` — 44.1% of
  all ALLOW verdicts in live telemetry (11,370 / 25,775 over Sep 22 - Oct 20).
- Low confidence (< 0.80) interrupted with a bare confidence number.
- Provider outages interrupted with a generic fail-to-human message.
- Approval rule keys were GENERIC: one `[a]lways` on any of these stored
  `plugin_rule:nerve:low-confidence` / `nerve:low-allow-probability` /
  `nerve:provider-unavailable` / `nerve:{tool_name}` permanently — authorizing
  every future unrelated command in that class. The live config still contains
  `plugin_rule:nerve:low-confidence` from exactly this failure.

Under the old system, 79.5% of evaluated calls in ask-only windows would have
interrupted (measured from live telemetry), and the old gate silently ran
62% of consequential actions in the replay corpus (model happened to say
ALLOW with p >= 0.90).

## Design

Layered decision flow (gate.pre_tool_call):

1. Deterministic risk classification (`hermes_nerve/risk.py`) for every call:
   - `safe_routine` — read-only inspection; auto-execute.
   - `authorized_project_work` — reversible effects inside the project scope
     (workdir/tmp): git add/commit/branch, pytest, artifact cleanup, project
     file writes; auto-execute.
   - `uncertain` — not recognized; consult the decision model (one bounded
     retry), then a learned-preference check.
   - `consequential` — deletion, publication, privilege, sensitive files,
     external transfer, package/system changes; specific human approval.
   - `prohibited` — home/root deletion, disk destroy, persistence writes,
     pipe-to-shell RCE, host power-off; block with a concrete explanation
     (ask-only mode escalates to a human instead).
2. Model confidence and p(ALLOW) are calibration signals, never interruption
   reasons. The 0.82-vs-0.90 scenario auto-allows with a logged rationale
   (`uncertain-allow-below-floor`) because the deterministic scan found no
   hazard; recognized routine commands never reach the model at all.
3. Every approval message answers: what is being attempted, why permission is
   needed, what happens if approved, what could go wrong (+ safer alternative
   when one exists). Confidence numbers never appear.
4. Approval rule keys are CONTENT-SCOPED:
   `nerve:hitl:{risk_class}:{tool}:{shape-hash}` where the shape covers argv
   (inert numerics/pytest selectors normalized), workdir, and risk class.
   `[s]ession` / `[a]lways` therefore authorize exactly the approved action
   shape — nothing else. Same command => same key (stable); any variation
   re-prompts.
5. Provider outage: recognized routine work continues deterministically;
   unrecognized work fails to human with an honest "could not verify" note.
   No invented security claims; single retry, no loops.
6. Preference learning (`hermes_nerve/preferences.py`): only uncertain-band
   shapes, only after the operator approved that exact shape twice
   (`gate_preference_min_samples`); consequential/prohibited classes are never
   learned and never bypassed. Consent is never inferred from generic keys.

Hermes's own security floors (hardline blocks, dangerous-command detection,
container guards) run independently upstream and are untouched.

## Configuration (plugin.yaml)

- `gate_risk_preflight` (default true) — deterministic layer before the model.
- `gate_learned_preferences` (default true) — shape-scoped learning.
- `gate_preference_min_samples` (default 2).
- `gate_mode` unchanged: off / advisory / enforce / ask-only.
- `min_allow_probability` retained ONLY as a telemetry annotation for
  uncertain-band allows; it no longer interrupts.
- `min_confidence` retained for telemetry only; it no longer interrupts.

New telemetry actions in gate-events.jsonl: `auto-allow`,
`evaluated-advisory`, `escalated`, `blocked` (plus legacy evaluated/bypass/
disabled/provider-error). New fields: `risk_category`, `rule_key`, `detail`.
`gate.report()` summarizes interruptions and risk categories.

## Verification evidence

- `tests/test_hitl_v2_blockers.py` — 33 release-blocker fixtures; proven
  failing pre-fix (19) and passing post-fix with the identical final test file.
- `tests/test_hitl_v2.py` — 74 v2 fixtures: all mission categories
  (read-only, pipelines, git inspection, project edits/tests, unknown
  executables, deletion, privilege escalation, sensitive files, credential
  exposure, publication, network transfers, provider outage, session-key
  reuse, 0.82-vs-0.90) plus 15 adversarial cases.
- Full suite: **559 passed, 1 skipped** (legacy gate tests updated to the v2.1
  contract, with comments documenting each intended change).
- `docs/evidence/replay-metrics-v2.1.txt` — old-vs-new replay over identical
  corpora, model outputs sampled from 29,021 real ledger events:
  - routine work (incl. authorized git mutations): 28 -> 0 unnecessary
    interruptions (100% reduction; target >= 80%)
  - consequential corpus gated: old 6/21 -> new 21/21
  - prohibited corpus blocked: new 4/4 (old: 2 touches, 2 silently ran)
  - outage over routine work: old 53/53 -> new 0/53
  - overall operator touches: 89 -> 25
- `docs/evidence/cli-round3.txt` — five real-CLI round-3 scenarios
  (effect-bound push with real commits, stash clear, global config write,
  read_file sensitive probe, same-effect stability).
- `docs/evidence/replay-metrics-v2.2.txt` — v2.2 replay: unchanged acceptance
  (routine 81 -> 0, 100% reduction; B gated 21/21; C blocked 4/4; outage 0/53).
- `docs/evidence/cli-reuse-v2.1.txt` — six real Hermes approval-gate reuse
  scenarios (numeric targets, pipelines, file-write content, git classes,
  workdir trust, legacy push regression) driven through the actual pty prompt.
- `docs/evidence/cli-transcript-*.txt` — earlier v2.0 transcripts (push/rm/
  push-main/outage) retained.
- `tests/conftest.py` — all test telemetry redirected to temp dirs; verified a
  full test run no longer appends to the live ledger (earlier runs appended
  217 session-less rows + 37 `nerve:hitl:*` rows on Oct 20; disclosed in the
  origins report, live data otherwise untouched).

## Rollback

Nothing live was modified. The working tree is:

    /home/user/Documents/LargeProjects/nerve-hitl-v2   (git repo, 2 commits)
      b270f94 baseline: installed v0.3.2.3 exactly as running
      817e752 feat(hitl-v2): risk-aware layered gate

- The live plugin (`/home/user/.hermes/plugins/nerve`) was never touched; there
  is nothing to roll back in the live environment.
- To abandon the work: delete the `nerve-hitl-v2` scratch repository directory.
- To diff against running code: `git -C nerve-hitl-v2 diff b270f94..HEAD`.
- Optional hygiene (out of scope, requires authorization): remove the stale
  generic `plugin_rule:nerve:low-confidence` line from the live
  `command_allowlist` in `/home/user/.hermes/config.yaml`.
- To install later (requires explicit authorization): stop Hermes, back up
  `/home/user/.hermes/plugins/nerve`, copy this repo over it, restart. A pre-change
  backup already exists at
  `/home/user/.hermes/plugins/hermes-nerve.before-panel-20261008T182840Z-337dfa`.

## Remaining limitations

1. The deterministic catalog covers common agent traffic; genuinely novel
   commands still depend on the model band (by design), and under v2.1 a
   weak-ALLOW in that band escalates — expected operator-touch volume for the
   uncertain band is therefore higher than v2.0 but far below the old gate's
   48.5% interrupt rate. New patterns should be added to `risk.py` as they
   appear in telemetry.
2. Shape keys hash argv, so whitespace-quoting variants of the same command
   (`git push "origin" main` vs `git push origin main`) produce different
   shapes and re-prompt. Conservative by intent.
3. Preference learning counts post-success executions; a tool that succeeds
   without actually doing what was approved would still count (Hermes reports
   success, not semantics). Learned shapes are now content-complete (B1-B4),
   so a learned key authorizes exactly the observed operation.
4. `sudo` inner commands are re-classified but environment-prefix wrappers
   other than the known-safe list fall through to uncertain (conservative).
5. The 0.82-vs-0.90 auto-allow relies on the deterministic layer's coverage;
   the labeled-replay cluster (#19: dangerous calls at p(ALLOW) 0.72-0.81) is
   covered by explicit prohibited/consequential rules in this design, but the
   catalog should be replay-validated periodically against new labeled data.
6. Windows paths are not specially handled (agent runs on Linux here).
7. Approval-rule keys changed material in v2.1 (canonical workdir, content
   digests, args fingerprints), so keys stored by v2.0 sessions will not match
   v2.1 shapes — at install time this is DESIRABLE (all broad grants lapse),
   but operators should expect one re-approval for previously-always'd shapes.
8. The live `profile.json advanced.gate_mode` overrides `config.yaml`; any
   install must update or clear that key or the configured mode silently loses
   (see docs/evidence/live-interruption-origins.md).
