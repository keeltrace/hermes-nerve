"""Resolve Nerve personalities without initializing providers or runtimes."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from .modules import MODULES
from .profiles import PROFILES, load_profile, normalize_profile_name, validate_profile


@dataclass
class ResolvedNerveConfig:
    profile: str
    modules: dict[str, bool]
    advanced: dict[str, Any]
    reasons: dict[str, str]
    _adapter: Callable = field(repr=False)
    ignored_settings_profile: str = ""
    override_sources: dict[str, str] = field(default_factory=dict)
    module_conflicts: list[dict[str, object]] = field(default_factory=list)

    def enabled(self, module: str) -> bool:
        return bool(self.modules.get(module, False))

    def get_config(self, key: str, default=None):
        return self.advanced[key] if key in self.advanced else self._adapter(key, default)


def _parse_module_overrides(value: Any) -> dict[str, bool]:
    if value in (None, "", {}):
        return {}
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError("nerve_modules must be valid JSON or a mapping") from exc
    if not isinstance(value, Mapping):
        raise ValueError("nerve_modules must be a mapping")
    unknown = [str(k) for k in value if str(k) not in MODULES]
    if unknown:
        raise ValueError("Unknown Nerve module override(s): " + ", ".join(sorted(unknown)))
    invalid = [str(k) for k, v in value.items() if type(v) is not bool]
    if invalid:
        raise ValueError("Nerve module overrides must be booleans: " + ", ".join(sorted(invalid)))
    return {str(k): v for k, v in value.items()}


def resolve_config(get_config: Callable | None = None, *, home=None, profile: dict | None = None) -> ResolvedNerveConfig:
    """Resolve defaults -> selected profile -> same-source overrides -> advanced settings.

    The Nerve sidecar document is the store both supported writers maintain
    (``nerve setup`` and the Control Panel), so when it names a profile it
    selects the effective profile and its ``nerve_modules`` are honored.
    Hermes ``config.yaml`` ``nerve_profile`` applies only when no sidecar
    profile exists. If it names a different profile while the sidecar is
    present, the sidecar wins and the ignored setting is reported through
    ``ignored_settings_profile`` so UIs and the CLI can surface the conflict
    instead of silently diverging from what the operator saved.

    No sidecar and no explicit Hermes ``nerve_profile`` means exact Legacy mode.
    """
    getter = get_config or (lambda key, default=None: default)
    document = load_profile(home) if profile is None else validate_profile(profile)

    configured_profile = normalize_profile_name(getter("nerve_profile", ""))
    configured_modules = _parse_module_overrides(getter("nerve_modules", None))
    sidecar_name = document["nerve_profile"] if document else ""

    ignored_settings_profile = ""
    if sidecar_name:
        name = sidecar_name
        use_sidecar_values = True
        if configured_profile and configured_profile != sidecar_name:
            ignored_settings_profile = configured_profile
    elif configured_profile:
        if configured_profile not in PROFILES:
            raise ValueError(f"Unknown Nerve profile: {configured_profile}")
        name = configured_profile
        use_sidecar_values = False
    else:
        name = sidecar_name or "legacy"  # Unmarked existing homes stay on Legacy; setup initializes new homes to Operator.
        use_sidecar_values = bool(document)

    if name not in PROFILES:
        raise ValueError(f"Unknown Nerve profile: {name}")

    advanced = dict(document.get("advanced", {})) if use_sidecar_values and document else {}

    def get(key, default=None):
        return advanced[key] if key in advanced else getter(key, default)

    if name in ("legacy", "custom"):
        modules = {
            "reflex": True,
            "nervous": bool(get("nervous_enabled", True)),
            "work_supervision": bool(get("work_supervision_enabled", True)),
            "token_trajectory": bool(get("work_nerve_observer_enabled", True)),
            "action_gate": get("gate_mode", "off") != "off",
            "context_governor": bool(get("context_ledger_enabled", True) or get("context_engine_register", True) or get("context_curation_mode", "shadow") != "off"),
            "remote_workers": True,
            "shared_context": False,
            "assistant_loops": False,
            "assistant_audit": False,
            "shadow_testing": get("reflex_backend", "jev") == "shadow",
            "receipts": True,
            "local_learning": bool(get("nervous_local_learning", True)),
            "verified_experience": name == "custom",
            "selective_context": name == "custom",
            "selective_context_memory": name == "custom",
            "selective_context_tools": name == "custom",
            "selective_context_skills": name == "custom",
        }
    else:
        modules = dict(PROFILES[name])
        if name == "fat_cat":
            modules["remote_workers"] = bool(get("remote_hosts", {}))

    reasons = {key: ("existing configuration" if name in ("legacy", "custom") else name + " preset") for key in MODULES}
    if name == "fat_cat" and not modules["remote_workers"]:
        reasons["remote_workers"] = "no remote_hosts configured"

    # Module-override precedence keeps the 0.4.13 contract: Hermes config.yaml
    # overrides apply over saved sidecar module values. The disagreement is no
    # longer invisible — both sources and the winner are recorded per module so
    # CLI and panel can expose exactly which authority set the effective state.
    merged_overrides: dict[str, bool] = {}
    override_sources: dict[str, str] = {}
    module_conflicts: list[dict[str, object]] = []
    if use_sidecar_values and document:
        for key, value in _parse_module_overrides(document.get("nerve_modules", {})).items():
            merged_overrides[key] = value
            override_sources[key] = "sidecar"
    for key, value in configured_modules.items():
        if key in merged_overrides and merged_overrides[key] != value:
            module_conflicts.append({
                "module": key, "sidecar": merged_overrides[key],
                "config": value, "winner": "config",
            })
        merged_overrides[key] = value
        override_sources[key] = "config"
    for key, value in merged_overrides.items():
        modules[key] = value
        reasons[key] = "explicit override"

    if name != "legacy":
        # Advanced/legacy-compatible enable flags remain valid after a profile is
        # selected. Reflect them in the resolved capability graph so policy,
        # reasons, dependency pruning, and runtime activation cannot disagree.
        runtime_enable_flags = {
            "nervous": ("nervous_enabled", True),
            "work_supervision": ("work_supervision_enabled", True),
            "token_trajectory": ("work_nerve_observer_enabled", True),
            "local_learning": ("nervous_local_learning", True),
        }
        for module, (setting, default) in runtime_enable_flags.items():
            if modules.get(module, False) and not bool(get(setting, default)):
                modules[module] = False
                reasons[module] = f"{setting}=false"

        changed = True
        while changed:
            changed = False
            for key, spec in MODULES.items():
                missing = [dep for dep in spec.dependencies if not modules.get(dep, False)]
                if modules.get(key, False) and missing:
                    modules[key] = False
                    reasons[key] = "requires " + ", ".join(missing)
                    changed = True

    return ResolvedNerveConfig(name, modules, advanced, reasons, getter, ignored_settings_profile, override_sources, module_conflicts)


resolve_nerve_config = resolve_config
