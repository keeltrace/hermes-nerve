"""Global Reflex backend configuration used by DecisionEngine default construction."""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from ..client import JevClient
from ..paths import report_home
from .laya import DEFAULT_BASE_URL as LAYA_BASE_URL, DEFAULT_MODEL as LAYA_MODEL, LayaClient
from .openjev import DEFAULT_BASE_URL as OPENJEV_BASE_URL, DEFAULT_MODEL as OPENJEV_MODEL, OpenJevClient
from .shadow import ShadowProvider
from .systemone import (
    DEFAULT_BASE_URL as SYSTEMONE_BASE_URL,
    DEFAULT_MODEL as SYSTEMONE_MODEL,
    DEFAULT_PATH as SYSTEMONE_PATH,
    D1_DEFAULT_MODEL,
    D1Client,
    SystemOneHTTPClient,
)
from . import telemetry

_LOCK = threading.RLock()
_SETTINGS: dict[str, Any] = {
    "backend": "jev",
    "laya_base_url": LAYA_BASE_URL,
    "laya_model": LAYA_MODEL,
    "laya_timeout_seconds": 5.0,
    "laya_token": "",
    "openjev_base_url": OPENJEV_BASE_URL,
    "openjev_model": OPENJEV_MODEL,
    "openjev_timeout_seconds": 10.0,
    "openjev_token": "",
    "openjev_expected_identity": "",
    "systemone_base_url": SYSTEMONE_BASE_URL,
    "systemone_path": SYSTEMONE_PATH,
    "systemone_model": SYSTEMONE_MODEL,
    "systemone_timeout_seconds": 5.0,
    "systemone_token": "",
    "systemone_provider": "SystemOne",
    "systemone_expected_identity": "",
    "d1_base_url": SYSTEMONE_BASE_URL,
    "d1_path": SYSTEMONE_PATH,
    "d1_model": D1_DEFAULT_MODEL,
    "d1_timeout_seconds": 5.0,
    "d1_token": "",
    "d1_expected_identity": "",
    "shadow_backend": "laya",
    "shadow_async": True,
    "shadow_log": "",
}

_BACKENDS = {"jev", "laya", "openjev", "systemone", "d1", "shadow"}
_SHADOW_BACKENDS = {"laya", "openjev", "systemone", "d1"}


def _timeout(value: Any, default: float) -> float:
    try:
        return min(120.0, max(0.25, float(value)))
    except (TypeError, ValueError):
        return default


def configure(
    *,
    backend: Any = "jev",
    laya_base_url: Any = LAYA_BASE_URL,
    laya_model: Any = LAYA_MODEL,
    laya_timeout_seconds: Any = 5.0,
    laya_token: Any = "",
    openjev_base_url: Any = OPENJEV_BASE_URL,
    openjev_model: Any = OPENJEV_MODEL,
    openjev_timeout_seconds: Any = 10.0,
    openjev_token: Any = "",
    openjev_expected_identity: Any = "",
    systemone_base_url: Any = SYSTEMONE_BASE_URL,
    systemone_path: Any = SYSTEMONE_PATH,
    systemone_model: Any = SYSTEMONE_MODEL,
    systemone_timeout_seconds: Any = 5.0,
    systemone_token: Any = "",
    systemone_provider: Any = "SystemOne",
    systemone_expected_identity: Any = "",
    d1_base_url: Any = SYSTEMONE_BASE_URL,
    d1_path: Any = SYSTEMONE_PATH,
    d1_model: Any = D1_DEFAULT_MODEL,
    d1_timeout_seconds: Any = 5.0,
    d1_token: Any = "",
    d1_expected_identity: Any = "",
    shadow_backend: Any = "laya",
    shadow_async: Any = True,
    shadow_log: Any = "",
) -> None:
    selected = str(backend or "jev").strip().lower()
    if selected not in _BACKENDS:
        raise ValueError("reflex backend must be one of: jev, laya, openjev, systemone, d1, shadow")
    selected_shadow = str(shadow_backend or "laya").strip().lower()
    if selected_shadow not in _SHADOW_BACKENDS:
        raise ValueError("reflex shadow backend must be one of: laya, openjev, systemone, d1")

    with _LOCK:
        _SETTINGS.update(
            {
                "backend": selected,
                "laya_base_url": str(laya_base_url or LAYA_BASE_URL).rstrip("/"),
                "laya_model": str(laya_model or LAYA_MODEL).strip() or LAYA_MODEL,
                "laya_timeout_seconds": _timeout(laya_timeout_seconds, 5.0),
                "laya_token": str(laya_token or "").strip(),
                "openjev_base_url": str(openjev_base_url or OPENJEV_BASE_URL).rstrip("/"),
                "openjev_model": str(openjev_model or OPENJEV_MODEL).strip() or OPENJEV_MODEL,
                "openjev_timeout_seconds": _timeout(openjev_timeout_seconds, 10.0),
                "openjev_token": str(openjev_token or "").strip(),
                "openjev_expected_identity": str(openjev_expected_identity or "").strip(),
                "systemone_base_url": str(systemone_base_url or SYSTEMONE_BASE_URL).rstrip("/"),
                "systemone_path": str(systemone_path or SYSTEMONE_PATH).strip() or SYSTEMONE_PATH,
                "systemone_model": str(systemone_model or "").strip(),
                "systemone_timeout_seconds": _timeout(systemone_timeout_seconds, 5.0),
                "systemone_token": str(systemone_token or "").strip(),
                "systemone_provider": str(systemone_provider or "SystemOne").strip() or "SystemOne",
                "systemone_expected_identity": str(systemone_expected_identity or "").strip(),
                "d1_base_url": str(d1_base_url or SYSTEMONE_BASE_URL).rstrip("/"),
                "d1_path": str(d1_path or SYSTEMONE_PATH).strip() or SYSTEMONE_PATH,
                "d1_model": str(d1_model or D1_DEFAULT_MODEL).strip() or D1_DEFAULT_MODEL,
                "d1_timeout_seconds": _timeout(d1_timeout_seconds, 5.0),
                "d1_token": str(d1_token or "").strip(),
                "d1_expected_identity": str(d1_expected_identity or "").strip(),
                "shadow_backend": selected_shadow,
                "shadow_async": bool(shadow_async),
                "shadow_log": str(shadow_log or "").strip(),
            }
        )


def settings() -> dict[str, Any]:
    with _LOCK:
        out = dict(_SETTINGS)
    for name in ("laya_token", "openjev_token", "systemone_token", "d1_token"):
        out[name + "_configured"] = bool(out.pop(name, ""))
    return out


def _raw_settings() -> dict[str, Any]:
    with _LOCK:
        return dict(_SETTINGS)


def _laya_client(cfg: dict[str, Any]) -> LayaClient:
    return LayaClient(
        base_url=cfg["laya_base_url"],
        model=cfg["laya_model"],
        timeout=cfg["laya_timeout_seconds"],
        token=cfg["laya_token"] or None,
    )


def _openjev_client(cfg: dict[str, Any]) -> OpenJevClient:
    return OpenJevClient(
        base_url=cfg["openjev_base_url"],
        model=cfg["openjev_model"],
        timeout=cfg["openjev_timeout_seconds"],
        token=cfg["openjev_token"] or None,
        expected_identity=cfg["openjev_expected_identity"] or None,
    )


def _systemone_client(cfg: dict[str, Any]) -> SystemOneHTTPClient:
    return SystemOneHTTPClient(
        base_url=cfg["systemone_base_url"],
        path=cfg["systemone_path"],
        model=cfg["systemone_model"],
        timeout=cfg["systemone_timeout_seconds"],
        token=cfg["systemone_token"] or None,
        provider_name=cfg["systemone_provider"],
        expected_identity=cfg["systemone_expected_identity"] or None,
    )


def _d1_client(cfg: dict[str, Any]) -> D1Client:
    return D1Client(
        base_url=cfg["d1_base_url"],
        path=cfg["d1_path"],
        model=cfg["d1_model"],
        timeout=cfg["d1_timeout_seconds"],
        token=cfg["d1_token"] or None,
        expected_identity=cfg["d1_expected_identity"] or None,
    )


def _open_source_client(name: str, cfg: dict[str, Any]):
    if name == "laya":
        return _laya_client(cfg)
    if name == "openjev":
        return _openjev_client(cfg)
    if name == "systemone":
        return _systemone_client(cfg)
    if name == "d1":
        return _d1_client(cfg)
    raise ValueError(f"unsupported Reflex provider: {name}")


def get_provider():
    cfg = _raw_settings()
    backend = cfg["backend"]
    if backend == "jev":
        return JevClient()
    if backend in _SHADOW_BACKENDS:
        return _open_source_client(backend, cfg)

    shadow = _open_source_client(cfg["shadow_backend"], cfg)
    shadow_path = Path(cfg["shadow_log"]).expanduser() if cfg["shadow_log"] else report_home() / "reflex" / "shadow.jsonl"
    return ShadowProvider(JevClient(), shadow, path=shadow_path, asynchronous=cfg["shadow_async"])


def report(*, recent_limit: int = 5) -> dict[str, Any]:
    cfg = settings()
    path = Path(str(cfg.get("shadow_log") or "")).expanduser() if cfg.get("shadow_log") else report_home() / "reflex" / "shadow.jsonl"
    return {"settings": cfg, "shadow": telemetry.report(path, recent_limit=recent_limit)}
