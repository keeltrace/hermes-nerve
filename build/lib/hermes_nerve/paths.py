"""Profile-aware filesystem paths for Nerve.

Hermes named profiles are selected by setting ``HERMES_HOME`` before startup,
and newer multiplexed hosts can additionally use a context-local Hermes-home
override. Prefer Hermes' own resolver when importable; fall back to environment
and finally ``~/.hermes`` so the package remains usable in standalone tests.
"""

from __future__ import annotations

import os
from pathlib import Path


def hermes_home() -> Path:
    try:
        from hermes_constants import get_hermes_home  # type: ignore
        value = get_hermes_home()
        if value:
            return Path(value).expanduser()
    except Exception:
        pass
    explicit = str(os.getenv("HERMES_HOME") or "").strip()
    return Path(explicit).expanduser() if explicit else Path.home() / ".hermes"


def default_hermes_root() -> Path:
    try:
        from hermes_constants import get_default_hermes_root  # type: ignore
        value = get_default_hermes_root()
        if value:
            return Path(value).expanduser()
    except Exception:
        pass
    home = hermes_home()
    if home.parent.name == "profiles":
        return home.parent.parent
    return Path.home() / ".hermes"


def infer_profile_home_from_path(path: Path | None = None) -> Path | None:
    """Infer a trustworthy <root>/profiles/<name> home from *path*."""
    candidate = (path or Path.cwd()).expanduser().resolve(strict=False)
    parts = candidate.parts
    try:
        default_root = default_hermes_root().expanduser().resolve(strict=False)
    except RuntimeError:
        # Path.home() can be unavailable in hermetic/report test environments.
        # Explicit path inference must still work without process HOME metadata.
        default_root = None

    # A path may itself live inside another profile's scratch/cache tree, so
    # inspect the nearest profile candidate first. Accept a fresh profile under
    # the canonical .hermes/default root, or an alternate root only when it has
    # a concrete Nerve/profile-install marker. This keeps arbitrary
    # repo/vendor profiles/<name> directories from becoming report authority.
    for index in range(len(parts) - 2, -1, -1):
        if parts[index] != "profiles" or index + 1 >= len(parts):
            continue
        profile_home = Path(*parts[: index + 2])
        root = Path(*parts[:index]).resolve(strict=False)
        nested_under_profile = any(
            parts[parent] == "profiles" and parent + 1 < index
            for parent in range(index)
        )
        known_root = root == default_root or (root.name == ".hermes" and not nested_under_profile)
        initialized = (profile_home / "nerve" / "profile.json").is_file()
        installed_plugin = (profile_home / "plugins" / "hermes-nerve" / "plugin.yaml").is_file()
        if (known_root and profile_home.is_dir()) or initialized or installed_plugin:
            return profile_home
    return None


def report_home(path: Path | None = None) -> Path:
    """Best home for a standalone report command.

    Running a plugin script directly does not inherit the wrapper's HERMES_HOME.
    If the script is launched from inside a named profile plugin directory, use
    that profile rather than silently falling back to the global default home.
    """
    explicit = str(os.getenv("HERMES_HOME") or "").strip()
    if explicit:
        return Path(explicit).expanduser()
    inferred = infer_profile_home_from_path(path)
    return inferred if inferred is not None else hermes_home()
