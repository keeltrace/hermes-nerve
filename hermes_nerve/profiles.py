"""Versioned profile sidecar with serialized writers and fail-open recovery."""
from __future__ import annotations

from contextlib import contextmanager
import json
import logging
import os
from pathlib import Path
import tempfile
from types import MappingProxyType

from .modules import MODULES
from .paths import hermes_home

logger = logging.getLogger("hermes_nerve.profiles")


class ProfileStateError(RuntimeError):
    """Persisted profile state exists but cannot be trusted."""


PROFILE_NAMES = ("fat_cat", "operator", "lean", "marie_kondo", "custom", "legacy")
_ENABLED = {
    "fat_cat": set(MODULES) - {"shadow_testing", "shared_context"},
    "operator": {"reflex", "nervous", "action_gate", "context_governor", "receipts", "local_learning"},
    "lean": {"reflex", "nervous", "work_supervision", "token_trajectory", "receipts"},
    "marie_kondo": {"reflex", "work_supervision", "token_trajectory", "receipts"},
    "custom": set(),
    "legacy": set(),
}
PROFILES = MappingProxyType({name: MappingProxyType({key: key in enabled for key in MODULES}) for name, enabled in _ENABLED.items()})


def normalize_profile_name(value: object) -> str:
    raw = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    while "__" in raw:
        raw = raw.replace("__", "_")
    return raw


def profile_path(home: str | Path | None = None) -> Path:
    return (Path(home).expanduser() if home is not None else hermes_home()) / "nerve" / "profile.json"


def validate_profile(data: dict) -> dict:
    if not isinstance(data, dict) or set(data) - {"version", "nerve_profile", "nerve_modules", "advanced"}:
        raise ValueError("Invalid profile document")
    if type(data.get("version")) is not int or data["version"] != 1:
        raise ValueError("Unsupported profile version")
    name = normalize_profile_name(data.get("nerve_profile"))
    if name not in PROFILE_NAMES:
        raise ValueError("Unknown Nerve profile")
    modules = data.get("nerve_modules", {})
    advanced = data.get("advanced", {})
    if not isinstance(modules, dict) or any(k not in MODULES or type(v) is not bool for k, v in modules.items()):
        raise ValueError("Module overrides must contain known module IDs and booleans")
    if not isinstance(advanced, dict) or any(not isinstance(k, str) or k.startswith("nerve_") for k in advanced):
        raise ValueError("Invalid advanced settings")
    return json.loads(json.dumps(dict(data, nerve_profile=name, nerve_modules=modules, advanced=advanced), allow_nan=False))


def _read_profile(path: Path) -> dict:
    with path.open(encoding="utf-8") as stream:
        return validate_profile(json.load(stream))


def load_profile(home: str | Path | None = None) -> dict | None:
    """Load the profile; recover from a valid backup or fail closed on corruption."""
    path = profile_path(home)
    try:
        return _read_profile(path)
    except FileNotFoundError:
        return None
    except (json.JSONDecodeError, ValueError, OSError) as exc:
        backup = path.with_suffix(".json.bak")
        try:
            recovered = _read_profile(backup)
        except FileNotFoundError as backup_exc:
            logger.error("Invalid Nerve profile %s; no valid backup exists: %s", path, exc)
            raise ProfileStateError(f"Invalid Nerve profile and no valid backup: {path}") from backup_exc
        except (json.JSONDecodeError, ValueError, OSError) as backup_exc:
            logger.error("Invalid Nerve profile %s and invalid backup %s: primary=%s backup=%s", path, backup, exc, backup_exc)
            raise ProfileStateError(f"Invalid Nerve profile and backup: {path}") from backup_exc
        logger.warning("Recovered invalid Nerve profile %s from backup %s: %s", path, backup, exc)
        return recovered


@contextmanager
def _lock(path: Path):
    with path.open("a+b") as stream:
        if os.name == "nt":
            import msvcrt
            stream.seek(0); stream.write(b"\0"); stream.flush(); stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if os.name == "nt":
                stream.seek(0); msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _atomic_write(path: Path, payload: bytes):
    fd, temporary = tempfile.mkstemp(prefix="." + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def save_profile(data: dict, home: str | Path | None = None) -> Path:
    payload = (json.dumps(validate_profile(data), indent=2, sort_keys=True, allow_nan=False) + "\n").encode()
    path = profile_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _lock(path.with_suffix(".lock")):
        if path.exists():
            # Only promote a known-valid primary into the recovery slot.
            # If the primary is corrupt, preserve any existing good backup
            # instead of overwriting the last recoverable profile with bad bytes.
            try:
                _read_profile(path)
            except (json.JSONDecodeError, ValueError, OSError):
                pass
            else:
                _atomic_write(path.with_suffix(".json.bak"), path.read_bytes())
        _atomic_write(path, payload)
        if os.name != "nt":
            fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
    return path


def snapshot_profile_state(home: str | Path | None = None) -> dict[str, bytes | None]:
    """Capture the exact persisted primary/backup bytes for transactional rollback."""
    path = profile_path(home)
    backup = path.with_suffix(".json.bak")
    path.parent.mkdir(parents=True, exist_ok=True)
    with _lock(path.with_suffix(".lock")):
        return {
            "primary": path.read_bytes() if path.exists() else None,
            "backup": backup.read_bytes() if backup.exists() else None,
        }


def restore_profile_state(snapshot: dict[str, bytes | None], home: str | Path | None = None) -> None:
    """Restore an exact snapshot without promoting attempted state into backup."""
    path = profile_path(home)
    backup = path.with_suffix(".json.bak")
    path.parent.mkdir(parents=True, exist_ok=True)
    with _lock(path.with_suffix(".lock")):
        for candidate, key in ((path, "primary"), (backup, "backup")):
            payload = snapshot.get(key)
            if payload is None:
                try:
                    candidate.unlink()
                except FileNotFoundError:
                    pass
            else:
                _atomic_write(candidate, payload)
        if os.name != "nt":
            fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)


def clear_profile(home: str | Path | None = None) -> None:
    """Return to Legacy and clear stale profile recovery authority."""
    path = profile_path(home)
    backup = path.with_suffix(".json.bak")
    path.parent.mkdir(parents=True, exist_ok=True)
    with _lock(path.with_suffix(".lock")):
        for candidate in (path, backup):
            try:
                candidate.unlink()
            except FileNotFoundError:
                pass
