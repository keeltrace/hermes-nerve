#!/bin/bash
# Round-4 real-CLI verification: authorization-integrity pass.
set -u
set -o pipefail
REPO=/home/user/Documents/LargeProjects/nerve-hitl-v2
export HERMES_HOME=/tmp/hitl-home-r4
export HERMES_BUNDLED_LOCALES=/home/user/.hermes/hermes-agent/locales
PUSH_REPO=/tmp/r4-push-repo
ENV_DIR=/tmp/r4-env-proj
export NERVE_CLI_PUSH_REPO="$PUSH_REPO"
export NERVE_CLI_ENV_DIR="$ENV_DIR"
EVIDENCE=$REPO/docs/evidence/cli-round4.txt
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

mkdir -p "$HERMES_HOME"
cat > "$HERMES_HOME/config.yaml" <<'YAML'
model:
  default: test
language: en
command_allowlist: []
_config_version: 50
YAML
rm -f "$HERMES_HOME/gate-events.jsonl"

# temp repo + temp env project
rm -rf "$PUSH_REPO" "$ENV_DIR"
git init -q -b main "$PUSH_REPO"
git -C "$PUSH_REPO" -c user.name=t -c user.email=t@t commit -q --allow-empty -m a
mkdir -p "$ENV_DIR"
printf 'API_KEY=secret\n' > "$ENV_DIR/.env"

# V1: repeated POST never sticks to a stored key
SEQ=$((SEQ+1)); S1=$SEQ
rc=$(run "$S1" a curl-post 0) || true
[ "$rc" = "0" ] || fail "POST should approve once"
SEQ=$((SEQ+1)); S2=$SEQ
rc=$(run "$S2" o curl-post 0) || true
[ "$rc" = "0" ] || fail "repeated POST must PROMPT again (non-reusable key)"
K1=$(directive_key "$S1"); K2=$(directive_key "$S2")
[ -n "$K1" ] && [ -n "$K2" ] || fail "POST keys missing"
[ "$K1" != "$K2" ] || fail "repeated POST reused key"
pass "V1 repeated API POST re-approves (keys differ: ${K1##*:} vs ${K2##*:})"

# V2: push --all effect-bound (new branch -> new approval)
SEQ=$((SEQ+1)); S3=$SEQ
rc=$(run "$S3" a push-all 0) || true
[ "$rc" = "0" ] || fail "push --all should approve once"
KA=$(directive_key "$S3")
git -C "$PUSH_REPO" checkout -q -b extra
git -C "$PUSH_REPO" -c user.name=t -c user.email=t@t commit -q --allow-empty -m b
SEQ=$((SEQ+1)); S4=$SEQ
rc=$(run "$S4" o push-all 0) || true
[ "$rc" = "0" ] || fail "push --all after new branch must PROMPT"
KB=$(directive_key "$S4")
[ "$KA" != "$KB" ] || fail "push --all reused key after ref change"
pass "V2 push --all re-approves after ref set changes"

# V3: path-qualified executable prompts even though basename is trusted
SEQ=$((SEQ+1))
rc=$(run "$SEQ" d path-qualified 3) || true
[ "$rc" = "3" ] || fail "./ls must prompt (model band / provider error)"
pass "V3 ./ls -la prompts (never trusted by basename)"

# V4: read_file with 10 sensitive paths prompts with explanation
SEQ=$((SEQ+1))
rc=$(run "$SEQ" d read-multi 3) || true
[ "$rc" = "3" ] || fail "10-path read_file must prompt"
pass "V4 10-path sensitive read_file prompts"

# V5: project .env read prompts
SEQ=$((SEQ+1))
rc=$(run "$SEQ" d cat-env 3) || true
[ "$rc" = "3" ] || fail "cat .env must prompt"
grep -q "Sensitive file access" "$EVIDENCE" || fail ".env message must explain"
pass "V5 project .env read prompts with explanation"

echo | tee -a "$EVIDENCE" 1>&2
echo "ALL ROUND-4 CLI TESTS PASSED" | tee -a "$EVIDENCE"
