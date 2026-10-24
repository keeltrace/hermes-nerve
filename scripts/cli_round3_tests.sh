#!/bin/bash
# Round-3 real-CLI tests: effect-bound push approvals (REAL commits in a temp
# git repo), destructive-git gating, read_file sensitive probes.
set -u
set -o pipefail
REPO=/home/user/Documents/LargeProjects/nerve-hitl-v2
export HERMES_HOME=/tmp/hitl-home-r3
export HERMES_BUNDLED_LOCALES=/home/user/.hermes/hermes-agent/locales
PUSH_REPO=/tmp/r3-push-repo
export NERVE_CLI_PUSH_REPO="$PUSH_REPO"
EVIDENCE=$REPO/docs/evidence/cli-round3.txt
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

directive_key() { # seq -> rule_key from that numbered block of the transcript
  awk -v s="#$1 " 'index($0, s) {f=1; next} f && match($0, /nerve:hitl:[a-z_]*:[a-z_]*:[0-9a-f]*/) {print substr($0, RSTART, RLENGTH); exit}' "$EVIDENCE"
}

mkdir -p "$HERMES_HOME"
cat > "$HERMES_HOME/config.yaml" <<'YAML'
model:
  default: test
language: en
command_allowlist: []
_config_version: 50
YAML
rm -f "$HERMES_HOME/gate-events.jsonl"

# real temp git repo with two sequential states
rm -rf "$PUSH_REPO"
git init -q -b main "$PUSH_REPO"
git -C "$PUSH_REPO" -c user.name=t -c user.email=t@t commit -q --allow-empty -m "commit A"
SHA_A=$(git -C "$PUSH_REPO" rev-parse HEAD)

# C1: [a]lways on push of commit A
SEQ_A=$SEQ
SEQ=$((SEQ+1)); rc=$(run "$SEQ" a push-repo 0) || true
[ "$rc" = "0" ] || fail "push of commit A should approve"
KEY_A=$(grep -o "plugin_rule:nerve:hitl:[a-z_]*:terminal:[0-9a-f]*" "$HERMES_HOME/config.yaml" | head -1)
[ -n "$KEY_A" ] || fail "no allowlist key stored for push A"

# C2: NEW COMMIT -> same command must PROMPT again (different effect)
git -C "$PUSH_REPO" -c user.name=t -c user.email=t@t commit -q --allow-empty -m "commit B"
SHA_B=$(git -C "$PUSH_REPO" rev-parse HEAD)
[ "$SHA_A" != "$SHA_B" ] || fail "test setup: commits did not change"
SEQ_B=$((SEQ+1))
SEQ=$((SEQ+1)); SEQ_B=$SEQ; rc=$(run "$SEQ" o push-repo 0) || true
[ "$rc" = "0" ] || fail "push of commit B must PROMPT after always on commit A"
KEY_B=$(directive_key "$SEQ_B")
[ -n "$KEY_B" ] || fail "push-B directive key not found"
FP_A="${KEY_A#plugin_rule:nerve:hitl:}"
case "$KEY_B" in
  *"$FP_A") fail "push-B reused push-A's key fingerprint (effect not bound): $KEY_B" ;;
esac
pass "C1 effect-bound push: new commit -> re-approval ($KEY_A vs $KEY_B)"

# C3: destructive git prompts and can be denied
SEQ=$((SEQ+1)); rc=$(run "$SEQ" d stash-clear 3) || true
[ "$rc" = "3" ] || fail "git stash clear must prompt (denied -> exit 3)"
pass "C2 git stash clear prompts as consequential (denied in test)"

# C4: global git config write prompts
SEQ=$((SEQ+1)); rc=$(run "$SEQ" d git-global 3) || true
[ "$rc" = "3" ] || fail "git config --global write must prompt"
pass "C3 global git config write prompts"

# C5: read_file sensitive path prompts (bypass closed)
SEQ=$((SEQ+1)); rc=$(run "$SEQ" d read-sensitive 3) || true
[ "$rc" = "3" ] || fail "read_file ~/.ssh/id_ed25519 must prompt"
grep -q "Sensitive file access" "$EVIDENCE" || fail "read_file message must explain sensitive access"
pass "C4 read_file sensitive path prompts with explanation (bypass closed)"

# C6: same effect (commit B still HEAD) -> SAME key (stable), prompts again
# because C2 was approved [o]nce only
SEQ=$((SEQ+1)); SEQ_C=$SEQ
rc=$(run "$SEQ" o push-repo 0) || true
[ "$rc" = "0" ] || fail "re-push of commit B should still prompt after [o]nce"
KEY_B2=$(directive_key "$SEQ_C")
[ "$KEY_B2" = "$KEY_B" ] || fail "same effect must keep a stable key: $KEY_B2 != $KEY_B"
pass "C5 same-effect stability: re-push of commit B reuses key $KEY_B"

echo | tee -a "$EVIDENCE" 1>&2
echo "ALL ROUND-3 CLI TESTS PASSED" | tee -a "$EVIDENCE"
