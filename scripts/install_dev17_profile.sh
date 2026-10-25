#!/usr/bin/env bash
set -eu

PROFILE="${1:-abtest-nerve-dev17}"
case "$PROFILE" in ""|.|..|*[!a-zA-Z0-9_.-]*) echo "Invalid profile name" >&2; exit 1;; esac
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
DEST="$HOME/.hermes/profiles/$PROFILE/plugins/hermes-nerve"
LEGACY="$HOME/.hermes/profiles/$PROFILE/plugins/hermes-jev"
STAMP=$(date +%Y%m%dT%H%M%S)

# Freeze only the explicit runtime payload. Native admission must inspect the
# same bytes we later deploy, not the developer checkout (or a later reread).
python3 "$ROOT/scripts/install_runtime_payload.py" --check "$ROOT"
STAGE=$(mktemp -d)
trap 'rm -rf -- "$STAGE"' EXIT
python3 "$ROOT/scripts/install_runtime_payload.py" "$ROOT" "$STAGE"
hermes -p "$PROFILE" plugins validate "$STAGE"

# Import/version failure must also leave both existing deployments untouched.
VERSION=$(python3 -c 'import sys; sys.path.insert(0, sys.argv[1]); from hermes_nerve.provenance import VERSION; print(VERSION)' "$STAGE")

# Preflight both names before moving either deployment. Otherwise mv can nest
# an old deployment inside a pre-existing timestamped backup directory.
ARCHIVE="$(dirname "$DEST")/__nerve_backups__"
BACKUP="$ARCHIVE/hermes-nerve.pre-dev17-${STAMP}"
LEGACY_BACKUP="$ARCHIVE/hermes-jev.pre-nerve-${STAMP}"
# Retain collision checks for older installers' sibling backup locations too.
for BACKUP_PATH in "$BACKUP" "$LEGACY_BACKUP" "${DEST}.pre-dev17-${STAMP}" "${LEGACY}.pre-nerve-${STAMP}"; do
  if [ -e "$BACKUP_PATH" ] || [ -L "$BACKUP_PATH" ]; then
    printf 'Refusing existing backup path: %s\n' "$BACKUP_PATH" >&2
    exit 1
  fi
done

# Limited filesystem/config compensation only; PM effects remain unqualified.
python3 "$ROOT/scripts/install_profile_transaction.py" "$ROOT" "$STAGE" \
  "$HOME/.hermes/profiles/$PROFILE" "$BACKUP" "$LEGACY_BACKUP" "$PROFILE"
printf 'Installed Nerve %s into %s\n' "$VERSION" "$DEST"
