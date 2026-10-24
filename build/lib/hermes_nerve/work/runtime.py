from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Callable

from .supervisor import CardSupervisor

_LOCK = threading.RLock()
_supervisor: CardSupervisor | None = None
_enabled = False
_reviewer = ""
_settings: dict[str, Any] = {}
_tool_dispatcher: Callable[[str, dict[str, Any]], Any] | None = None


def set_tool_dispatcher(dispatcher: Callable[[str, dict[str, Any]], Any] | None) -> None:
    """Install Hermes' in-process tool dispatcher for hook-owned lifecycle actions."""
    global _tool_dispatcher
    with _LOCK:
        _tool_dispatcher = dispatcher


def tool_dispatcher_available() -> bool:
    """Whether the plugin captured Hermes' in-process dispatcher capability."""
    with _LOCK:
        return _tool_dispatcher is not None


def dispatch_tool(name: str, args: dict[str, Any]) -> Any:
    """Invoke a registered Hermes tool from controller/hook code.

    Hermes documents ``PluginContext.dispatch_tool`` as safe from hook callbacks.
    Dev15 treats this captured capability as *controller authority*: model/worker
    code is still fenced from lifecycle mutation, while hooks may perform the
    verified terminal transition without another model turn.
    """
    with _LOCK:
        dispatcher = _tool_dispatcher
    if dispatcher is None:
        raise RuntimeError("Hermes plugin tool dispatcher is unavailable")
    return dispatcher(str(name), dict(args or {}))


def configure(
    *,
    enabled: bool = False,
    mode: str = "shadow",
    store_path: str = "",
    preview_chars: int = 1200,
    calibration_min_samples: int = 12,
    calibration_max_brier: float = 0.24,
    enforcement_override: bool = False,
    control_confidence: float = 0.86,
    reviewer: str = "",
    auto_bind_kanban: bool = True,
    headless_workers: bool = True,
    default_task_budget_tokens: int = 70000,
    checkpoint_fractions: str = "0.40,0.70",
    supervisor_budget_fraction: float = 0.03,
    roi_min_expected_savings_tokens: int = 1500,
    provider_decision_cooldown_tokens: int = 8000,
    estimated_decision_call_tokens: int | None = None,
    estimated_jev_call_tokens: int = 800,
    provider_decisions_enabled: bool = True,
    directive_high_confidence: float = 0.90,
    directive_watch_confidence: float = 0.75,
    repeated_failure_trigger: int = 2,
    directive_max_chars: int = 320,
    completion_controller_attempts: int = 3,
    auto_estimate_task_budget: bool = True,
    budget_dod_required: bool = True,
    budget_dod_tolerance: float = 1.10,
    budget_estimator_base_tokens: int = 120000,
    budget_estimator_per_criterion_tokens: int = 75000,
    budget_estimator_body_char_factor: float = 25.0,
    budget_estimator_safety_multiplier: float = 1.25,
    budget_estimator_max_tokens: int = 2000000,
    nerve_observer_enabled: bool = True,
    nerve_auto_kill: bool = False,
    nerve_watch_fraction: float = 0.65,
    nerve_forecast_fraction: float = 0.80,
    nerve_extension_fraction: float = 0.25,
    nerve_extension_min_confidence: float = 0.60,
    nerve_no_handoff_fraction: float = 0.90,
    nerve_replan_fraction: float = 0.90,
    nerve_hard_budget_multiplier: float = 1.75,
    nerve_min_calls_before_kill: int = 12,
    nerve_repeated_failure_kill: int = 3,
    nerve_high_context_streak_kill: int = 3,
) -> None:
    global _supervisor, _enabled, _reviewer, _settings
    with _LOCK:
        _enabled = bool(enabled)
        _reviewer = str(reviewer or "").strip()
        _settings = {
            "auto_bind_kanban": bool(auto_bind_kanban),
            "headless_workers": bool(headless_workers),
            "default_task_budget_tokens": max(1000, int(default_task_budget_tokens or 70000)),
            "checkpoint_fractions": str(checkpoint_fractions or "0.40,0.70"),
            "supervisor_budget_fraction": max(0.0, min(0.25, float(supervisor_budget_fraction))),
            "roi_min_expected_savings_tokens": max(0, int(roi_min_expected_savings_tokens)),
            "provider_decision_cooldown_tokens": max(0, int(provider_decision_cooldown_tokens)),
            "estimated_decision_call_tokens": max(1, int(estimated_decision_call_tokens if estimated_decision_call_tokens is not None else estimated_jev_call_tokens)),
            "estimated_jev_call_tokens": max(1, int(estimated_decision_call_tokens if estimated_decision_call_tokens is not None else estimated_jev_call_tokens)),
            "provider_decisions_enabled": bool(provider_decisions_enabled),
            "directive_high_confidence": max(0.0, min(1.0, float(directive_high_confidence))),
            "directive_watch_confidence": max(0.0, min(1.0, float(directive_watch_confidence))),
            "repeated_failure_trigger": max(1, int(repeated_failure_trigger)),
            "directive_max_chars": max(80, min(1000, int(directive_max_chars))),
            "completion_controller_attempts": max(1, min(10, int(completion_controller_attempts or 3))),
            "auto_estimate_task_budget": bool(auto_estimate_task_budget),
            "budget_dod_required": bool(budget_dod_required),
            "budget_dod_tolerance": max(1.0, min(2.0, float(budget_dod_tolerance))),
            "budget_estimator_base_tokens": max(1000, int(budget_estimator_base_tokens)),
            "budget_estimator_per_criterion_tokens": max(0, int(budget_estimator_per_criterion_tokens)),
            "budget_estimator_body_char_factor": max(0.0, float(budget_estimator_body_char_factor)),
            "budget_estimator_safety_multiplier": max(1.0, float(budget_estimator_safety_multiplier)),
            "budget_estimator_max_tokens": max(1000, int(budget_estimator_max_tokens)),
            "nerve_observer_enabled": bool(nerve_observer_enabled),
            "nerve_auto_kill": bool(nerve_auto_kill),
            "nerve_watch_fraction": max(0.05, min(1.5, float(nerve_watch_fraction))),
            "nerve_forecast_fraction": max(0.10, min(1.5, float(nerve_forecast_fraction))),
            "nerve_extension_fraction": max(0.01, min(1.0, float(nerve_extension_fraction))),
            "nerve_extension_min_confidence": max(0.0, min(1.0, float(nerve_extension_min_confidence))),
            "nerve_no_handoff_fraction": max(0.10, min(1.5, float(nerve_no_handoff_fraction))),
            "nerve_replan_fraction": max(0.10, min(2.0, float(nerve_replan_fraction))),
            "nerve_hard_budget_multiplier": max(1.05, float(nerve_hard_budget_multiplier)),
            "nerve_min_calls_before_kill": max(1, int(nerve_min_calls_before_kill)),
            "nerve_repeated_failure_kill": max(2, int(nerve_repeated_failure_kill)),
            "nerve_high_context_streak_kill": max(2, int(nerve_high_context_streak_kill)),
        }
        if _enabled:
            _supervisor = CardSupervisor(
                store_path=Path(store_path).expanduser() if str(store_path or "").strip() else None,
                mode=mode,
                preview_chars=preview_chars,
                calibration_min_samples=calibration_min_samples,
                calibration_max_brier=calibration_max_brier,
                enforcement_override=enforcement_override,
                control_confidence=control_confidence,
            )
        else:
            # Hard-OFF means no SQLite path creation or supervision-store initialization.
            _supervisor = None


def enabled() -> bool:
    with _LOCK:
        return _enabled


def reviewer() -> str:
    with _LOCK:
        return _reviewer


def settings() -> dict[str, Any]:
    with _LOCK:
        return dict(_settings)


def supervisor() -> CardSupervisor:
    global _supervisor
    with _LOCK:
        if not _enabled:
            raise RuntimeError("work supervision is disabled")
        if _supervisor is None:
            _supervisor = CardSupervisor()
        return _supervisor


def set_supervisor_for_tests(value: CardSupervisor | None, *, enabled_value: bool = True) -> None:
    global _supervisor, _enabled
    with _LOCK:
        _supervisor = value
        _enabled = bool(enabled_value)
