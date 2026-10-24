"""Local Nerve setup backend. Uses the existing validated profile resolver."""
from __future__ import annotations

import base64
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import sysconfig
import threading
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import build_opener, HTTPRedirectHandler, ProxyHandler, Request

from .config_resolver import resolve_config
from .host_settings import load_host_settings
from .modules import MODULES
from .paths import hermes_home
from .profiles import (
    PROFILE_NAMES, PROFILES, ProfileStateError, load_profile, normalize_profile_name,
    profile_path, restore_profile_state, save_profile, snapshot_profile_state,
    validate_profile,
)
from .provenance import VERSION, runtime_identity
from .privacy import _SECRET_KEYS, _SECRET_SUFFIXES, redact as redact_value

BACKENDS = ("jev", "laya", "openjev", "systemone", "d1", "shadow")

# Marks when this Python process imported the panel: in-process (served by the
# Hermes plugin host) it approximates when the running Hermes process loaded
# Nerve, which is what "pending restart" is measured against.
_PANEL_PROCESS_START = time.time()

# Server-driven profile emphasis (Phase 3): the UI renders these verbatim and
# keeps no per-profile rules of its own.
_PROFILE_EMPHASIS = {
    "operator": "Interactive safety and context: action gate, context governor, receipts, learning that earns its keep.",
    "lean": "Work supervision with token efficiency and low overhead; nothing runs without a reason.",
    "fat_cat": "Every supervisory subsystem available; remote workers and Shared Context additionally require setup.",
    "marie_kondo": "Minimum Nerve: completion verification, receipts and supervision that clearly earn their cost.",
    "custom": "Full Configuration: every module in the registry, your choice.",
    "legacy": "Compatibility path for pre-profile homes; explicit migration to a named profile is offered, never automatic.",
}
# Modules that exist in the registry but only become usable after extra setup;
# shown as "available with setup" instead of silently ON.
_PROFILE_AVAILABLE_WITH_SETUP = {
    "operator": ("remote_workers", "shared_context"),
    "lean": ("remote_workers", "shared_context"),
    "fat_cat": ("remote_workers", "shared_context"),
    "marie_kondo": ("remote_workers", "shared_context"),
    "custom": ("remote_workers", "shared_context"),
    "legacy": (),
}


class PanelError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def digest(payload):
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def safe_key(key):
    value = str(key).lower().replace("-", "_")
    return value not in _SECRET_KEYS and not value.endswith(_SECRET_SUFFIXES)


def _redact_nested(value):
    if isinstance(value, dict):
        return {key: _redact_nested(child) for key, child in value.items() if safe_key(key)}
    if isinstance(value, list):
        return [_redact_nested(child) for child in value]
    return redact_value(value)


def public_advanced(values):
    return {key: _redact_nested(value) for key, value in values.items() if safe_key(key)}


def owner_only_write(path, data):
    import tempfile
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".nerve-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class ControlPanel:
    """Preview, configure and restore one Hermes home."""

    def __init__(self, home=None, source=None):
        self.home = (Path(home) if home is not None else hermes_home()).expanduser()
        self.source = (Path(source) if source is not None else Path(__file__).resolve().parents[1]).resolve()
        self.lock = threading.RLock()

    def host_settings(self):
        return load_host_settings(self.home)

    def read(self):
        settings, warnings = self.host_settings()
        try:
            doc = load_profile(self.home)
        except (ProfileStateError, OSError):
            raise PanelError("Saved profile is invalid. Repair it with the Nerve CLI before applying changes.", 409)
        try:
            cfg = resolve_config(
                get_config=lambda key, default=None: settings.get(key, default),
                home=self.home, profile=doc,
            )
        except ValueError as exc:
            raise PanelError("Hermes profile settings cannot be resolved: " + str(exc), 409) from exc
        return settings, warnings, doc, cfg

    def catalog(self):
        from .cli import _advanced_catalog
        try:
            return {key: value for key, value in _advanced_catalog().items() if safe_key(key)}
        except Exception:
            return {}

    def installed_path(self):
        # Hermes identifies the plugin by manifest id "nerve"; on-disk folder
        # names may be "nerve" or the older "hermes-nerve". Prefer the copy
        # serving this UI, otherwise the canonical id directory.
        plugins = self.home / "plugins"
        for directory in (plugins / "nerve", plugins / "hermes-nerve"):
            if directory == self.source and (directory / "plugin.yaml").is_file():
                return directory
        for directory in (plugins / "nerve", plugins / "hermes-nerve"):
            if (directory / "plugin.yaml").is_file():
                return directory
        return plugins / "nerve"

    def installation(self):
        path = self.installed_path()
        return {
            "detected": (path / "plugin.yaml").is_file(),
            "path": str(path), "hermes_found": bool(shutil.which("hermes")),
            "active": "unknown (requires Hermes runtime introspection)",
        }

    def snapshots(self):
        root = self.home / "nerve" / "control-panel-backups"
        if not root.is_dir():
            return []
        return [{"id": file.stem} for file in sorted(root.glob("snapshot-*.json"), reverse=True)[:12]
                if file.is_file() and not file.is_symlink()]

    def status(self):
        settings, warnings, doc, cfg = self.read()
        fresh = doc is None and not settings.get("nerve_profile")
        return {
            "version": VERSION,
            "runtime_identity": runtime_identity(),
            "home": str(self.home),
            "fresh": fresh,
            "suggested_profile": "operator" if fresh else cfg.profile,
            "current": {
                "profile": cfg.profile, "modules": cfg.modules, "reasons": cfg.reasons,
                "saved_profile": doc.get("nerve_profile") if doc else None,
                "overrides": doc.get("nerve_modules", {}) if doc else {},
                "advanced": public_advanced(doc.get("advanced", {})) if doc else {},
                "backend": cfg.get_config("reflex_backend", "jev"),
            },
            "conflict": (
                {"ignored_settings_profile": cfg.ignored_settings_profile,
                 "message": ("Hermes config.yaml selects the '"
                             + cfg.ignored_settings_profile + "' Nerve profile, but the saved Nerve profile is '"
                             + cfg.profile + "'. The saved profile wins; update or clear "
                             "plugins.entries.nerve.config.nerve_profile in Hermes config.yaml to remove this notice.")}
                if getattr(cfg, "ignored_settings_profile", "") else None
            ),
            "profiles": [
                {"id": "operator", "name": "Operator", "description": "Recommended. Interactive, context-aware."},
                {"id": "lean", "name": "Lean", "description": "Task supervision with lower overhead."},
                {"id": "fat_cat", "name": "Fat Cat", "description": "Every supervisory subsystem available."},
                {"id": "marie_kondo", "name": "Marie Kondo", "description": "Essential supervision and receipts."},
                {"id": "custom", "name": "Full Configuration", "description": "Choose your own modules."},
                {"id": "legacy", "name": "Legacy", "description": "Preserve old behavior."},
            ],
            "modules": [
                {"id": m.id, "name": m.name, "description": m.description, "dependencies": list(m.dependencies)}
                for m in MODULES.values()
            ],
            "catalog": self.catalog(), "backend_options": list(BACKENDS),
            "installation": self.installation(), "warnings": warnings,
            "external_profile_override": str(settings.get("nerve_profile") or ""),
            "backups": self.snapshots(),
            "views": self.profile_views(doc, cfg),
            "states": self.config_states(settings, doc, cfg),
        }

    def profile_views(self, doc, cfg):
        """Server-computed per-profile feature views (Phase 3). The UI renders
        these values directly and keeps no hardcoded per-profile module rules."""
        names = {p["id"]: p["name"] for p in (
            {"id": "operator", "name": "Operator"},
            {"id": "lean", "name": "Lean"},
            {"id": "fat_cat", "name": "Fat Cat"},
            {"id": "marie_kondo", "name": "Marie Kondo"},
            {"id": "custom", "name": "Full Configuration"},
            {"id": "legacy", "name": "Legacy"},
        )}
        views = {}
        for name in PROFILE_NAMES:
            if name == "custom":
                # Full Configuration always mirrors the live module state:
                # switching into custom from another profile must show the
                # full current selection, never an empty primary list.
                primary = sorted(k for k, on in cfg.modules.items() if on)
            elif name == "legacy":
                primary = sorted(k for k, on in cfg.modules.items() if on) \
                    if cfg.profile == "legacy" else []
            else:
                defaults = PROFILES.get(name, {})
                primary = sorted(k for k, on in defaults.items() if on)
            setup = [m for m in _PROFILE_AVAILABLE_WITH_SETUP.get(name, ())
                     if m not in primary]
            views[name] = {
                "id": name, "name": names.get(name, name),
                "emphasis": _PROFILE_EMPHASIS.get(name, ""),
                "primary_modules": primary,
                "available_with_setup": setup,
                "is_current_profile": cfg.profile == name,
            }
        return views

    def config_states(self, settings, doc, cfg):
        """Configured / Effective / Running / Pending-restart (Phase 5).
        Running state is only claimed when this process is the live Hermes
        plugin host; otherwise verification is explicitly unavailable."""
        host_modules = ("hermes_cli", "hermes_agent")
        in_host = any(m in sys.modules for m in host_modules)
        identity = runtime_identity()
        loaded = identity.get("loaded") or []
        running = {
            "available": bool(in_host and loaded),
            "plugin_root": identity.get("plugin_root"),
            "source_sha256": identity.get("source_tree_sha256"),
            "loaded_modules": len(loaded),
        }
        pending = None
        if running["available"]:
            sidecar = profile_path(self.home)
            try:
                saved_after_load = sidecar.stat().st_mtime > _PANEL_PROCESS_START
            except OSError:
                saved_after_load = False
            pending = bool(saved_after_load)
        else:
            running["detail"] = (
                "Running-state verification unavailable: this Control Panel "
                "process is not the active Hermes plugin host. Query the panel "
                "served inside Hermes for live loaded-code state.")
        return {
            "configured": {
                "profile": doc.get("nerve_profile") if doc else None,
                "overrides": {k: v for k, v in (doc.get("nerve_modules", {}) if doc else {}).items()},
                "advanced": public_advanced(doc.get("advanced", {})) if doc else {},
                "has_saved_sidecar": doc is not None,
                "hermes_pinned_profile": normalize_profile_name(settings.get("nerve_profile")) or None,
                "path": str(profile_path(self.home)),
            },
            "effective": {
                "profile": cfg.profile,
                "modules": dict(cfg.modules),
                "reasons": dict(cfg.reasons),
            },
            "running": running,
            "pending_restart": pending,
        }

    def validate_advanced(self, proposed):
        if not isinstance(proposed, dict):
            raise PanelError("Advanced settings must be an object.")
        catalog = self.catalog()
        result = {}
        for key, value in proposed.items():
            if key not in catalog:
                raise PanelError("Unknown or protected setting: " + str(key))
            typ = catalog[key]["type"]
            if typ == "str" and not (isinstance(value, str) and len(value) <= 1024):
                raise PanelError(key + " expects text.")
            if typ == "bool" and type(value) is not bool:
                raise PanelError(key + " expects true or false.")
            if typ == "int" and type(value) is not int:
                raise PanelError(key + " expects an integer.")
            if typ == "float" and (type(value) not in (int, float) or not math.isfinite(value)):
                raise PanelError(key + " expects a number.")
            if typ == "list" and (not isinstance(value, list) or len(value) > 128):
                raise PanelError(key + " expects a JSON list.")
            if typ == "dict" and (not isinstance(value, dict) or len(value) > 128):
                raise PanelError(key + " expects a JSON object.")
            if typ not in ("str", "bool", "int", "float", "list", "dict"):
                raise PanelError("Unsupported setting type: " + typ)
            result[key] = value
        return result

    def candidate(self, request, existing):
        if not isinstance(request, dict):
            raise PanelError("Expected a JSON object.")
        name = normalize_profile_name(request.get("profile"))
        if name not in PROFILE_NAMES:
            raise PanelError("Choose a valid profile.")
        reset = request.get("reset", False)
        if type(reset) is not bool:
            raise PanelError("Reset must be a boolean.")
        same = bool(existing and existing["nerve_profile"] == name and not reset)
        overrides = dict(existing.get("nerve_modules", {})) if same else {}
        advanced = dict(existing.get("advanced", {})) if same else {}
        changed = request.get("overrides", {})
        if not isinstance(changed, dict):
            raise PanelError("Feature switches must be an object.")
        overrides.update(changed)
        cleared = request.get("clear_advanced", [])
        if not isinstance(cleared, list) or any(not isinstance(key, str) or key not in self.catalog() for key in cleared):
            raise PanelError("Invalid advanced settings to clear.")
        for key in cleared:
            advanced.pop(key, None)
        advanced.update(self.validate_advanced(request.get("advanced", {})))
        try:
            return validate_profile({
                "version": 1, "nerve_profile": name,
                "nerve_modules": overrides, "advanced": advanced,
            })
        except (ValueError, TypeError) as exc:
            raise PanelError("Invalid profile configuration: " + str(exc))

    def preview(self, request):
        settings, warnings, old, current = self.read()
        if any("unavailable" in msg or "could not be parsed" in msg for msg in warnings):
            raise PanelError("Cannot inspect existing Hermes settings. Refusing to overwrite an unknown configuration.", 409)
        doc = self.candidate(request, old)
        external = normalize_profile_name(settings.get("nerve_profile"))
        conflict_note = ""
        if external and external != doc["nerve_profile"]:
            # The saved Nerve profile is authoritative at runtime (sidecar wins);
            # proceed, but tell the operator config.yaml carries a stale pin.
            conflict_note = ("Note: Hermes config.yaml selects the '" + external + "' Nerve profile; "
                             "this saved profile takes precedence at runtime. Clear "
                             "plugins.entries.nerve.config.nerve_profile to remove this notice.")
        future = resolve_config(
            get_config=lambda key, default=None: settings.get(key, default),
            home=self.home, profile=doc,
        )
        if future.profile != doc["nerve_profile"]:
            raise PanelError("Hermes settings override the selected profile.", 409)
        before_bytes = snapshot_profile_state(self.home)
        version = digest({
            "primary": base64.b64encode(before_bytes["primary"] or b"").decode(),
            "backup": base64.b64encode(before_bytes["backup"] or b"").decode(),
            "settings": settings, "doc": doc,
        })
        return {
            "preview_id": version, "profile": future.profile, "modules": future.modules,
            "reasons": dict(future.reasons),
            "states": self.config_states(settings, old, current),
            "module_changes": [
                {"id": k, "name": spec.name, "before": current.enabled(k),
                 "after": future.enabled(k), "reason": future.reasons[k]}
                for k, spec in MODULES.items() if current.enabled(k) != future.enabled(k)
            ],
            "pruned": [
                {"id": k, "reason": reason} for k, reason in future.reasons.items()
                if reason.startswith("requires ") and doc["nerve_modules"].get(k, True)
            ],
            "backend": future.get_config("reflex_backend", "jev"),
            "old_backend": current.get_config("reflex_backend", "jev"),
            "conflict_note": conflict_note,
            "save_path": str(profile_path(self.home)),
            "warnings": warnings + [
                "Changes take effect after Hermes restarts or reloads this plugin.",
                "This setting does not change your main Hermes model.",
            ],
        }

    def backups_dir(self):
        return self.home / "nerve" / "control-panel-backups"

    def _save_backup(self, snapshot):
        ident = "snapshot-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + "-" + secrets.token_hex(4)
        raw = {
            "format": 1, "id": ident,
            "primary": base64.b64encode(snapshot["primary"]).decode() if snapshot["primary"] is not None else None,
            "backup": base64.b64encode(snapshot["backup"]).decode() if snapshot["backup"] is not None else None,
        }
        owner_only_write(self.backups_dir() / (ident + ".json"), json.dumps(raw).encode())
        return ident

    def apply(self, request):
        with self.lock:
            if not isinstance(request, dict) or not isinstance(request.get("preview_id"), str):
                raise PanelError("Preview changes before applying.", 409)
            newest = self.preview(request)
            if not secrets.compare_digest(newest["preview_id"], request["preview_id"]):
                raise PanelError("Settings changed since preview. Review again before saving.", 409)
            _, _, old, _ = self.read()
            doc = self.candidate(request, old)
            prior = snapshot_profile_state(self.home)
            ident = self._save_backup(prior)
            try:
                saved = save_profile(doc, self.home)
            except Exception:
                restore_profile_state(prior, self.home)
                raise
            return {
                "ok": True, "profile": doc["nerve_profile"], "saved_to": str(saved),
                "rollback_id": ident, "restart_required": True,
                "message": "Settings saved. Restart Hermes to activate changes.",
            }

    def rollback(self, request):
        with self.lock:
            ident = request.get("id") if isinstance(request, dict) else None
            if not isinstance(ident, str) or not re.fullmatch(r"snapshot-[0-9TZ]+-[0-9a-f]{8}", ident):
                raise PanelError("Select a valid saved backup.")
            path = self.backups_dir() / (ident + ".json")
            if path.is_symlink():
                raise PanelError("Invalid backup.")
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if data["format"] != 1 or data["id"] != ident:
                    raise ValueError("Bad backup format")
                restored = {
                    key: base64.b64decode(data[key], validate=True) if data[key] is not None else None
                    for key in ("primary", "backup")
                }
                for blob in restored.values():
                    if blob is not None:
                        validate_profile(json.loads(blob))
            except (FileNotFoundError, ValueError, TypeError, KeyError, OSError):
                raise PanelError("Backup cannot be validated.", 409)
            inverse = self._save_backup(snapshot_profile_state(self.home))
            restore_profile_state(restored, self.home)
            return {
                "ok": True, "recovery_id": inverse, "restart_required": True,
                "message": "Previous settings restored. Restart Hermes to activate.",
            }

    @staticmethod
    def _loopback_url(value):
        """Accept only explicit loopback URLs; no userinfo, paths, proxy or redirects."""
        if not isinstance(value, str) or len(value) > 256:
            raise PanelError("Invalid local sidecar address.")
        try:
            parsed = urlsplit(value)
            port = parsed.port
        except ValueError as exc:
            raise PanelError("Malformed local sidecar URL.") from exc
        if (parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "localhost", "::1")
                or parsed.username or parsed.password or parsed.query or parsed.fragment
                or parsed.path not in ("", "/") or port is None or not 1 <= port <= 65535):
            raise PanelError("Only HTTP loopback addresses with an explicit port are allowed.")
        # Canonical base spelling prevents arbitrary URL suffixes/userinfo.
        hostname = "[::1]" if parsed.hostname == "::1" else parsed.hostname
        return "http://" + hostname + ":" + str(port)

    @staticmethod
    def _local_opener():
        class NoRedirect(HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None
        return build_opener(ProxyHandler({}), NoRedirect())

    @staticmethod
    def _probe_question():
        """Single, harmless choice question with a machine-checkable response."""
        return {
            "setup_probe": {
                "type": "choice",
                "instructions": "Route this customer request to the appropriate department.",
                "criteria": {
                    "billing": "Invoices, payments, and refund requests.",
                    "technical": "Software crashes, outages, and errors.",
                },
            },
        }

    @staticmethod
    def _assert_proven_answer(answers):
        answer = answers.get("setup_probe") if isinstance(answers, dict) else None
        if not isinstance(answer, dict) or answer.get("choice") not in {"billing", "technical"}:
            raise ValueError("The server did not return a valid typed choice for the probe.")
        confidence = answer.get("confidence")
        if confidence is not None:
            if type(confidence) not in (float, int) or not math.isfinite(confidence) or not 0 <= confidence <= 1:
                raise ValueError("The decision confidence is invalid.")
        probabilities = answer.get("probabilities")
        if probabilities is not None:
            if (not isinstance(probabilities, dict)
                    or not all(k in {"billing", "technical"} and type(v) in (float, int)
                               and math.isfinite(v) and 0 <= v <= 1 for k, v in probabilities.items())):
                raise ValueError("The returned probabilities are invalid.")
        return {"choice": answer["choice"], "confidence": confidence}

    def _backend_probe(self, backend, *, provider="openrouter", base_url=None, mode="health"):
        settings, _, saved, resolved = self.read()
        def get(key, default=None):
            return resolved.get_config(key, default)
        if backend == "jev":
            if provider not in ("openrouter", "typesafe", "opencode"):
                raise PanelError("Unknown Jev provider.")
            if mode == "health":
                env_name = {"openrouter": "OPENROUTER_API_KEY", "typesafe": "TYPESAFE_API_KEY",
                            "opencode": "OPENCODE_API_KEY"}[provider]
                return {
                    "ok": False, "verified": False, "status": "not_tested",
                    "backend": backend, "provider": provider,
                    "credential_environment_present": bool(os.getenv(env_name)),
                    "message": "No model request sent. Select Run real decision test to test hosted Jev. The provider may charge.",
                }
            from .client import JevClient, JevError
            opts = {"openrouter": ("jev_model", "typesafe/jev-1.13"),
                    "typesafe": ("typesafe_model", "jev-latest"),
                    "opencode": ("opencode_model", "jev-1.13")}
            key, default_model = opts[provider]
            # No secrets leave the server. Let JevClient use Hermes' secret scope,
            # falling back to the process environment outside Hermes.
            try:
                client = JevClient(provider=provider, model=get(key, default_model), timeout=20.0)
                response = client.system_one(
                    state="I was charged twice this month. Please refund the second charge.",
                    questions=self._probe_question(),
                )
                outcome = self._assert_proven_answer(response.answers)
            except (JevError, ValueError, TypeError, OSError) as exc:
                # Upstream HTTP bodies may contain user/proxy details. Never
                # return raw provider exceptions to browser-side diagnostics.
                reason = str(exc)
                if "not configured" in reason:
                    detail = "Provider credential is not available in this Hermes environment."
                elif "HTTP " in reason:
                    code = re.search(r"HTTP ([0-9]{3})", reason)
                    detail = "Provider returned HTTP " + code.group(1) if code else "Provider returned an HTTP error."
                elif "timed out" in reason.lower() or "connection" in reason.lower():
                    detail = "Provider could not be reached within the test timeout."
                else:
                    detail = "The request failed or did not return a valid typed answer."
                return {
                    "ok": False, "verified": False, "backend": backend, "provider": provider,
                    "status": "inference_failed",
                    "message": "Hosted Jev inference did not pass. " + detail,
                }
            return {
                "ok": True, "verified": True, "status": "inference_passed", "backend": backend,
                "provider": provider, "response_model": response.model, "result": outcome,
                "latency_ms": round(response.latency_ms, 1), "usage": response.usage,
                "message": "Real hosted decision returned a valid typed answer. Provider usage may be billable.",
            }

        defaults = {"laya": "http://127.0.0.1:8765", "openjev": "http://127.0.0.1:3000",
                    "systemone": "http://127.0.0.1:8080", "d1": "http://127.0.0.1:8080"}
        if backend not in defaults:
            raise PanelError("Unknown local backend.")
        base = self._loopback_url(base_url or get("reflex_" + backend + "_base_url", defaults[backend]))
        opener = self._local_opener()
        if mode == "health":
            # Include the existing Laya /healthz path, but a health response
            # is NEVER counted as a successful inference.
            for endpoint in ("/healthz", "/health", "/v1/version", "/v1/models"):
                try:
                    with opener.open(Request(base + endpoint, method="GET"), timeout=3) as response:
                        raw = response.read(65536)
                        if not 200 <= response.status < 300:
                            continue
                        summary = {}
                        try:
                            body = json.loads(raw)
                            if isinstance(body, dict):
                                summary = {"reported_model": str(body.get("model", ""))[:120]}
                        except (ValueError, UnicodeError):
                            pass
                        return {
                            "ok": False, "reachable": True, "verified": False,
                            "backend": backend, "status": "reachable_only", **summary,
                            "message": backend.upper() + " HTTP service responded on " + endpoint +
                                       ". Run real decision test to verify model inference.",
                        }
                except (HTTPError, URLError, TimeoutError, OSError, ValueError):
                    continue
            return {
                "ok": False, "reachable": False, "verified": False, "backend": backend,
                "status": "unreachable",
                "message": "No supported local health endpoint responded. Start/install the sidecar, then retry.",
            }
        # Real test: use the ACTUAL System One wire contract, not /health.
        paths = {"laya": "/v1/systemone", "openjev": "/v1/systemone",
                 "systemone": get("reflex_systemone_path", "/v1/systemone"),
                 "d1": get("reflex_d1_path", "/v1/systemone")}
        path = paths[backend]
        if not isinstance(path, str) or not re.fullmatch(r"/[a-zA-Z0-9_/-]{1,79}", path):
            raise PanelError("Invalid System One endpoint path.")
        model = {
            "laya": get("reflex_laya_model", "convaiinnovations/laya-typed-decisions"),
            "openjev": get("reflex_openjev_model", "openjev"),
            "systemone": get("reflex_systemone_model", ""),
            "d1": get("reflex_d1_model", "LiquidAI/d1-3B"),
        }[backend]
        token = get("reflex_" + backend + "_token", "") or os.getenv("HERMES_REFLEX_" + backend.upper() + "_TOKEN", "")
        payload = {"state": "I was charged twice this month. Please refund the second charge.",
                   "questions": self._probe_question()}
        if model:
            payload["model"] = model
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if token:
            headers["Authorization"] = "Bearer " + str(token)
        body = json.dumps(payload, separators=(",", ":")).encode()
        began = time.monotonic()
        try:
            with opener.open(Request(base + path, headers=headers, data=body, method="POST"), timeout=60) as response:
                if not 200 <= response.status < 300:
                    raise ValueError("Server returned HTTP " + str(response.status))
                raw = response.read(262145)
                if len(raw) > 262144:
                    raise ValueError("Response exceeded the bounded probe limit.")
                answer = json.loads(raw)
            if not isinstance(answer, dict):
                raise ValueError("Server returned a non-object response.")
            outcome = self._assert_proven_answer(answer.get("answers"))
            reported_model = str(answer.get("model") or "")[:140]
            expected = str(get("reflex_" + backend + "_expected_identity", "") or "").strip()
            if expected and expected not in reported_model:
                raise ValueError("Reported model does not match the configured expected identity.")
            if not reported_model:
                raise ValueError("No model identity was reported.")
            return {
                "ok": True, "verified": True, "backend": backend,
                "status": "inference_passed", "response_model": reported_model,
                "result": outcome, "usage": answer.get("usage") if isinstance(answer.get("usage"), dict) else {},
                "latency_ms": round((time.monotonic() - began) * 1000, 1),
                "message": "Real System One inference passed: " + outcome["choice"] +
                           ". Server reported " + reported_model + ". Model identity is server-reported, not independently attested.",
            }
        except (HTTPError, URLError, TimeoutError, OSError, ValueError, TypeError) as exc:
            # Bounded generic errors: never return upstream bodies, headers or tokens.
            return {
                "ok": False, "verified": False, "backend": backend,
                "status": "inference_failed",
                "message": "Real decision probe failed (" + type(exc).__name__ + "). " +
                           ("HTTP " + str(exc.code) if isinstance(exc, HTTPError) else
                            "Check model readiness, endpoint and selected backend."),
            }

    def test_backend(self, request):
        if not isinstance(request, dict):
            raise PanelError("Invalid backend check.")
        backend = request.get("backend", "jev")
        if backend not in BACKENDS:
            raise PanelError("Unknown backend.")
        mode = request.get("mode", "health")
        if mode not in ("health", "live"):
            raise PanelError("Choose health or live test.")
        provider = request.get("provider", "openrouter")
        if backend == "shadow":
            comparator = request.get("compare_backend", "d1")
            if comparator not in ("laya", "openjev", "systemone", "d1"):
                raise PanelError("Choose a valid local comparator.")
            jev = self._backend_probe("jev", provider=provider, mode=mode)
            local = self._backend_probe(comparator, base_url=request.get("base_url"), mode=mode)
            ok = bool(jev.get("verified") and local.get("verified"))
            return {"ok": ok, "verified": ok, "backend": "shadow",
                    "status": "inference_passed" if ok else "comparison_not_verified",
                    "comparison": {"jev": jev, comparator: local},
                    "message": ("Both Jev and " + comparator + " returned typed decisions." if ok
                               else "Comparison is NOT verified: inspect both backend results."),
                    "hosted_usage_possible": mode == "live"}
        return self._backend_probe(backend, provider=provider,
                                   base_url=request.get("base_url"), mode=mode)

    def local_setup_preview(self, request):
        from .local_backend_setup import preview, LocalSetupError
        backend = request.get("backend") if isinstance(request, dict) else None
        try:
            return preview(self.home, backend)
        except LocalSetupError as exc:
            raise PanelError(str(exc), 409) from exc

    def local_setup_status(self, backend):
        from .local_backend_setup import status
        if backend not in ("laya", "d1", "openjev", "systemone"):
            raise PanelError("Unknown local backend.")
        return status(self.home, backend)

    def local_setup_install(self, request):
        from .local_backend_setup import launch, LocalSetupError
        backend = request.get("backend") if isinstance(request, dict) else None
        if backend not in ("laya", "d1"):
            raise PanelError("One-click installation is available only for Laya and D1.")
        with self.lock:
            try:
                return launch(self.home, backend, request, self.source)
            except LocalSetupError as exc:
                raise PanelError(str(exc), 409) from exc

    def local_setup_stop(self, request):
        from .local_backend_setup import stop, LocalSetupError
        if not isinstance(request, dict) or request.get("confirm_stop") is not True:
            raise PanelError("Explicit confirmation is required to stop a local model.")
        backend = request.get("backend")
        with self.lock:
            try:
                return stop(self.home, backend)
            except LocalSetupError as exc:
                raise PanelError(str(exc), 409) from exc

    def installation_sources(self):
        # Source checkout: plugin root and importable package live together.
        if (self.source / "plugin.yaml").is_file() and (self.source / "__init__.py").is_file():
            pkg = self.source / "hermes_nerve"
            if pkg.is_dir():
                return self.source, pkg
        # Installed wheel: the root Hermes registration files live in share/;
        # Python modules and bundled web assets live in site-packages.
        roots = [Path(sysconfig.get_path("data")), Path(sys.prefix), Path.home() / ".local"]
        for root in roots:
            candidate = root / "share" / "hermes-nerve"
            if (candidate / "plugin.yaml").is_file() and (candidate / "__init__.py").is_file():
                return candidate, Path(__file__).resolve().parent
        raise PanelError("Plugin registration assets were not packaged; reinstall Nerve or run the Control Panel from a source checkout.", 409)

    def install_preview(self):
        destination = self.installed_path()
        registration_source, package_source = self.installation_sources()
        if destination.is_symlink() or destination.parent.is_symlink():
            raise PanelError("Refusing to install through a symbolic link.")
        if destination.exists():
            manifest = destination / "plugin.yaml"
            if not manifest.is_file() or not re.search(r"(?m)^name:\s*nerve\s*$", manifest.read_text(encoding="utf-8")):
                raise PanelError("Destination is not a recognized Nerve installation.")
        already_installed = destination.resolve() == registration_source.resolve()
        if already_installed:
            return {
                "source": str(registration_source), "destination": str(destination),
                "already_installed": True, "replaces_existing": False,
                "initialize_operator": False, "version": VERSION,
                "message": "This is the same installed Nerve copy. No upgrade is needed.",
                "preview_id": None,
            }
        if not shutil.which("hermes"):
            raise PanelError("Hermes command is not available. Install Hermes first.", 409)
        current = self.status()
        if any("unavailable" in note or "could not be parsed" in note for note in current["warnings"]):
            raise PanelError("Cannot safely inspect the existing Hermes configuration. Resolve that first.", 409)
        new_profile = not destination.exists() and current["fresh"]
        return {
            "source": str(registration_source), "package_source": str(package_source),
            "destination": str(destination),
            "replaces_existing": destination.exists(), "preserve_backup": True,
            "initialize_operator": new_profile,
            "version": VERSION, "restart_required": True,
            "preview_id": digest({
                "source": str(registration_source), "package_source": str(package_source),
            "destination": str(destination),
                "mtime": destination.stat().st_mtime_ns if destination.exists() else None,
                "initialize_operator": new_profile,
                "version": VERSION,
            }),
        }

    def install(self, request):
        with self.lock:
            preview = self.install_preview()
            if preview.get("already_installed"):
                raise PanelError("Nerve is already installed at the source location. No copy will be made.", 409)
            if not isinstance(request, dict) or request.get("preview_id") != preview["preview_id"]:
                raise PanelError("Review installation location before installing.", 409)
            dest = self.installed_path()
            archive = dest.parent / "__nerve_backups__"
            for target in (dest, archive):
                for part in (target, *target.parents):
                    if part.is_symlink():
                        raise PanelError("Refusing an installation path containing a symlink.", 409)
            if archive.exists() and not archive.is_dir():
                raise PanelError("Installation archive is not a directory.", 409)
            host_state = {}
            for name in ("config.yaml", "users.json"):
                path = self.home / name
                if path.is_symlink() or (path.exists() and not path.is_file()):
                    raise PanelError("Refusing non-regular Hermes profile state.", 409)
                host_state[name] = ((path.read_bytes(), path.stat().st_mode & 0o7777)
                                    if path.exists() else None)
            old_profile = snapshot_profile_state(self.home)
            registration_source, package_source = self.installation_sources()
            dest.parent.mkdir(parents=True, exist_ok=True)
            archive_created = not archive.exists()
            archive.mkdir(mode=0o700, exist_ok=True)
            if (archive.stat().st_dev != dest.parent.stat().st_dev or
                    (dest.exists() and dest.stat().st_dev != archive.stat().st_dev)):
                if archive_created:
                    archive.rmdir()
                raise PanelError("Plugin and recovery archive must share a filesystem.", 409)
            stage = archive / (dest.name + ".installing-" + secrets.token_hex(4))
            recovery = archive / ("profile-before-" + secrets.token_hex(4))
            stage_created = recovery_created = native_started = False
            old = None
            switched = False
            try:
                recovery.mkdir(mode=0o700)
                recovery_created = True
                for name, state in host_state.items():
                    if state is not None:
                        owner_only_write(recovery / name, state[0])
                for name, payload in old_profile.items():
                    if payload is not None:
                        owner_only_write(recovery / ("nerve-" + name + ".json"), payload)
                owner_only_write(recovery / "snapshot.json", json.dumps({
                    "host": {name: {"present": state is not None,
                                    "mode": state[1] if state is not None else None}
                             for name, state in host_state.items()},
                    "nerve": {name: payload is not None for name, payload in old_profile.items()},
                }).encode())
                stage.mkdir(mode=0o700)
                stage_created = True
                root_names = ("plugin.yaml", "__init__.py", "CONTRIBUTORS.md", "CHANGELOG.md")
                # A source checkout can contain private state beside its code.
                # Ship Python modules and declared package assets, never an
                # unrestricted tree (e.g. .env, .git, local databases).
                runtime = set(package_source.rglob("*.py"))
                for pattern in ("web/*.html", "web/*.css", "web/*.js",
                                "university/content/*.json", "observatory/assets/*.css",
                                "observatory/assets/*.js", "research/*.md"):
                    runtime.update(package_source.glob(pattern))
                runtime = {path for path in runtime
                           if not any(part.startswith(".") or part == "__pycache__" or ".bak" in part
                                      for part in path.relative_to(package_source).parts)}
                # Resolve no selected symlink, including directory components:
                # copytree otherwise follows links into checkout-private data.
                sources = [(registration_source / name, registration_source) for name in root_names]
                sources.extend((path, package_source) for path in runtime)
                for source, root in sources:
                    current = root
                    if current.is_symlink():
                        raise RuntimeError("Runtime payload symlinks are not accepted.")
                    for part in source.relative_to(root).parts:
                        current = current / part
                        if current.is_symlink():
                            raise RuntimeError("Runtime payload symlinks are not accepted.")
                    if not source.is_file():
                        raise RuntimeError("Runtime payload source is not a regular file.")
                for name in root_names:
                    shutil.copy2(registration_source / name, stage / name)
                included = set()
                for path in runtime:
                    relative = path.relative_to(package_source)
                    included.add(relative)
                    included.update(relative.parents)

                def runtime_ignore(directory, names):
                    relative = Path(directory).relative_to(package_source)
                    return [name for name in names if relative / name not in included]

                shutil.copytree(package_source, stage / "hermes_nerve", ignore=runtime_ignore)
                hermes = shutil.which("hermes")
                if hermes is None:
                    raise RuntimeError("Hermes command is no longer available.")
                cmd = [hermes]
                env = dict(os.environ)
                if self.home.parent.name == "profiles":
                    cmd += ["-p", self.home.name]
                    env.pop("HERMES_HOME", None)
                else:
                    env["HERMES_HOME"] = str(self.home)
                # Admission must inspect the exact staged bytes while the old
                # installation is still intact. Never use enable as validation.
                native_started = True
                admission = subprocess.run(
                    cmd + ["plugins", "validate", str(stage)], env=env,
                    capture_output=True, text=True, timeout=25, check=False,
                )
                if admission.returncode:
                    raise RuntimeError("Hermes native plugin validation failed.")
                if dest.exists():
                    backup = archive / (dest.name + ".before-panel-" +
                                        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") +
                                        "-" + secrets.token_hex(3))
                    if backup.exists() or backup.is_symlink():
                        raise RuntimeError("Installation backup collision; existing backup preserved.")
                    old = backup
                    os.replace(dest, old)
                os.replace(stage, dest)
                switched = True
                result = subprocess.run(
                    cmd + ["plugins", "enable", "nerve"], env=env,
                    capture_output=True, text=True, timeout=25, check=False,
                )
                if result.returncode:
                    raise RuntimeError("Hermes plugin enable failed.")
                if preview["initialize_operator"]:
                    save_profile({"version":1, "nerve_profile":"operator",
                                  "nerve_modules":{}, "advanced":{}}, self.home)
            except Exception as exc:
                rollback_errors = []
                try:
                    if switched and dest.is_dir():
                        shutil.rmtree(dest)
                    if old is not None and old.exists():
                        os.replace(old, dest)
                    if stage_created and stage.exists():
                        shutil.rmtree(stage)
                except OSError:
                    rollback_errors.append("plugin files")
                try:
                    if native_started:
                        for name, state in host_state.items():
                            path = self.home / name
                            if path.is_symlink() or (path.exists() and not path.is_file()):
                                raise OSError("Profile state became non-regular during installation.")
                            if state is None:
                                path.unlink(missing_ok=True)
                            else:
                                owner_only_write(path, state[0])
                                os.chmod(path, state[1])
                                if path.read_bytes() != state[0] or path.stat().st_mode & 0o7777 != state[1]:
                                    raise OSError("Profile state restoration verification failed.")
                    restore_profile_state(old_profile, self.home)
                except OSError:
                    rollback_errors.append("profile settings")
                if not rollback_errors:
                    try:
                        if recovery_created:
                            shutil.rmtree(recovery)
                        if archive_created:
                            archive.rmdir()
                    except OSError:
                        rollback_errors.append("recovery artifacts")
                if rollback_errors:
                    raise PanelError("Install failed. Automatic rollback is INCOMPLETE for: " +
                                     ", ".join(rollback_errors) + ". Inspect recovery evidence at " +
                                     str(recovery) + " and the plugin backup before retrying.", 500) from exc
                raise PanelError("Install failed; Nerve plugin files and profile were restored. Verify Hermes plugin enablement before retrying: " + str(exc), 409)
            cleanup_warning = None
            try:
                shutil.rmtree(recovery)
                if archive_created and not any(archive.iterdir()):
                    archive.rmdir()
            except OSError:
                cleanup_warning = "Installation succeeded; private recovery files remain at " + str(recovery)
            return {
                "cleanup_warning": cleanup_warning,
                "ok": True, "installed_to": str(dest),
                "backup": str(old) if old else None, "restart_required": True,
                "profile_initialized": preview["initialize_operator"],
                "message": ("Nerve installed with Operator profile. " if preview["initialize_operator"] else "Nerve installed. ") + "Restart Hermes to load changes.",
            }
