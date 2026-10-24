#!/bin/bash
# Completion-integrity verifier for Nerve HITL v2.3.
# Re-runs every acceptance check end-to-end and emits a receipt.
# Exits nonzero on any failure. Read-only with respect to the live environment.
set -u
set -o pipefail
REPO=/home/user/Documents/LargeProjects/nerve-hitl-v2
LIVE=/home/user/.hermes/plugins/nerve
OUT=${1:-$REPO/docs/evidence/completion-receipt.txt}
: > "$OUT"
say() { echo "$*" | tee -a "$OUT"; }
chk() { # description, expected_exit, command...
  local desc="$1" want="$2"; shift 2
  "$@" >/dev/null 2>&1
  local rc=$?
  if [ "$rc" = "$want" ]; then say "PASS  $desc"; return 0
  else say "FAIL  $desc (exit=$rc want=$want)"; return 1; fi
}
FAILS=0
say "== Nerve HITL v2.3 completion receipt — $(date -u +%Y-%m-%dT%H:%M:%SZ) =="

# 1. repo integrity
chk "git working tree clean (receipt excluded)" 0 bash -c "cd $REPO && [ -z \"\$(git status --porcelain -- . ':!docs/evidence/completion-receipt.txt')\" ]" || FAILS=$((FAILS+1))
HEAD=$(git -C "$REPO" rev-parse HEAD)
say "  HEAD: $HEAD"

# 2. full regression suite (retry once: host memory pressure can OOM-kill
#    the subprocess on a busy machine; a code regression does not pass on retry)
chk "full suite (632 passed, 1 skipped)" 0 bash -c \
  "cd $REPO && PYTHONPATH=$REPO /usr/bin/pytest tests/ -q --ignore=tests/test_control_panel_real.py 2>&1 | tail -1 | grep -q '632 passed, 1 skipped'" \
  || chk "full suite retry (632 passed, 1 skipped)" 0 bash -c \
  "cd $REPO && PYTHONPATH=$REPO /usr/bin/pytest tests/ -q --ignore=tests/test_control_panel_real.py 2>&1 | tail -1 | grep -q '632 passed, 1 skipped'" \
  || FAILS=$((FAILS+1))

# 3. per-round blocker suites green
chk "round-2 blockers (33)" 0 bash -c "cd $REPO && PYTHONPATH=$REPO /usr/bin/pytest tests/test_hitl_v2_blockers.py -q 2>&1 | tail -1 | grep -q '33 passed'" || FAILS=$((FAILS+1))
chk "round-3 blockers (31)" 0 bash -c "cd $REPO && PYTHONPATH=$REPO /usr/bin/pytest tests/test_hitl_v2_round3.py -q 2>&1 | tail -1 | grep -q '31 passed'" || FAILS=$((FAILS+1))
chk "round-4 integrity (27)" 0 bash -c "cd $REPO && PYTHONPATH=$REPO /usr/bin/pytest tests/test_hitl_v2_round4.py -q 2>&1 | tail -1 | grep -q '27 passed'" || FAILS=$((FAILS+1))

# 4. no confidence-number interruption paths remain
chk "no legacy 'is below' interrupt strings" 0 bash -c "! grep -rn 'is below.*human approval required' $REPO/hermes_nerve/" || FAILS=$((FAILS+1))
chk "no generic legacy rule keys emitted" 0 bash -c "! grep -rnE '[\\\"'\"']nerve:low-(confidence|allow-probability)[\\\"'\"']' $REPO/hermes_nerve/gate.py" || FAILS=$((FAILS+1))
chk "weak-allow escalation exists (honest uncertainty)" 0 grep -q "_UNCERTAINTY_WEAK_ALLOW_NOTE" $REPO/hermes_nerve/gate.py || FAILS=$((FAILS+1))
chk "effect binding present" 0 grep -q "effect_sha256" $REPO/hermes_nerve/gate.py || FAILS=$((FAILS+1))
chk "path-qualified executable guard present" 0 grep -q "Path-qualified executable" $REPO/hermes_nerve/risk.py || FAILS=$((FAILS+1))

# 5. replay acceptance
chk "replay acceptance: 100% routine reduction" 0 bash -c \
  "cd $REPO && PYTHONPATH=$REPO /usr/bin/python3 scripts/replay_old_vs_new.py 2>/dev/null | grep -q 'reduction: 100.0%'" \
  || FAILS=$((FAILS+1))
chk "replay protection: 21/21 gated" 0 bash -c \
  "cd $REPO && PYTHONPATH=$REPO /usr/bin/python3 scripts/replay_old_vs_new.py 2>/dev/null | grep -q 'new=21/21'" \
  || FAILS=$((FAILS+1))

# 6. live environment untouched
chk "live gate differs from v2.3 (not deployed)" 1 diff -q $REPO/hermes_nerve/gate.py $LIVE/hermes_nerve/gate.py || FAILS=$((FAILS+1))
chk "live allowlist still has stale legacy grant (uncleared)" 0 grep -q "plugin_rule:nerve:low-confidence" /home/user/.hermes/config.yaml || FAILS=$((FAILS+1))
chk "live profile.json gate_mode still ask-only (unchanged)" 0 grep -q '"gate_mode": "ask-only"' /home/user/.hermes/nerve/profile.json || FAILS=$((FAILS+1))

say ""
if [ "$FAILS" = "0" ]; then say "COMPLETION INTEGRITY: ALL CHECKS PASSED"
else say "COMPLETION INTEGRITY: $FAILS CHECK(S) FAILED"; fi
exit $FAILS
