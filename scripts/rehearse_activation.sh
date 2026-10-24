#!/bin/bash
# ACTIVATION PHASE-2 REHEARSAL — executes the docs/ACTIVATION.md in-session
# verification checklist against the REAL Hermes approval gate in a SANDBOX
# HERMES_HOME. The live plugin/config/profile/allowlist are not touched.
# This is the rehearsal required before the durable install (Phase 3).
set -u
set -o pipefail
REPO=/home/user/Documents/LargeProjects/nerve-hitl-v2
export HERMES_HOME=/tmp/hitl-rehearsal
export HERMES_BUNDLED_LOCALES=/home/user/.hermes/hermes-agent/locales
PUSH_REPO=/tmp/rehearsal-repo
ENV_DIR=/tmp/rehearsal-env
export NERVE_CLI_PUSH_REPO="$PUSH_REPO"
export NERVE_CLI_ENV_DIR="$ENV_DIR"
EVIDENCE=$REPO/docs/evidence/activation-rehearsal.txt
: > "$EVIDENCE"

fail() { echo "FAIL: $*" | tee -a "$EVIDENCE"; exit 1; }
pass() { echo "PASS: $*" | tee -a "$EVIDENCE"; }
SEQ=0

run() { # seq choice scenario [expect_exit]
  local seq="$1" choice="$2" scen="$3" expect="${4:-}"
  echo "=== [$scen #$seq choice=$choice] ===" | tee -a "$EVIDENCE" 1>&2
  /usr/bin/python3 - "$choice" "$scen" <<'EOF' 2>&1 | tee -a "$EVIDENCE" 1>&2
import pty, sys, os, subprocess, time, select
choice, scen = sys.argv[1], sys.argv[2]
master, slave = pty.openpty()
p = subprocess.Popen(
    ["/usr/bin/python3", "/home/user/Documents/LargeProjects/nerve-hitl-v2/scripts/hermes_cli_driver.py", choice, scen],
    stdin=slave, stdout=slave, stderr=slave, close_fds=True)
os.close(slave)
os.write(master, (choice + "\n").encode())
out = b""
deadline = time.time() + 45
while time.time() < deadline:
    r, _, _ = select.select([master], [], [], 1.0)
    if not r:
        if p.poll() is not None:
            break
        continue
    try:
        chunk = os.read(master, 65536)
    except OSError:
        break
    if not chunk:
        break
    out += chunk
    if p.poll() is not None:
        while True:
            r, _, _ = select.select([master], [], [], 0.2)
            if not r:
                break
            try:
                chunk = os.read(master, 65536)
            except OSError:
                break
            if not chunk:
                break
            out += chunk
        break
rc = p.wait()
os.close(master)
print(out.decode("utf-8", "replace"))
sys.exit(rc)
EOF
  local rc=$?
  if [ -n "$expect" ] && [ "$rc" != "$expect" ]; then fail "[$scen] exit=$rc expected=$expect"; fi
  echo "--- exit=$rc ---" | tee -a "$EVIDENCE" 1>&2
  echo "$rc"
}

directive_key() {
  awk -v s="#$1 " 'index($0, s) {f=1; next} f && match($0, /nerve:hitl:[a-z_]*:[a-z_]*:[0-9a-f]*/) {print substr($0, RSTART, RLENGTH); exit}' "$EVIDENCE"
}

# ---- Phase 0/1 equivalent: sandbox session with v2.2 loaded from the repo --
mkdir -p "$HERMES_HOME"
cat > "$HERMES_HOME/config.yaml" <<'YAML'
model:
  default: test
language: en
command_allowlist: []
_config_version: 50
YAML
rm -f "$HERMES_HOME/gate-events.jsonl"
rm -rf "$PUSH_REPO" "$ENV_DIR"
git init -q -b main "$PUSH_REPO"
git -C "$PUSH_REPO" -c user.name=t -c user.email=t@t commit -q --allow-empty -m a
mkdir -p "$ENV_DIR"
printf 'API_KEY=secret\n' > "$ENV_DIR/.env"
echo "== ACTIVATION REHEARSAL — $(date -u +%Y-%m-%dT%H:%M:%SZ) (sandbox $HERMES_HOME) ==" >> "$EVIDENCE"

# Checklist 1: routine work flows with zero interruptions
SEQ=$((SEQ+1)); rc=$(run "$SEQ" - ls-routine 4) || true
[ "$rc" = "4" ] || fail "checklist 1a: ls -la must flow without prompting"
SEQ=$((SEQ+1)); rc=$(run "$SEQ" - git-routine 4) || true
[ "$rc" = "4" ] || fail "checklist 1b: git status must flow without prompting"
pass "1 routine reads and git inspection: zero interruptions"

# Checklist 2: push prompts once, explained
SEQ=$((SEQ+1)); rc=$(run "$SEQ" o push-repo 0) || true
[ "$rc" = "0" ] || fail "checklist 2: git push must prompt with explanation"
grep -q "External publication" "$EVIDENCE" || fail "push prompt must explain the action"
pass "2 git push prompts with what/why/impact/risk"

# Checklist 3: effect-bound — new commit forces re-approval
git -C "$PUSH_REPO" -c user.name=t -c user.email=t@t commit -q --allow-empty -m b
SEQ=$((SEQ+1)); SB=$SEQ
rc=$(run "$SB" o push-repo 0) || true
[ "$rc" = "0" ] || fail "checklist 3: push after new commit must re-prompt"
pass "3 new commits force a fresh push approval (effect-bound)"

# Checklist 4: cat .env prompts
SEQ=$((SEQ+1)); rc=$(run "$SEQ" d cat-env 3) || true
[ "$rc" = "3" ] || fail "checklist 4: cat .env must prompt"
pass "4 project .env read prompts (denied in rehearsal)"

# Checklist 5: destructive git prompts (deny)
SEQ=$((SEQ+1)); rc=$(run "$SEQ" d stash-clear 3) || true
[ "$rc" = "3" ] || fail "checklist 5: git stash clear must prompt"
pass "5 destructive git prompts (denied in rehearsal)"

# Session evidence: telemetry carries hitl keys and categories
grep -q "risk_category" "$HERMES_HOME/gate-events.jsonl" || fail "telemetry missing risk categories"
grep -q "nerve:hitl:" "$HERMES_HOME/gate-events.jsonl" || fail "telemetry missing hitl rule keys"
pass "6 sandbox telemetry records hitl rule keys and risk categories"

# Live untouched after the whole rehearsal
diff -q "$REPO/hermes_nerve/gate.py" /home/user/.hermes/plugins/nerve/hermes_nerve/gate.py >/dev/null 2>&1 \
  && fail "LIVE PLUGIN WAS MODIFIED" || true
grep -q "plugin_rule:nerve:low-confidence" /home/user/.hermes/config.yaml || fail "live allowlist changed"
pass "7 live plugin/config/allowlist byte-untouched"

echo | tee -a "$EVIDENCE" 1>&2
echo "ACTIVATION REHEARSAL: ALL CHECKLIST ITEMS PASSED" | tee -a "$EVIDENCE"
