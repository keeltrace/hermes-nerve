"""Read Hermes ``config.yaml`` plugin settings for Nerve.

Shared by the Control Panel and the CLI so both surfaces resolve against the
same external values the running gateway honors. Read-only; missing files and
optional dependencies degrade to empty settings plus warnings.
"""
from __future__ import annotations

from pathlib import Path

from .paths import hermes_home


def load_host_settings(home: str | Path | None = None):
    """Return ``(settings, warnings)`` from ``<home>/config.yaml``.

    Settings come from ``plugins.entries.nerve`` (``config`` merged under
    ``settings``). Never raises for missing or unparsable files; problems are
    reported as warnings so setup surfaces keep working.
    """
    base = Path(home).expanduser() if home else hermes_home()
    path = base / "config.yaml"
    if not path.is_file():
        return {}, []
    try:
        import yaml
    except ImportError:
        return {}, ["Hermes config.yaml exists but PyYAML is unavailable; external overrides cannot be inspected."]
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        entries = ((data.get("plugins") or {}).get("entries") or {})
        active = entries.get("nerve") or {}
        settings = dict(active.get("config") or {})
        settings.update(active.get("settings") or {})
        warnings = []
        if "hermes-nerve" in entries:
            warnings.append("Old plugins.entries.hermes-nerve settings are ignored by Hermes.")
        return settings, warnings
    except (OSError, ValueError, TypeError, AttributeError, yaml.YAMLError):
        return {}, ["Hermes config.yaml could not be parsed."]
