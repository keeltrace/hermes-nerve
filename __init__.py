"""Nerve plugin registration — profile-aware modular nervous system."""
from __future__ import annotations

import logging
import os
from pathlib import Path

try:
    from .hermes_nerve import assistant, client, context, gate, ledger, nervous, receipts, schemas, tools
    from .hermes_nerve import reflex
    from .hermes_nerve.config_resolver import resolve_config
    # Import the module, not the class: Hermes' context-engine directory loader
    # instantiates any ContextEngine subclass it finds on this package, which would
    # yield an engine that cannot read Nerve settings.
    from .hermes_nerve import context_engine as nerve_context_engine
    from .hermes_nerve.provenance import VERSION
    from .hermes_nerve.remote import control as remote_control
    from .hermes_nerve.remote import runtime as remote_runtime
    from .hermes_nerve.remote import tools as remote_tools
    from .hermes_nerve.work import hooks as work_hooks
    from .hermes_nerve.work import runtime as work_runtime
    from .hermes_nerve.work import tools as work_tools
except ImportError:
    from hermes_nerve import assistant, client, context, gate, ledger, nervous, receipts, schemas, tools
    from hermes_nerve import reflex
    from hermes_nerve.config_resolver import resolve_config
    from hermes_nerve import context_engine as nerve_context_engine
    from hermes_nerve.provenance import VERSION
    from hermes_nerve.remote import control as remote_control
    from hermes_nerve.remote import runtime as remote_runtime
    from hermes_nerve.remote import tools as remote_tools
    from hermes_nerve.work import hooks as work_hooks
    from hermes_nerve.work import runtime as work_runtime
    from hermes_nerve.work import tools as work_tools

logger=logging.getLogger("hermes_nerve")

# Earlier Nerve docs and scripts wrote this namespace. Hermes reads a plugin's settings
# only from its own entry, plugins.entries.<plugin id> (settings, then the older config
# subtree), and the plugin id is "nerve".
_LEGACY_SETTINGS_ENTRY = "hermes-nerve"


def _warn_if_legacy_settings_ignored(ctx):
    """Warn when config.yaml still holds settings Hermes never passes to Nerve."""
    plugin_id = str(getattr(ctx, "plugin_id", "") or "nerve")
    if plugin_id == _LEGACY_SETTINGS_ENTRY:
        return
    try:
        from hermes_cli.config import load_config_readonly
        entries = ((load_config_readonly() or {}).get("plugins") or {}).get("entries") or {}
    except Exception:
        return  # Outside a Hermes host there is no config.yaml to inspect.
    legacy = entries.get(_LEGACY_SETTINGS_ENTRY) if isinstance(entries, dict) else None
    if not isinstance(legacy, dict):
        return
    settings = legacy.get("settings")
    keys = sorted(str(key) for key in settings) if isinstance(settings, dict) else []
    shown = ", ".join(keys[:8]) or "none"
    logger.warning(
        "config.yaml has plugins.entries.%s, which Hermes never passes to Nerve; those settings "
        "(%s) are ignored. Move them to plugins.entries.%s.settings.",
        _LEGACY_SETTINGS_ENTRY, shown, plugin_id,
    )


def _register_legacy(ctx):
    is_kanban_worker = bool(
        str(os.getenv("HERMES_KANBAN_TASK") or os.getenv("HERMES_KANBAN_TASK_ID") or "").strip()
    )
    headless_worker = is_kanban_worker and bool(ctx.get_config("work_headless_workers", True))
    legacy_model = ctx.get_config("model_id", "typesafe/jev-1.13")
    client.configure(
        provider=ctx.get_config("jev_provider", "openrouter"),
        model=ctx.get_config("jev_model", legacy_model),
        typesafe_model=ctx.get_config("typesafe_model", "jev-latest"),
        opencode_model=ctx.get_config("opencode_model", "jev-1.13"),
        timeout=ctx.get_config("timeout_seconds", 10.0),
    )
    reflex.configure(
        backend=ctx.get_config("reflex_backend", "jev"),
        laya_base_url=ctx.get_config("reflex_laya_base_url", os.getenv("HERMES_REFLEX_LAYA_BASE_URL", "http://127.0.0.1:8765")),
        laya_model=ctx.get_config("reflex_laya_model", os.getenv("HERMES_REFLEX_LAYA_MODEL", "convaiinnovations/laya-typed-decisions")),
        laya_timeout_seconds=ctx.get_config("reflex_laya_timeout_seconds", os.getenv("HERMES_REFLEX_LAYA_TIMEOUT", 5.0)),
        **{("laya_" + "token"): ctx.get_config("reflex_laya_token", os.getenv("HERMES_REFLEX_LAYA_TOKEN", ""))},
        openjev_base_url=ctx.get_config("reflex_openjev_base_url", os.getenv("HERMES_REFLEX_OPENJEV_BASE_URL", "http://127.0.0.1:3000")),
        openjev_model=ctx.get_config("reflex_openjev_model", os.getenv("HERMES_REFLEX_OPENJEV_MODEL", "openjev")),
        openjev_timeout_seconds=ctx.get_config("reflex_openjev_timeout_seconds", os.getenv("HERMES_REFLEX_OPENJEV_TIMEOUT", 10.0)),
        **{("openjev_" + "token"): ctx.get_config("reflex_openjev_token", os.getenv("HERMES_REFLEX_OPENJEV_TOKEN", ""))},
        openjev_expected_identity=ctx.get_config("reflex_openjev_expected_identity", os.getenv("HERMES_REFLEX_OPENJEV_EXPECTED_IDENTITY", "")),
        shadow_backend=ctx.get_config("reflex_shadow_backend", "laya"),
        shadow_async=ctx.get_config("reflex_shadow_async", True),
        shadow_log=ctx.get_config("reflex_shadow_log", ""),
    )
    gate.configure(
        mode=ctx.get_config("gate_mode", "off"),
        min_confidence=ctx.get_config("min_confidence", 0.80),
        min_allow_probability=ctx.get_config("min_allow_probability", os.getenv("HERMES_NERVE_MIN_ALLOW_PROBABILITY", 0.90)),
        scope=ctx.get_config("gate_scope", "selective"),
    )
    receipts.configure(detail=ctx.get_config("receipt_detail", "hash"))
    ledger.configure(
        enabled=ctx.get_config("context_ledger_enabled", True),
        detail=ctx.get_config("context_ledger_detail", "sanitized"),
    )
    nervous.configure(
        enabled=ctx.get_config("nervous_enabled", True),
        admission_enabled=ctx.get_config("nervous_turn_admission", True),
        mode=ctx.get_config("nervous_mode", "correct_next"),
        challenge_confidence=ctx.get_config("nervous_challenge_confidence", 0.86),
        call_threshold=ctx.get_config("nervous_call_threshold", 0.58),
        max_provider_calls_per_turn=ctx.get_config("nervous_max_provider_calls_per_turn", 96),
        event_preview_chars=ctx.get_config("nervous_event_preview_chars", 1200),
        retain_recent_events=ctx.get_config("nervous_retain_recent_events", 64),
        emit_prompt_hint=ctx.get_config("nervous_emit_prompt_hint", False),
        local_learning=ctx.get_config("nervous_local_learning", True),
        local_learning_min_samples=ctx.get_config("nervous_local_learning_min_samples", 8),
        repeated_failure_local_replan_at=ctx.get_config("nervous_repeated_failure_local_replan_at", 3),
    )
    context.configure(
        preview_chars=ctx.get_config("context_preview_chars", 1200),
        anchor_chars=ctx.get_config("context_anchor_chars", 220),
        preserve_tail=ctx.get_config("context_preserve_tail", 4),
        mode=ctx.get_config("context_curation_mode", "shadow"),
        drop_max_needed=ctx.get_config("context_drop_max_needed", 0.20),
        drop_max_exact=ctx.get_config("context_drop_max_exact", 0.20),
        drop_min_superseded=ctx.get_config("context_drop_min_superseded", 0.75),
        anchor_max_needed=ctx.get_config("context_anchor_max_needed", 0.55),
        anchor_max_exact=ctx.get_config("context_anchor_max_exact", 0.45),
        conflict_pin_min=ctx.get_config("context_conflict_pin_min", 0.70),
    )
    # dev11: retain Hermes' supported in-process tool dispatcher so a passing
    # pre_verify gate can complete the owning Kanban run without another model turn.
    # Older/offline test contexts may not expose dispatch_tool; fail closed there.
    work_runtime.set_tool_dispatcher(getattr(ctx, "dispatch_tool", None))
    work_runtime.configure(
        enabled=ctx.get_config("work_supervision_enabled", True),
        mode=ctx.get_config("work_supervision_mode", "advisory"),
        store_path=ctx.get_config("work_supervision_db", ""),
        preview_chars=ctx.get_config("work_evidence_preview_chars", 1200),
        calibration_min_samples=ctx.get_config("work_calibration_min_samples", 12),
        calibration_max_brier=ctx.get_config("work_calibration_max_brier", 0.24),
        enforcement_override=ctx.get_config("work_enforcement_override", False),
        control_confidence=ctx.get_config("work_control_confidence", 0.86),
        reviewer=ctx.get_config("work_reviewer", ""),
        auto_bind_kanban=ctx.get_config("work_auto_bind_kanban", True),
        headless_workers=ctx.get_config("work_headless_workers", True),
        default_task_budget_tokens=ctx.get_config("work_default_task_budget_tokens", 70000),
        checkpoint_fractions=ctx.get_config("work_checkpoint_fractions", "0.40,0.70"),
        supervisor_budget_fraction=ctx.get_config("work_supervisor_budget_fraction", 0.03),
        roi_min_expected_savings_tokens=ctx.get_config("work_roi_min_expected_savings_tokens", 1500),
        provider_decision_cooldown_tokens=ctx.get_config("work_provider_decision_cooldown_tokens", 8000),
        estimated_decision_call_tokens=ctx.get_config("work_estimated_decision_call_tokens", ctx.get_config("work_estimated_jev_call_tokens", 800)),
        estimated_jev_call_tokens=ctx.get_config("work_estimated_jev_call_tokens", 800),
        provider_decisions_enabled=ctx.get_config("work_provider_decisions_enabled", True),
        directive_high_confidence=ctx.get_config("work_directive_high_confidence", 0.90),
        directive_watch_confidence=ctx.get_config("work_directive_watch_confidence", 0.75),
        repeated_failure_trigger=ctx.get_config("work_repeated_failure_trigger", 2),
        directive_max_chars=ctx.get_config("work_directive_max_chars", 320),
        completion_controller_attempts=ctx.get_config("work_completion_controller_attempts", 3),
        auto_estimate_task_budget=ctx.get_config("work_auto_estimate_task_budget", True),
        budget_dod_required=ctx.get_config("work_budget_dod_required", True),
        budget_dod_tolerance=ctx.get_config("work_budget_dod_tolerance", 1.10),
        budget_estimator_base_tokens=ctx.get_config("work_budget_estimator_base_tokens", 120000),
        budget_estimator_per_criterion_tokens=ctx.get_config("work_budget_estimator_per_criterion_tokens", 75000),
        budget_estimator_body_char_factor=ctx.get_config("work_budget_estimator_body_char_factor", 25.0),
        budget_estimator_safety_multiplier=ctx.get_config("work_budget_estimator_safety_multiplier", 1.25),
        budget_estimator_max_tokens=ctx.get_config("work_budget_estimator_max_tokens", 2000000),
        nerve_observer_enabled=ctx.get_config("work_nerve_observer_enabled", True),
        nerve_auto_kill=ctx.get_config("work_nerve_auto_kill", True),
        nerve_watch_fraction=ctx.get_config("work_nerve_watch_fraction", 0.65),
        nerve_replan_fraction=ctx.get_config("work_nerve_replan_fraction", 0.90),
        nerve_hard_budget_multiplier=ctx.get_config("work_nerve_hard_budget_multiplier", 1.75),
        nerve_min_calls_before_kill=ctx.get_config("work_nerve_min_calls_before_kill", 12),
        nerve_repeated_failure_kill=ctx.get_config("work_nerve_repeated_failure_kill", 3),
        nerve_high_context_streak_kill=ctx.get_config("work_nerve_high_context_streak_kill", 3),
    )
    # dev7: establish the exact Kanban run + locked DoD before the worker's
    # first provider call. This is local-only and therefore adds no LLM/token
    # overhead. Later hooks refresh the session id but do not own bootstrap.
    startup_identity = None
    if headless_worker and work_runtime.enabled():
        startup_identity = work_hooks.bootstrap_kanban_worker()

    remote_runtime.configure(
        hosts=ctx.get_config("remote_hosts", {}),
        default_host=ctx.get_config("remote_default_host", ""),
        default_max_turns=ctx.get_config("remote_default_max_turns", 100),
        default_task_timeout_seconds=ctx.get_config("remote_default_task_timeout_seconds", 3600),
        data_dir=ctx.get_config("remote_data_dir", ""),
    )

    registrations = [
        ("nerve_decide", schemas.NERVE_DECIDE, tools.nerve_decide),
        ("nerve_rank", schemas.NERVE_RANK, tools.nerve_rank),
        ("nerve_verify", schemas.NERVE_VERIFY, tools.nerve_verify),
        ("nerve_assess", schemas.NERVE_ASSESS, tools.nerve_assess),
        ("nerve_context_curate", schemas.NERVE_CONTEXT_CURATE, tools.nerve_context_curate),
        ("nerve_context_rehydrate", schemas.NERVE_CONTEXT_REHYDRATE, tools.nerve_context_rehydrate),
        ("nerve_stats", schemas.NERVE_STATS, tools.nerve_stats),
        ("nerve_nervous_event", schemas.NERVE_NERVOUS_EVENT, tools.nerve_nervous_event),
        ("nerve_supervise_card", schemas.NERVE_SUPERVISE_CARD, work_tools.nerve_supervise_card),
        ("nerve_work_event", schemas.NERVE_WORK_EVENT, work_tools.nerve_work_event),
        ("nerve_work_status", schemas.NERVE_WORK_STATUS, work_tools.nerve_work_status),
        ("nerve_remote_delegate_task", schemas.NERVE_REMOTE_DELEGATE_TASK, remote_tools.nerve_remote_delegate_task),
        ("nerve_remote_worker_status", schemas.NERVE_REMOTE_WORKER_STATUS, remote_tools.nerve_remote_worker_status),
        ("nerve_remote_worker_result", schemas.NERVE_REMOTE_WORKER_RESULT, remote_tools.nerve_remote_worker_result),
        ("nerve_remote_worker_cancel", schemas.NERVE_REMOTE_WORKER_CANCEL, remote_tools.nerve_remote_worker_cancel),
        ("nerve_remote_worker_control", schemas.NERVE_REMOTE_WORKER_CONTROL, remote_tools.nerve_remote_worker_control),
    ]
    # Controller/admin sessions retain the full Nerve surface. Ordinary Kanban
    # workers are deliberately headless: exposing these schemas was the largest
    # fixed token cost in the first A/B benchmark and encouraged the worker to
    # spend turns operating its own supervisor.
    if not headless_worker:
        for name, schema, handler in registrations:
            ctx.register_tool(name=name, toolset="nerve", schema=schema, handler=handler)

    def _pre_tool_control(**kwargs):
        callbacks = (remote_control.pre_tool_call, work_hooks.pre_tool_call)
        if not headless_worker:
            callbacks += (nervous.pre_tool_call, gate.pre_tool_call)
        for callback in callbacks:
            directive = callback(**kwargs)
            if directive is not None:
                return directive
        return None

    def _pre_llm(**kwargs):
        work_result = work_hooks.pre_llm_call(**kwargs)
        if headless_worker:
            return work_result
        nervous_result = nervous.pre_llm_call(**kwargs)
        return work_result if work_result is not None else nervous_result

    def _transform_tool_result(**kwargs):
        work_result = work_hooks.transform_tool_result(**kwargs)
        if work_result is not None:
            return work_result
        if not headless_worker:
            return nervous.transform_tool_result(**kwargs)
        return None

    def _post_llm(**kwargs):
        work_hooks.post_llm_call(**kwargs)
        if not headless_worker:
            nervous.post_llm_call(**kwargs)

    def _session_end(**kwargs):
        work_hooks.on_session_end(**kwargs)
        if not headless_worker:
            nervous.on_session_end(**kwargs)

    ctx.register_hook("pre_tool_call", _pre_tool_control)
    ctx.register_hook("post_tool_call", work_hooks.post_tool_call)
    if not headless_worker:
        ctx.register_hook("post_tool_call", ledger.observe_tool_call)
        ctx.register_hook("post_tool_call", nervous.post_tool_call)
    ctx.register_hook("pre_llm_call", _pre_llm)
    ctx.register_hook("transform_tool_result", _transform_tool_result)
    ctx.register_hook("pre_verify", work_hooks.pre_verify if headless_worker else nervous.pre_verify)
    ctx.register_hook("post_api_request", work_hooks.post_api_request)
    ctx.register_hook("api_request_error", work_hooks.api_request_error)
    ctx.register_hook("post_llm_call", _post_llm)
    ctx.register_hook("on_session_end", _session_end)

    if not headless_worker and bool(ctx.get_config("context_engine_register", True)) and hasattr(ctx, "register_context_engine"):
        ctx.register_context_engine(nerve_context_engine.NerveContextEngine(
            mode=ctx.get_config("context_engine_mode", "shadow"),
            threshold_percent=ctx.get_config("context_engine_threshold_percent", 0.72),
            protect_first_n=ctx.get_config("context_engine_protect_first_n", 3),
            protect_last_n=ctx.get_config("context_engine_protect_last_n", 6),
            shadow_trigger_percent=ctx.get_config("context_engine_shadow_trigger_percent", 0.55),
            fallback_builtin=ctx.get_config("context_engine_fallback_builtin", True),
        ))
    logger.info(
        "Nerve %s loaded from %s; tools=%d headless_worker=%s work_supervision=%s",
        VERSION, Path(__file__).resolve().parent, 0 if headless_worker else len(registrations), headless_worker,
        ctx.get_config("work_supervision_enabled", True),
    )
    if headless_worker:
        if startup_identity is not None:
            logger.info(
                "Nerve headless supervision bound at startup: task=%s run=%s contract=%s",
                startup_identity.task_id, startup_identity.run_id, startup_identity.contract_hash[:12],
            )
        else:
            message = (
                "Nerve headless supervision did not bind at startup; inspect supervision_diagnostics "
                "before treating this run as a valid Nerve-supervised measurement"
            )
            if os.getenv("HERMES_NERVE_OFFLINE_VERIFY") == "1":
                logger.info("%s (offline verifier: not applicable)", message)
            else:
                logger.warning(message)


def _register_profile(ctx):
    policy=resolve_config(ctx.get_config)
    get=policy.get_config

    def profile_default(key, default, **profile_values):
        """Apply profile runtime defaults without overriding explicit configuration."""
        marker=object()
        if key in policy.advanced and policy.advanced[key] is not None:
            return policy.advanced[key]
        configured=policy._adapter(key, marker)
        if configured is not marker and configured is not None:
            return configured
        if policy.profile in profile_values:
            return profile_values[policy.profile]
        return default

    is_kanban_worker=bool(str(os.getenv("HERMES_KANBAN_TASK") or os.getenv("HERMES_KANBAN_TASK_ID") or "").strip())
    headless_worker=is_kanban_worker and bool(get("work_headless_workers",True))

    # Provider initialization is skipped only when every provider-backed module is off.
    provider_needed=any(policy.enabled(x) for x in ("reflex","nervous","work_supervision","action_gate","context_governor","assistant_loops","assistant_audit"))
    if provider_needed:
        legacy_model=get("model_id","typesafe/jev-1.13")
        client.configure(
            provider=get("jev_provider","openrouter"),
            model=get("jev_model",legacy_model),
            typesafe_model=get("typesafe_model","jev-latest"),
            opencode_model=get("opencode_model","jev-1.13"),
            timeout=get("timeout_seconds",10.0),
        )
        backend=get("reflex_backend","jev")
        if policy.profile!="legacy" and backend=="shadow" and not policy.enabled("shadow_testing"):
            backend="jev"
        reflex.configure(
            backend=backend,
            laya_base_url=get("reflex_laya_base_url",os.getenv("HERMES_REFLEX_LAYA_BASE_URL","http://127.0.0.1:8765")),
            laya_model=get("reflex_laya_model",os.getenv("HERMES_REFLEX_LAYA_MODEL","convaiinnovations/laya-typed-decisions")),
            laya_timeout_seconds=get("reflex_laya_timeout_seconds",os.getenv("HERMES_REFLEX_LAYA_TIMEOUT",5.0)),
            **{("laya_" + "token"): get("reflex_laya_token",os.getenv("HERMES_REFLEX_LAYA_TOKEN",""))},
            openjev_base_url=get("reflex_openjev_base_url",os.getenv("HERMES_REFLEX_OPENJEV_BASE_URL","http://127.0.0.1:3000")),
            openjev_model=get("reflex_openjev_model",os.getenv("HERMES_REFLEX_OPENJEV_MODEL","openjev")),
            openjev_timeout_seconds=get("reflex_openjev_timeout_seconds",os.getenv("HERMES_REFLEX_OPENJEV_TIMEOUT",10.0)),
            **{("openjev_" + "token"): get("reflex_openjev_token",os.getenv("HERMES_REFLEX_OPENJEV_TOKEN",""))},
            openjev_expected_identity=get("reflex_openjev_expected_identity",os.getenv("HERMES_REFLEX_OPENJEV_EXPECTED_IDENTITY","")),
            shadow_backend=get("reflex_shadow_backend","laya"),
            shadow_async=get("reflex_shadow_async",True),
            shadow_log=get("reflex_shadow_log",""),
        )

    gate.configure(
        mode=profile_default("gate_mode","advisory") if policy.enabled("action_gate") and not headless_worker else "off",
        min_confidence=get("min_confidence",0.80),
        min_allow_probability=get("min_allow_probability",os.getenv("HERMES_NERVE_MIN_ALLOW_PROBABILITY",0.90)),
        scope=get("gate_scope","selective"),
    )
    receipts.configure(enabled=policy.enabled("receipts"),detail=get("receipt_detail","hash"))
    ledger.configure(
        enabled=policy.enabled("context_governor") and bool(get("context_ledger_enabled",True)),
        detail=get("context_ledger_detail","sanitized"),
    )
    nervous.configure(
        enabled=policy.enabled("nervous") and bool(get("nervous_enabled",True)),
        admission_enabled=policy.enabled("nervous") and bool(profile_default("nervous_turn_admission",True,lean=False)),
        mode=get("nervous_mode","correct_next"),
        challenge_confidence=get("nervous_challenge_confidence",0.86),
        call_threshold=get("nervous_call_threshold",0.58),
        max_provider_calls_per_turn=profile_default("nervous_max_provider_calls_per_turn",96,lean=12),
        event_preview_chars=profile_default("nervous_event_preview_chars",1200,lean=800),
        retain_recent_events=profile_default("nervous_retain_recent_events",64,lean=32),
        emit_prompt_hint=get("nervous_emit_prompt_hint",False),
        local_learning=policy.enabled("local_learning") and bool(get("nervous_local_learning",True)),
        local_learning_min_samples=get("nervous_local_learning_min_samples",8),
        repeated_failure_local_replan_at=get("nervous_repeated_failure_local_replan_at",3),
    )
    context.configure(
        preview_chars=get("context_preview_chars",1200),
        anchor_chars=get("context_anchor_chars",220),
        preserve_tail=get("context_preserve_tail",4),
        mode=get("context_curation_mode","shadow"),
        drop_max_needed=get("context_drop_max_needed",0.20),
        drop_max_exact=get("context_drop_max_exact",0.20),
        drop_min_superseded=get("context_drop_min_superseded",0.75),
        anchor_max_needed=get("context_anchor_max_needed",0.55),
        anchor_max_exact=get("context_anchor_max_exact",0.45),
        conflict_pin_min=get("context_conflict_pin_min",0.70),
    )

    work_runtime.set_tool_dispatcher(getattr(ctx,"dispatch_tool",None))
    work_runtime.configure(
        enabled=policy.enabled("work_supervision") and bool(get("work_supervision_enabled",True)),
        mode=get("work_supervision_mode","advisory"),
        store_path=get("work_supervision_db",""),
        preview_chars=get("work_evidence_preview_chars",1200),
        calibration_min_samples=get("work_calibration_min_samples",12),
        calibration_max_brier=get("work_calibration_max_brier",0.24),
        enforcement_override=get("work_enforcement_override",False),
        control_confidence=get("work_control_confidence",0.86),
        reviewer=get("work_reviewer",""),
        auto_bind_kanban=get("work_auto_bind_kanban",True),
        headless_workers=get("work_headless_workers",True),
        default_task_budget_tokens=get("work_default_task_budget_tokens",70000),
        checkpoint_fractions=get("work_checkpoint_fractions","0.40,0.70"),
        supervisor_budget_fraction=get("work_supervisor_budget_fraction",0.03),
        roi_min_expected_savings_tokens=get("work_roi_min_expected_savings_tokens",1500),
        provider_decision_cooldown_tokens=get("work_provider_decision_cooldown_tokens",8000),
        estimated_decision_call_tokens=get("work_estimated_decision_call_tokens",get("work_estimated_jev_call_tokens",800)),
        estimated_jev_call_tokens=get("work_estimated_jev_call_tokens",800),
        provider_decisions_enabled=policy.enabled("work_supervision") and bool(get("work_provider_decisions_enabled",True)),
        directive_high_confidence=get("work_directive_high_confidence",0.90),
        directive_watch_confidence=get("work_directive_watch_confidence",0.75),
        repeated_failure_trigger=get("work_repeated_failure_trigger",2),
        directive_max_chars=get("work_directive_max_chars",320),
        completion_controller_attempts=get("work_completion_controller_attempts",3),
        auto_estimate_task_budget=get("work_auto_estimate_task_budget",True),
        budget_dod_required=get("work_budget_dod_required",True),
        budget_dod_tolerance=get("work_budget_dod_tolerance",1.10),
        budget_estimator_base_tokens=get("work_budget_estimator_base_tokens",120000),
        budget_estimator_per_criterion_tokens=get("work_budget_estimator_per_criterion_tokens",75000),
        budget_estimator_body_char_factor=get("work_budget_estimator_body_char_factor",25.0),
        budget_estimator_safety_multiplier=get("work_budget_estimator_safety_multiplier",1.25),
        budget_estimator_max_tokens=get("work_budget_estimator_max_tokens",2000000),
        nerve_observer_enabled=policy.enabled("token_trajectory") and bool(get("work_nerve_observer_enabled",True)),
        nerve_auto_kill=get("work_nerve_auto_kill",False),
        nerve_watch_fraction=get("work_nerve_watch_fraction",0.65),
        nerve_forecast_fraction=get("work_nerve_forecast_fraction",0.80),
        nerve_extension_fraction=get("work_nerve_extension_fraction",0.25),
        nerve_extension_min_confidence=get("work_nerve_extension_min_confidence",0.60),
        nerve_no_handoff_fraction=get("work_nerve_no_handoff_fraction",0.90),
        nerve_replan_fraction=get("work_nerve_replan_fraction",0.90),
        nerve_hard_budget_multiplier=get("work_nerve_hard_budget_multiplier",1.75),
        nerve_min_calls_before_kill=get("work_nerve_min_calls_before_kill",12),
        nerve_repeated_failure_kill=get("work_nerve_repeated_failure_kill",3),
        nerve_high_context_streak_kill=get("work_nerve_high_context_streak_kill",3),
    )

    assistant.configure(
        loops_enabled=policy.enabled("assistant_loops"),
        audit_enabled=policy.enabled("assistant_audit"),
        min_completion_confidence=get("assistant_review_min_confidence",0.75),
        audit_min_confidence=get("assistant_audit_min_confidence",0.70),
        prompt_max_chars=get("assistant_prompt_max_chars",4000),
        provider_max_chars=get("assistant_provider_max_chars",6000),
    )

    startup_identity=None
    if headless_worker and policy.enabled("work_supervision") and work_runtime.enabled():
        startup_identity=work_hooks.bootstrap_kanban_worker()

    if policy.enabled("remote_workers"):
        remote_runtime.configure(
            hosts=get("remote_hosts",{}),
            default_host=get("remote_default_host",""),
            default_max_turns=get("remote_default_max_turns",100),
            default_task_timeout_seconds=get("remote_default_task_timeout_seconds",3600),
            data_dir=get("remote_data_dir",""),
        )
    else:
        remote_runtime.configure(hosts={},default_host="",default_max_turns=100,default_task_timeout_seconds=3600,data_dir=get("remote_data_dir",""))

    registrations=[]
    if policy.enabled("reflex"):
        # Lean keeps Reflex internally but advertises only the smallest explicit
        # decision surface. Internal nervous/work supervision calls DecisionEngine
        # directly, so rank/verify/assess schemas are pure prompt overhead here.
        registrations.append(("nerve_decide",schemas.NERVE_DECIDE,tools.nerve_decide))
        if policy.profile!="lean":
            registrations += [
                ("nerve_rank",schemas.NERVE_RANK,tools.nerve_rank),
                ("nerve_verify",schemas.NERVE_VERIFY,tools.nerve_verify),
                ("nerve_assess",schemas.NERVE_ASSESS,tools.nerve_assess),
            ]
    if policy.enabled("context_governor"):
        registrations += [
            ("nerve_context_curate",schemas.NERVE_CONTEXT_CURATE,tools.nerve_context_curate),
            ("nerve_context_rehydrate",schemas.NERVE_CONTEXT_REHYDRATE,tools.nerve_context_rehydrate),
        ]
    if policy.enabled("receipts"):
        registrations.append(("nerve_stats",schemas.NERVE_STATS,tools.nerve_stats))
    # Assistant actions intentionally reuse the existing nervous-event transport.
    # The transport therefore belongs to either module: custom configurations may
    # enable durable Assistant loops without enabling the Nervous subsystem.
    if policy.enabled("nervous") or policy.enabled("assistant_loops"):
        registrations.append(("nerve_nervous_event",schemas.NERVE_NERVOUS_EVENT,tools.nerve_nervous_event))
    if policy.enabled("work_supervision"):
        registrations += [
            ("nerve_supervise_card",schemas.NERVE_SUPERVISE_CARD,work_tools.nerve_supervise_card),
            ("nerve_work_event",schemas.NERVE_WORK_EVENT,work_tools.nerve_work_event),
            ("nerve_work_status",schemas.NERVE_WORK_STATUS,work_tools.nerve_work_status),
        ]
    if policy.enabled("remote_workers"):
        registrations += [
            ("nerve_remote_delegate_task",schemas.NERVE_REMOTE_DELEGATE_TASK,remote_tools.nerve_remote_delegate_task),
            ("nerve_remote_worker_status",schemas.NERVE_REMOTE_WORKER_STATUS,remote_tools.nerve_remote_worker_status),
            ("nerve_remote_worker_result",schemas.NERVE_REMOTE_WORKER_RESULT,remote_tools.nerve_remote_worker_result),
            ("nerve_remote_worker_cancel",schemas.NERVE_REMOTE_WORKER_CANCEL,remote_tools.nerve_remote_worker_cancel),
            ("nerve_remote_worker_control",schemas.NERVE_REMOTE_WORKER_CONTROL,remote_tools.nerve_remote_worker_control),
        ]
    if not headless_worker:
        for name,schema,handler in registrations:
            ctx.register_tool(name=name,toolset="nerve",schema=schema,handler=handler)

    def _pre_tool_control(**kwargs):
        callbacks=[]
        if policy.enabled("remote_workers"): callbacks.append(remote_control.pre_tool_call)
        if policy.enabled("work_supervision"): callbacks.append(work_hooks.pre_tool_call)
        if not headless_worker and policy.enabled("nervous"): callbacks.append(nervous.pre_tool_call)
        if not headless_worker and policy.enabled("action_gate"): callbacks.append(gate.pre_tool_call)
        for callback in callbacks:
            directive=callback(**kwargs)
            if directive is not None:return directive
        return None

    def _pre_llm(**kwargs):
        hints=[]
        if policy.enabled("work_supervision"):
            value=work_hooks.pre_llm_call(**kwargs)
            if value is not None:hints.append(str(value))
        if not headless_worker and policy.enabled("nervous"):
            value=nervous.pre_llm_call(**kwargs)
            if value is not None:hints.append(str(value))
        if not headless_worker and policy.enabled("assistant_loops"):
            value=assistant.pre_llm_call(**kwargs)
            if value is not None:hints.append(str(value))
        return "\n\n".join(x for x in hints if x.strip()) or None

    def _transform_tool_result(**kwargs):
        if policy.enabled("work_supervision"):
            value=work_hooks.transform_tool_result(**kwargs)
            if value is not None:return value
        if not headless_worker and policy.enabled("nervous"):
            return nervous.transform_tool_result(**kwargs)
        return None

    def _post_llm(**kwargs):
        if policy.enabled("work_supervision"):work_hooks.post_llm_call(**kwargs)
        if not headless_worker and policy.enabled("nervous"):nervous.post_llm_call(**kwargs)

    def _session_end(**kwargs):
        if policy.enabled("work_supervision"):work_hooks.on_session_end(**kwargs)
        if not headless_worker and policy.enabled("nervous"):nervous.on_session_end(**kwargs)

    pre_tool_needed=policy.enabled("remote_workers") or policy.enabled("work_supervision") or (not headless_worker and (policy.enabled("nervous") or policy.enabled("action_gate")))
    if pre_tool_needed:ctx.register_hook("pre_tool_call",_pre_tool_control)
    if policy.enabled("work_supervision"):ctx.register_hook("post_tool_call",work_hooks.post_tool_call)
    if not headless_worker and policy.enabled("context_governor"):ctx.register_hook("post_tool_call",ledger.observe_tool_call)
    if not headless_worker and policy.enabled("nervous"):ctx.register_hook("post_tool_call",nervous.post_tool_call)
    if policy.enabled("work_supervision") or (not headless_worker and (policy.enabled("nervous") or policy.enabled("assistant_loops"))):
        ctx.register_hook("pre_llm_call",_pre_llm)
    if policy.enabled("work_supervision") or (not headless_worker and policy.enabled("nervous")):
        ctx.register_hook("transform_tool_result",_transform_tool_result)
    if headless_worker and policy.enabled("work_supervision"):
        ctx.register_hook("pre_verify",work_hooks.pre_verify)
    elif not headless_worker and policy.enabled("nervous"):
        ctx.register_hook("pre_verify",nervous.pre_verify)
    elif policy.enabled("work_supervision"):
        ctx.register_hook("pre_verify",work_hooks.pre_verify)
    if policy.enabled("work_supervision"):
        ctx.register_hook("post_api_request",work_hooks.post_api_request)
        ctx.register_hook("api_request_error",work_hooks.api_request_error)
    if policy.enabled("work_supervision") or (not headless_worker and policy.enabled("nervous")):
        ctx.register_hook("post_llm_call",_post_llm)
        ctx.register_hook("on_session_end",_session_end)

    if not headless_worker and policy.enabled("context_governor") and bool(get("context_engine_register",True)) and hasattr(ctx,"register_context_engine"):
        ctx.register_context_engine(nerve_context_engine.NerveContextEngine(
            mode=get("context_engine_mode","shadow"),
            threshold_percent=get("context_engine_threshold_percent",0.72),
            protect_first_n=get("context_engine_protect_first_n",3),
            protect_last_n=get("context_engine_protect_last_n",6),
            shadow_trigger_percent=get("context_engine_shadow_trigger_percent",0.55),
            fallback_builtin=get("context_engine_fallback_builtin",True),
        ))

    if policy.enabled("shared_context") and not headless_worker:
        try:
            from .hermes_nerve.integrations.shared_context import detect
        except ImportError:
            from hermes_nerve.integrations.shared_context import detect
        shared=detect()
        if not shared["installed"]:
            logger.warning("Nerve profile %s enables Shared Context but HermesContextBus is not installed for this Hermes home",policy.profile)

    logger.info("Nerve %s loaded; profile=%s tools=%d headless_worker=%s",VERSION,policy.profile,0 if headless_worker else len(registrations),headless_worker)
    if headless_worker and startup_identity is None and policy.enabled("work_supervision"):
        message="Nerve headless supervision did not bind at startup; inspect supervision_diagnostics before treating this run as a valid Nerve-supervised measurement"
        if os.getenv("HERMES_NERVE_OFFLINE_VERIFY")=="1":logger.info("%s (offline verifier: not applicable)",message)
        else:logger.warning(message)

def register(ctx):
    """Register exact v0.2.3 Legacy behavior or the selected modular profile."""
    if not callable(getattr(ctx, "get_config", None)):
        # Hermes' context-engine directory loader (context.engine naming this plugin's
        # directory) passes a collector that cannot read plugin settings. Anything
        # registered here would silently run on defaults.
        logger.warning(
            "Nerve was loaded by a registration context that cannot read plugin settings, "
            "such as Hermes' context-engine directory loader when context.engine names the "
            "plugin directory. Nothing was registered here. Nerve's context engine is "
            "registered by the plugin system as 'jev': set context.engine: jev."
        )
        return None
    _warn_if_legacy_settings_ignored(ctx)
    policy = resolve_config(ctx.get_config)
    if policy.profile == "legacy":
        return _register_legacy(ctx)
    return _register_profile(ctx)