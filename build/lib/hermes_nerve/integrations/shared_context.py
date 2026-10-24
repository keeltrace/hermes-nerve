"""Adapter for the separately maintained HermesContextBus plugin."""
from __future__ import annotations

import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Any

from ..paths import hermes_home

PLUGIN_DIRNAME = "hermes-context-bus"
MIN_VERSION = (0, 2, 0)


def plugin_dir(home: Path | None = None) -> Path:
    return (home or hermes_home()) / "plugins" / PLUGIN_DIRNAME


def _version_tuple(value: str) -> tuple[int, int, int] | None:
    match = re.match(r"^\s*(\d+)\.(\d+)\.(\d+)(?:[-+].*)?\s*$", str(value or ""))
    return tuple(map(int, match.groups())) if match else None


def _manifest_info(target: Path) -> dict[str, Any]:
    manifest = target / "plugin.yaml"
    info = {"manifest": manifest.exists(), "name": "", "version": "", "health_tool": False}
    if not manifest.exists():
        return info
    text = manifest.read_text(errors="replace")
    name_match = re.search(r"(?m)^name:\s*[\"']?([^\"'\n]+)", text)
    info["name"] = name_match.group(1).strip() if name_match else ""
    match = re.search(r"(?m)^version:\s*[\"']?([^\"'\n]+)", text)
    info["version"] = match.group(1).strip() if match else ""
    in_tools = False
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not line.startswith((" ", "\t")):
            in_tools = line.strip() == "provides_tools:"
            continue
        if in_tools and re.match(r"^\s*-\s*shared_context_health\s*$", line):
            info["health_tool"] = True
            break
    return info


def _sqlite_health(home: Path | None = None) -> dict[str, Any]:
    root = home or hermes_home()
    db = root / "shared-context" / "context.db"
    if not db.exists():
        return {"db_exists": False, "schema_version": None}
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=1)
        try:
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            statuses = {}
            try:
                for state, count in conn.execute("SELECT status,COUNT(*) FROM context_messages GROUP BY status"):
                    statuses[str(state)] = int(count)
            except sqlite3.Error:
                pass
            return {"db_exists": True, "schema_version": version, "statuses": statuses, "db_path": str(db)}
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return {"db_exists": True, "schema_version": None, "error": str(exc), "db_path": str(db)}


def detect(home: Path | None = None) -> dict[str, Any]:
    target = plugin_dir(home)
    symlinked = target.is_symlink()
    info = _manifest_info(target)
    health = _sqlite_health(home)
    parsed = _version_tuple(info["version"])
    compatible = bool(not symlinked and info["name"] == PLUGIN_DIRNAME and parsed and parsed >= MIN_VERSION and info["health_tool"])
    return {
        "installed": target.is_dir(),
        "symlinked": symlinked,
        "path": str(target),
        "manifest": info["manifest"],
        "name": info["name"],
        "version": info["version"],
        "compatible": compatible,
        "minimum_version": ".".join(map(str, MIN_VERSION)),
        "health_tool": info["health_tool"],
        "schema_version": health.get("schema_version"),
        "sqlite": health,
        "doctor_command": f"hermes plugins doctor {target} --ci" if target.is_dir() else "",
        "authority": "coordination-data-only",
    }


def _reject_symlinked_source(root: Path) -> None:
    """Do not let an explicit plugin checkout smuggle arbitrary host files."""
    for item in root.rglob("*"):
        if item.is_symlink():
            raise ValueError(f"Shared Context source may not contain symlinks: {item}")


def install_from_source(source: str | Path, home: Path | None = None, *, replace: bool = False) -> dict[str, Any]:
    raw_src = Path(source).expanduser()
    if raw_src.is_symlink():
        raise ValueError(f"Shared Context source may not be a symlink: {raw_src}")
    src = raw_src.resolve()
    if not src.is_dir():
        raise ValueError(f"Shared Context source directory not found: {src}")
    _reject_symlinked_source(src)
    info = _manifest_info(src)
    parsed = _version_tuple(info["version"])
    if info["name"] != PLUGIN_DIRNAME or not info["manifest"] or not parsed or parsed < MIN_VERSION or not info["health_tool"]:
        raise ValueError("Shared Context source must be hermes-context-bus >=0.2.0 and declare shared_context_health")
    target = plugin_dir(home)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and not replace:
        existing = detect(home)
        if not existing["compatible"]:
            raise ValueError("An incompatible Shared Context install already exists; rerun with --replace-shared-context")
        return existing

    ignore = shutil.ignore_patterns(".git", "__pycache__", ".pytest_cache", "runtime", "state")
    stage = Path(tempfile.mkdtemp(prefix=f".{PLUGIN_DIRNAME}.stage-", dir=target.parent))
    shutil.rmtree(stage)
    backup = target.parent / f".{PLUGIN_DIRNAME}.backup-{uuid.uuid4().hex}"
    moved_existing = False
    committed = False
    try:
        shutil.copytree(src, stage, ignore=ignore)
        staged = _manifest_info(stage)
        staged_version = _version_tuple(staged["version"])
        if staged["name"] != PLUGIN_DIRNAME or not staged_version or staged_version < MIN_VERSION or not staged["health_tool"]:
            raise ValueError("Staged Shared Context copy failed compatibility validation")
        if target.exists():
            os.replace(target, backup)
            moved_existing = True
        os.replace(stage, target)
        committed = True
        if moved_existing:
            shutil.rmtree(backup, ignore_errors=True)
        return detect(home)
    except Exception:
        if moved_existing and backup.exists() and not target.exists():
            try:
                os.replace(backup, target)
            except OSError as rollback_exc:
                raise RuntimeError(f"Shared Context replacement failed and rollback is preserved at {backup}: {rollback_exc}") from rollback_exc
        raise
    finally:
        if stage.exists():
            shutil.rmtree(stage, ignore_errors=True)
        if committed and backup.exists():
            shutil.rmtree(backup, ignore_errors=True)


def reconcile_enabled(enabled: bool, *, home: Path | None = None, runner=subprocess.run) -> dict[str, Any]:
    root = home or hermes_home()
    status = detect(root)
    if enabled and not status["installed"]:
        return {**status, "desired_enabled": True, "changed": False, "warning": "HermesContextBus is not installed"}
    if enabled and not status["compatible"]:
        return {**status, "desired_enabled": True, "changed": False, "warning": f"HermesContextBus >= {status['minimum_version']} with shared_context_health is required"}
    if not status["installed"]:
        return {**status, "desired_enabled": False, "changed": False}
    executable = shutil.which("hermes") or "hermes"
    cmd = [executable, "plugins", "enable", PLUGIN_DIRNAME, "--no-allow-tool-override"] if enabled else [executable, "plugins", "disable", PLUGIN_DIRNAME]
    env = os.environ.copy()
    env["HERMES_HOME"] = str(root)
    try:
        proc = runner(cmd, capture_output=True, text=True, env=env)
    except FileNotFoundError as exc:
        raise RuntimeError("Hermes CLI is not installed or not on PATH; cannot reconcile Shared Context") from exc
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "Hermes plugin reconciliation failed").strip())
    return {**detect(root), "desired_enabled": bool(enabled), "changed": True, "command": " ".join(cmd)}


def explain(home: Path | None = None) -> str:
    st = detect(home)
    if st["installed"]:
        version = st.get("version") or "unknown"
        schema = st.get("schema_version")
        health = "health-tool" if st.get("health_tool") else "no-health-tool"
        compatibility = "compatible" if st.get("compatible") else "incompatible"
        return f"Shared Context {version} ({compatibility}) installed at {st['path']}; schema={schema}; {health}; coordination data only."
    return "Shared Context is selected by some Nerve profiles but HermesContextBus is not installed for this Hermes home."
