#!/bin/bash
# Real-CLI authorization-reuse test sequence (v2.1 release blockers).
# Fresh sandbox per run. Every prompt goes through the REAL Hermes
# request_tool_approval pty flow; every assertion is about whether a SECOND,
# DIFFERENT operation still prompts after an [a]lways on the first.
set -u
set -o pipefail
REPO=/home/user/Documents/LargeProjects/nerve-hitl-v2
export HERMES_HOME=/tmp/hitl-home-r2
export HERMES_BUNDLED_LOCALES=/home/user/.hermes/hermes-agent/locales
EVIDENCE=$REPO/docs/evidence/cli-reuse-v2.1.txt
: > "$EVIDENCE"

fail() { echo "FAIL: $*" | tee -a "$EVIDENCE"; exit 1; }
pass() { echo "PASS: $*" | tee -a "$EVIDENCE"; }

run() { # choice scenario [expect_exit]
  local choice="$1" scen="$2" expect="${3:-}"
  echo "=== [$scen] choice=$choice ===" | tee -a "$EVIDENCE" 1>&2
  /usr/bin/python3 - "$choice" "$scen" <<'EOF' 2>&1 | tee -a "$EVIDENCE" 1>&2
import pty, sys, os, subprocess, time, select
choice, scen = sys.argv[1], sys.argv[2]
master, slave = pty.openpty()
p = subprocess.Popen(
    ["/usr/bin/python3", "/home/user/Documents/LargeProjects/nerve-hitl-v2/scripts/hermes_cli_driver.py", choice, scen],
    stdin=slave, stdout=slave, stderr=slave, close_fds=True)
os.close(slave)
# feed the choice into the tty line discipline; input() picks it up at the prompt
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
        # drain remaining output
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

# fresh sandbox config
mkdir -p "$HERMES_HOME"
cat > "$HERMES_HOME/config.yaml" <<'YAML'
model:
  default: test
language: en
command_allowlist: []
_config_version: 50
YAML
rm -f "$HERMES_HOME/gate-events.jsonl"

# R1 numeric-target reuse: [a]lways on kill -9 4242 must not cover kill -9 1
rc=$(run a kill-4242 0) || true
[ "$rc" = "0" ] || fail "kill-4242 should approve"
grep -q "plugin_rule:nerve:hitl:" "$HERMES_HOME/config.yaml" || fail "no allowlist entry stored"
KEY1=$(grep -o "plugin_rule:nerve:hitl:[a-z_]*:terminal:[0-9a-f]*" "$HERMES_HOME/config.yaml" | head -1)
rc=$(run o kill-1 0) || true
[ "$rc" = "0" ] || fail "kill-1 must still PROMPT after always on kill-4242"
KEY2=$(awk '/=== \[kill-1\]/,/^--- exit/' "$EVIDENCE" | grep -o 'nerve:hitl:[a-z_]*:terminal:[0-9a-f]*' | head -1)
[ -n "$KEY2" ] || fail "kill-1 directive key not found in transcript"
[ "$KEY1" != "$KEY2" ] || fail "kill-1 reused kill-4242's key"
pass "R1 numeric targets: kill -9 1 re-prompts after [a]lways on kill -9 4242 ($KEY1 != $KEY2)"

# R2 pipeline reuse: [a]lways on curl A|head must not cover curl B|head
rc=$(run a curl-a 0) || true
[ "$rc" = "0" ] || fail "curl-a should approve"
KEYA=$(grep -o "plugin_rule:nerve:hitl:[a-z_]*:terminal:[0-9a-f]*" "$HERMES_HOME/config.yaml" | tail -1)
rc=$(run o curl-b 0) || true
[ "$rc" = "0" ] || fail "curl-b must still PROMPT after always on curl-a"
KEYB=$(awk '/=== \[curl-b\]/,/^--- exit/' "$EVIDENCE" | grep -o 'nerve:hitl:[a-z_]*:terminal:[0-9a-f]*' | head -1)
[ -n "$KEYB" ] || fail "curl-b directive key not found"
[ "$KEYA" != "$KEYB" ] || fail "curl-b reused curl-a's key"
pass "R2 pipelines: mirror-B pipeline re-prompts after [a]lways on mirror-A ($KEYA != $KEYB)"

# R3 file-write content reuse: [a]lways on benign write must not cover evil content
rc=$(run a write-benign 0) || true
[ "$rc" = "0" ] || fail "write-benign should approve"
KEYC=$(grep -o "plugin_rule:nerve:hitl:[a-z_]*:write_file:[0-9a-f]*" "$HERMES_HOME/config.yaml" | tail -1)
[ -n "$KEYC" ] || fail "no write_file key stored"
rc=$(run o write-evil 0) || true
[ "$rc" = "0" ] || fail "write-evil must still PROMPT after always on write-benign"
KEYD=$(awk '/=== \[write-evil\]/,/^--- exit/' "$EVIDENCE" | grep -o 'nerve:hitl:[a-z_]*:write_file:[0-9a-f]*' | head -1)
[ -n "$KEYD" ] || fail "write-evil directive key not found"
[ "$KEYC" != "$KEYD" ] || fail "write-evil reused write-benign's key"
pass "R3 file writes: different content to the same path re-prompts ($KEYC != $KEYD)"

# R4 git mutations leave the safe-read path (auto-allowed as PROJECT, never SAFE)
rc=$(run - git-config-set 4) || true
[ "$rc" = "4" ] || fail "git config KEY VALUE should auto-allow as project work (no prompt)"
grep -q '"reason": "deterministic:authorized_project_work"' "$HERMES_HOME/gate-events.jsonl" \
  || fail "git config set did not log authorized_project_work"
rc=$(run - git-stash 4) || true
[ "$rc" = "4" ] || fail "git stash should auto-allow as project work"
rc=$(run - git-config-get 4) || true
[ "$rc" = "4" ] || fail "git config --get should auto-allow as safe read"
grep -q '"reason": "deterministic:safe_routine"' "$HERMES_HOME/gate-events.jsonl" \
  || fail "git config --get did not log safe_routine"
pass "R4 git: config-set/stash = project work (no longer safe-read); config --get still safe"

# R5 workdir lie: claiming / as workdir must not make system files project-local
# (classified PROHIBITED in enforce mode -> hard block, exit 5)
rc=$(run - workdir-lie 5) || true
[ "$rc" = "5" ] || fail "rm /etc/hosts.allow with workdir=/ must be blocked (exit 5), got $rc"
grep -q "Destructive deletion" "$EVIDENCE" || fail "block message missing concrete explanation"
pass "R5 workdir trust: workdir=/ does not grant project scope; rm of /etc file BLOCKED with explanation"

# R6 legacy regression: [a]lways on push feature branch does not cover push main
rc=$(run a push 0) || true
rc=$(run o push-main 0) || true
[ "$rc" = "0" ] || fail "push-main must still prompt"
pass "R6 regression: push-main still prompts after [a]lways on push feature-branch"

echo | tee -a "$EVIDENCE"
echo "ALL CLI REUSE TESTS PASSED" | tee -a "$EVIDENCE"
