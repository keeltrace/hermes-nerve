"""Small lifecycle state shared by verification and context retention policy."""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Any

_LOCK = threading.RLock()
_latest_verification: dict[str, Any] = {}


def record_verification(*, value: str, contract: str, confidence: float, probabilities: dict[str, float] | None = None) -> None:
    with _LOCK:
        _latest_verification.clear()
        _latest_verification.update(
            {
                "value": str(value or "").upper(),
                "contract": str(contract or ""),
                "confidence": float(confidence or 0.0),
                "probabilities": dict(probabilities or {}),
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        )


def latest_verification() -> dict[str, Any]:
    with _LOCK:
        return dict(_latest_verification)


def verification_passed() -> bool:
    return latest_verification().get("value") == "PASS"


def reset() -> None:
    with _LOCK:
        _latest_verification.clear()
