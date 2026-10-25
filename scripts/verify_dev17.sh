#!/usr/bin/env bash
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT"

python3 -m compileall -q .
python3 scripts/verify_release.py
python3 scripts/verify_dev6_benchmark.py

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
export TMP
JOBS="${JEV_TEST_JOBS:-4}"

# Exact bounded-per-file pattern used for release verification. Keeping each
# pytest process independent avoids the known monolithic shutdown/background
# hang while still covering every in-repo test file. Per-file timeout 180s:
# test_patch_0212.py legitimately completes in ~70-87s (CPU-bound context
# curation over 49/60/851 candidates); 75s was below that and produced a
# false rc=124 timeout. 180s is ~2x the observed worst-case legitimate
# runtime, so a genuinely hanging test is still terminated, while slow-but-
# finite tests pass. Validated: test completes rc=0 well below this bound.
printf '%s\n' tests/test_*.py | xargs -P "$JOBS" -I{} bash -c '
  f="$1"
  b=$(basename "$f" .py)
  PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 timeout 180s python3 -m pytest -q "$f" >"$TMP/$b.log" 2>&1
  rc=$?
  echo "$rc $f" >"$TMP/$b.rc"
' _ {} || true

failed=0
for rcfile in "$TMP"/*.rc; do
  rc=$(cut -d' ' -f1 "$rcfile")
  f=$(cut -d' ' -f2- "$rcfile")
  if [ "$rc" = 0 ]; then
    printf 'PASS %s\n' "$f"
  else
    printf 'FAIL %s rc=%s\n' "$f" "$rc"
    b=$(basename "$f" .py)
    tail -80 "$TMP/$b.log" || true
    failed=1
  fi
done

[ "$failed" -eq 0 ] || exit 1
printf 'DEV17_VERIFY=PASS\n'
