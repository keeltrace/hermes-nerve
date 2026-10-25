from __future__ import annotations

CANONICAL_BOARD = "hermes-shared-context"
AGENT_IDS = (
    "hermes-whatsapp",
    "hermes-discord",
    "hermes-cli",
    "hermes-desktop",
)


def require_agent_id(agent_id: str) -> str:
    value = str(agent_id).strip()
    if value not in AGENT_IDS:
        raise ValueError(f"unknown Hermes agent id: {value}")
    return value