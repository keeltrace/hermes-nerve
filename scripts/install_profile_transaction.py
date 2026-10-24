"""Limited single-writer filesystem/config recovery, NOT native PM undo.

No crash durability or concurrent-writer guarantee. Reject symlinks/special files.
Recovery validates every saved preimage before displacing any remaining install.
Quarantine stays on the same filesystem as the destination (no EXDEV).
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def safe_path(path):
    for p in (path, *path.parents):
        if p.is_symlink():
            raise ValueError(f"symlink refused: {p}")


def fingerprint(path):
    safe_path(path)
    if not path.exists() and not path.is_symlink():
        return None
    if path.is_symlink():
        raise ValueError(f"symlink refused: {path}")
    mode = path.lstat().st_mode
    if stat.S_ISREG(mode):
        return ("file", stat.S_IMODE(mode), hashlib.sha256(path.read_bytes()).hexdigest())
    if stat.S_ISDIR(mode):
        return ("dir", stat.S_IMODE(mode), {p.name: fingerprint(p) for p in sorted(path.iterdir())})
    raise ValueError(f"special file refused: {path}")


def payload_fingerprint(path):
    """Fingerprint of payload content only; ignores generated __pycache__/.pyc."""
    def walk(p):
        if p.is_symlink():
            raise ValueError(f"symlink refused: {p}")
        if not p.exists():
            return None
        mode = p.lstat().st_mode
        if stat.S_ISREG(mode):
            if p.suffix == ".pyc":
                return None
            return ("file", stat.S_IMODE(mode), hashlib.sha256(p.read_bytes()).hexdigest())
        if stat.S_ISDIR(mode):
            if p.name == "__pycache__":
                return None
            entries = {}
            for child in sorted(p.iterdir()):
                value = walk(child)
                if value is not None:
                    entries[child.name] = value
            return ("dir", stat.S_IMODE(mode), entries)
        raise ValueError(f"special file refused: {p}")
    return walk(path)


def command(*args):
    subprocess.run([str(a) for a in args], check=True)


def validate_profile_name(name):
    if not name or not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
        raise ValueError("invalid profile name")
    if name in (".", ".."):
        raise ValueError("invalid profile name")


def main():
    if len(sys.argv) != 7:
        raise SystemExit("usage: install_profile_transaction.py ROOT STAGE PROFILE_PATH BACKUP LEGACY_BACKUP PROFILE_NAME")
    root, stage, profile, backup, legacy_backup = map(Path, sys.argv[1:6])
    profile_name = sys.argv[6]
    validate_profile_name(profile_name)

    dest = profile / "plugins" / "hermes-nerve"
    legacy = profile / "plugins" / "hermes-jev"
    config = profile / "config.yaml"
    archive = dest.parent / "__nerve_backups__"

    for path in (profile, dest, legacy, config, backup, legacy_backup, root, stage):
        safe_path(path)
    if backup.parent != archive or legacy_backup.parent != archive:
        raise ValueError("backup parent mismatch")
    if not backup.name.startswith(dest.name + ".pre-dev17-") or not legacy_backup.name.startswith(legacy.name + ".pre-nerve-"):
        raise ValueError("backup name mismatch")
    if backup.exists() or legacy_backup.exists():
        raise ValueError("backup collision")
    if not profile.is_dir():
        raise ValueError("profile must be a directory")
    if not stage.is_dir():
        raise ValueError("stage must be a directory")

    before = {p: fingerprint(p) for p in (dest, legacy, config)}
    for p in (dest, legacy):
        preimage = before[p]
        if preimage is not None and preimage[0] != "dir":
            raise ValueError("install must be a directory")
    config_preimage = before[config]
    if config_preimage is not None and config_preimage[0] != "file":
        raise ValueError("config must be a regular file")

    # A profile may itself be a mount point. Its parent is not sufficient
    # to ensure atomic restoration of config.yaml without EXDEV.
    tx = Path(tempfile.mkdtemp(prefix="nerve-recovery-", dir=str(profile)))
    saved = tx / "config.before"
    moved = []
    deploy_started = False
    mutated = False
    preserve = False
    try:
        if before[config] is not None:
            shutil.copy2(config, saved)
            if fingerprint(saved) != before[config]:
                raise ValueError("config snapshot mismatch")
        archive.mkdir(mode=0o700, parents=True, exist_ok=True)
        for path in (dest, legacy):
            if path.exists() and path.stat().st_dev != archive.stat().st_dev:
                raise ValueError("cross-filesystem deployment backups are not supported")
        mutated = True  # disable can mutate and fail
        if before[legacy] is not None:
            command("hermes", "-p", profile_name, "plugins", "disable", "hermes-jev")
            command("mv", "-T", "--", legacy, legacy_backup)
            moved.append((legacy, legacy_backup))
        if before[dest] is not None:
            command("mv", "-T", "--", dest, backup)
            moved.append((dest, backup))
        # Recovery tolerates an absent destination and quarantines a directory
        # created just before mkdir raises; marking only on return loses it.
        deploy_started = True
        dest.mkdir(parents=True)
        command("python3", root / "scripts" / "install_runtime_payload.py", stage, dest)
        payload_fp = payload_fingerprint(stage)
        dest_fp = payload_fingerprint(dest)
        if payload_fp is None or dest_fp is None or payload_fp[2] != dest_fp[2]:
            raise ValueError("deployed payload differs from admitted stage")
        command("hermes", "-p", profile_name, "plugins", "enable", "nerve")
        return 0
    except BaseException as exc:
        status = exc.returncode if isinstance(exc, subprocess.CalledProcessError) else 1
        print(f"Installation failed: {exc}", file=sys.stderr)
        if not mutated:
            return status
        try:
            safe_path(config)
            if config.exists() and not config.is_file():
                raise ValueError("config changed type")
            if before[config] is not None:
                if not saved.exists() or fingerprint(saved) != before[config]:
                    raise ValueError("missing/corrupt config preimage")
            for original, saved_tree in moved:
                if not saved_tree.exists() or fingerprint(saved_tree) != before[original]:
                    raise ValueError(f"missing/corrupt install preimage: {saved_tree}")
                safe_path(original)
                if original.exists() and not (deploy_started and original == dest):
                    raise ValueError("occupied recovery destination")
            if deploy_started:
                safe_path(dest)
                if dest.exists():
                    if not dest.is_dir():
                        raise ValueError("destination changed type")
                    quarantine = archive / f"{dest.name}.failed-candidate-{os.getpid()}-{int(time.time()*1000)}"
                    if quarantine.exists():
                        raise ValueError("quarantine collision")
                    os.rename(dest, quarantine)  # same filesystem as dest
            for original, saved_tree in reversed(moved):
                command("mv", "-T", "--", saved_tree, original)
            if before[config] is None:
                config.unlink(missing_ok=True)
            else:
                replacement = tx / "config.restore"
                shutil.copy2(saved, replacement)
                os.replace(replacement, config)
            # verify
            for p, expected in before.items():
                if fingerprint(p) != expected:
                    raise ValueError("post-recovery verification failed")
        except BaseException as recovery_error:
            preserve = True
            print(f"RECOVERY INCOMPLETE: {recovery_error}; evidence: {tx}", file=sys.stderr)
            return 70
        print("Limited filesystem/config recovery verified; native effects unqualified.", file=sys.stderr)
        return status
    finally:
        if not preserve:
            shutil.rmtree(tx, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
