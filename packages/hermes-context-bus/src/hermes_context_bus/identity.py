from __future__ import annotations

import os
from collections.abc import Mapping

from .protocol import AGENT_IDS


_PLATFORM_TO_AGENT = {
    "whatsapp": "hermes-whatsapp",
    "discord": "hermes-discord",
    "desktop": "hermes-desktop",
    "cli": "hermes-cli",
    "tui": "hermes-cli",
    "local": "hermes-cli",
}


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def resolve_agent_id(
    platform: str | None,
    *,
    environ: Mapping[str, str] | None = None,
    override: str | None = None,
) -> str | None:
    env = environ if environ is not None else os.environ
    explicit = str(override or env.get("HERMES_CONTEXT_AGENT_ID") or "").strip()
    if explicit:
        return explicit if explicit in AGENT_IDS else None

    normalized = str(platform or "").strip().lower()
    if normalized in {"cli", "tui", "local"} and (
        _truthy(env.get("HERMES_DESKTOP"))
        or _truthy(env.get("HERMES_DESKTOP_TERMINAL"))
    ):
        return "hermes-desktop"
    return _PLATFORM_TO_AGENT.get(normalized)