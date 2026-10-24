"""Hermes-visible tool handlers."""

from __future__ import annotations

import json

from .context import curate_context
from .engine import DecisionEngine
from . import assistant, experience, gate, ledger, lifecycle, receipts, nervous, selective_context
from .provenance import execution_provenance, result_provenance, runtime_identity

_engine_factory = DecisionEngine


def _engine() -> DecisionEngine:
    return _engine_factory()


def _ok(payload: dict) -> str:
    body = {"ok": True, **payload}
    inferred_live = bool(body.get("request_id") or body.get("requests"))
    body.setdefault("execution", execution_provenance(
        live_provider_call=inferred_live,
        request_id=str(body.get("request_id") or ""),
        receipt_id=str(body.get("receipt_id") or ""),
    ))
    execution = body.get("execution") if isinstance(body.get("execution"), dict) else {}
    live = bool(execution.get("live_provider_call"))
    body.setdefault("provenance_status", execution.get("provenance_status") or ("VERIFIED" if live and (body.get("request_id") or body.get("receipt_id")) else "LOCAL_ONLY" if not live else "UNVERIFIED"))
    body.setdefault("provenance", result_provenance(
        live_provider_call=live,
        request_id=str(body.get("request_id") or ""),
        receipt_id=str(body.get("receipt_id") or ""),
        provider=str(body.get("provider") or ""),
        transport=str(execution.get("transport") or ""),
        model=str(body.get("model") or ""),
        contract=str(body.get("contract") or ""),
        stale=bool(body.get("stale")),
    ))
    if bool(body.get("stale")):
        body["provenance_status"] = "UNVERIFIED"
        provenance = dict(body.get("provenance") or {})
        provenance["provenance_status"] = "UNVERIFIED"
        provenance["stale"] = True
        body["provenance"] = provenance
        execution = dict(body.get("execution") or {})
        execution["provenance_status"] = "UNVERIFIED"
        body["execution"] = execution
    return json.dumps(body, sort_keys=True)


def _error(exc: Exception) -> str:
    execution = execution_provenance(live_provider_call=False, error=True)
    return json.dumps({
        "ok": False,
        "error": str(exc),
        "provenance_status": "ERROR",
        "provenance": result_provenance(live_provider_call=False, error=True),
        "execution": execution,
    }, sort_keys=True)


def nerve_decide(args: dict, **kwargs) -> str:
    try:
        result = _engine().decide(
            state=args.get("state"),
            instructions=str(args.get("instructions") or "").strip(),
            choices=list(args.get("choices") or []),
            criteria=args.get("criteria") if isinstance(args.get("criteria"), dict) else None,
            contract=str(args.get("contract") or "decision/v1"),
        )
        return _ok(result.as_dict())
    except Exception as exc:  # handlers must return errors, not raise through Hermes
        return _error(exc)


def nerve_rank(args: dict, **kwargs) -> str:
    try:
        items = args.get("items")
        if not isinstance(items, dict) or len(items) < 2:
            raise ValueError("items must be an object with at least two candidates")
        result = _engine().rank(
            state=args.get("state"),
            instructions=str(args.get("instructions") or "").strip(),
            items=items,
            contract=str(args.get("contract") or "rank/v1"),
        )
        return _ok(result)
    except Exception as exc:
        return _error(exc)


def nerve_verify(args: dict, **kwargs) -> str:
    try:
        result = _engine().verify(
            state=args.get("state"),
            instructions=str(args.get("instructions") or "").strip(),
            contract=str(args.get("contract") or "verify/v1"),
        )
        lifecycle.record_verification(
            value=result.value, contract=result.contract, confidence=result.confidence, probabilities=result.probabilities
        )
        return _ok(result.as_dict())
    except Exception as exc:
        return _error(exc)


def nerve_assess(args: dict, **kwargs) -> str:
    try:
        questions = args.get("questions")
        if not isinstance(questions, dict):
            raise ValueError("questions must be an object")
        result = _engine().assess(
            state=args.get("state"),
            questions=questions,
            contract=str(args.get("contract") or "assess/v1"),
        )
        return _ok(result)
    except Exception as exc:
        return _error(exc)


def nerve_context_curate(args: dict, **kwargs) -> str:
    try:
        result = curate_context(
            goal=str(args.get("goal") or "").strip(),
            items=args.get("items"),
            preserve_tail=args.get("preserve_tail"),
            min_confidence=args.get("min_confidence"),
            preview_chars=args.get("preview_chars"),
            stub_chars=args.get("stub_chars"),
            anchor_chars=args.get("anchor_chars"),
            mode=args.get("mode"),
            policy=args.get("policy") if isinstance(args.get("policy"), dict) else None,
            contract=str(args.get("contract") or "context-curation/v2"),
        )
        return _ok(result)
    except Exception as exc:
        return _error(exc)


def nerve_context_rehydrate(args: dict, **kwargs) -> str:
    try:
        result = ledger.rehydrate(str(args.get("evidence_id") or ""))
        return _ok({
            "contract": str(args.get("contract") or "context-rehydrate/v1"),
            "rehydrated": result,
            "ledger": ledger.stats(),
            "execution": execution_provenance(live_provider_call=False, transport="local-evidence-ledger"),
        })
    except Exception as exc:
        return _error(exc)


def nerve_context_explain(args: dict, **kwargs) -> str:
    """Explain why local verified experience is believed or retrieved."""
    try:
        store = experience.default_store()
        claim_id = str(args.get("claim_id") or "").strip()
        query = str(args.get("query") or "").strip()
        if claim_id:
            payload = {"explanation": store.explain(claim_id)}
        elif query:
            payload = {
                "query_sha256": experience.canonical_hash(query),
                "matches": store.retrieve(
                    query,
                    scope=args.get("scope") if isinstance(args.get("scope"), dict) else None,
                    limit=args.get("limit", 8),
                    include_contradicted=bool(args.get("include_contradicted", False)),
                ),
            }
        else:
            payload = {"experience": store.report()}
        return _ok({
            "contract": str(args.get("contract") or "context-explain/v1"),
            **payload,
            "execution": execution_provenance(live_provider_call=False, transport="local-verified-experience"),
        })
    except Exception as exc:
        return _error(exc)



def nerve_nervous_event(args: dict, **kwargs) -> str:
    try:
        event = dict(args or {})
        event_type = str(event.get("type") or "").strip().lower()
        if event_type.startswith("assistant."):
            if not assistant.enabled():
                raise ValueError("Assistant loops are disabled")
            state = event.get("state") if isinstance(event.get("state"), dict) else {}
            if event_type == "assistant.status":
                result = assistant.status()
            elif event_type == "assistant.add_loop":
                result = assistant.add_loop(
                    title=str(event.get("goal") or state.get("title") or ""),
                    next_move=str(state.get("next") or ""),
                    owner=str(state.get("owner") or "agent"),
                    depends_on=state.get("depends_on"),
                    trigger=state.get("trigger"),
                    deadline=str(state.get("deadline") or ""),
                    definition_of_done=str(state.get("definition_of_done") or ""),
                )
            elif event_type == "assistant.update_loop":
                loop_id = str(state.get("loop_id") or "")
                changes = {k: state.get(k) for k in ("title","state","next","owner","depends_on","trigger","deadline","definition_of_done") if k in state}
                result = assistant.update_loop(loop_id, **changes)
            elif event_type == "assistant.complete":
                result = assistant.review_completion(str(state.get("loop_id") or ""), event.get("evidence"))
            elif event_type == "assistant.drop_loop":
                result = assistant.drop_loop(str(state.get("loop_id") or ""))
            else:
                raise ValueError(f"unsupported assistant event type: {event_type}")
            live = bool(result.get("provider_call")) if isinstance(result, dict) else False
            return _ok({
                "contract": "assistant-accountability/v2",
                "event_type": event_type,
                "assistant": result,
                "execution": execution_provenance(live_provider_call=live, transport="nerve-nervous-event"),
            })
        for key in ("turn_id", "session_id"):
            if not event.get(key) and kwargs.get(key):
                event[key] = kwargs.get(key)
        result = nervous.emit_event(event)
        return _ok({
            "contract": "hermes/nerve-nervous-event/v1",
            "event": result,
            "nervous": nervous.status(str(event.get("turn_id") or ""), str(event.get("session_id") or "")),
            "execution": execution_provenance(live_provider_call=False, transport="local-nervous-router"),
        })
    except Exception as exc:
        return _error(exc)

def _compact_receipts(report: dict) -> dict:
    return {key: report.get(key) for key in (
        "receipt_count", "provider_calls", "by_contract", "by_model", "total_cost",
        "provider_reported_cost", "provider_cost_reported_calls", "provider_cost_missing_calls",
        "input_tokens", "output_tokens", "average_latency_ms",
    )}


def _compact_gate(report: dict) -> dict:
    return {key: report.get(key) for key in (
        "metric_scope", "gate_mode", "scope", "event_count", "hook_observations",
        "disabled", "bypassed", "evaluated", "provider_errors", "provider_calls",
        "average_provider_latency_ms", "estimated_provider_calls_avoided", "by_action", "by_reason",
    )}


def _compact_context(report: dict) -> dict:
    return {key: report.get(key) for key in (
        "enabled", "detail", "evidence_events", "shadow_plans", "rehydrations",
        "unique_evidence", "compacted_unique_evidence", "rehydrated_compacted_evidence",
        "recovery_demand_rate", "shadow_action_counts", "shadow_proposed_saved_chars",
    )}


def _compact_nervous(report: dict) -> dict:
    quality = report.get("quality_metrics") if isinstance(report.get("quality_metrics"), dict) else {}
    return {
        "scope": report.get("scope"),
        "enabled": report.get("enabled"),
        "mode": report.get("mode"),
        "admission_enabled": report.get("admission_enabled"),
        "active_turns": report.get("active_turns"),
        "admissions": report.get("admissions"),
        "quality_metrics": quality,
    }


def nerve_stats(args: dict, **kwargs) -> str:
    """Bounded local telemetry. Default is deliberately compact (v0.2.1)."""
    try:
        section = str(args.get("section") or "summary").strip().lower()
        allowed = {"summary", "receipts", "gate", "context", "selective_context", "nervous", "quality", "outcomes", "identity", "all"}
        if section not in allowed:
            raise ValueError(f"section must be one of: {', '.join(sorted(allowed))}")
        try:
            recent_limit = max(0, min(20, int(args.get("recent_limit", 3))))
        except (TypeError, ValueError):
            recent_limit = 3
        include_recent = bool(args.get("include_recent", False))

        receipt_report = receipts.report(recent_limit=recent_limit if include_recent else 0)
        gate_report = gate.report(recent_limit=recent_limit if include_recent else 0)
        context_report = ledger.report()
        nervous_report = nervous.report(
            recent_limit=recent_limit, include_recent=include_recent,
            include_outcomes=section in {"outcomes", "all"},
        )
        identity = runtime_identity()

        base = {
            "contract": "stats/v2",
            "section": section,
            "scope_labels": {
                "receipts": "profile-lifetime receipt ledger",
                "gate": "profile-lifetime gate-event ledger",
                "context": "profile-lifetime context ledger",
                "nervous_runtime": "current process",
                "nervous_outcomes": "profile-lifetime outcome ledger",
                "recent": "rolling current-process or ledger tail, only when include_recent=true",
            },
            "execution": execution_provenance(live_provider_call=False, transport="local-telemetry"),
            "runtime_identity": {key: identity.get(key) for key in (
                "version", "build_id", "loaded_code_tree_sha256", "status", "issues", "plugin_root", "module_roots",
                "manifest_path", "manifest_version", "package_version", "package_metadata_path", "source_tree_sha256",
                "loaded_module_count",
            )},
        }
        if section == "summary":
            base.update({
                "receipts": _compact_receipts(receipt_report),
                "gate": _compact_gate(gate_report),
                "context": _compact_context(context_report),
                "selective_context": selective_context.report(),
                "nervous": _compact_nervous(nervous_report),
                "note": "Compact by default to avoid tool-result context bloat. Use section=... and include_recent=true for targeted detail.",
            })
        elif section == "identity":
            base["runtime_identity"] = identity
        elif section == "receipts":
            base["receipts"] = receipt_report
        elif section == "gate":
            base["gate"] = gate_report
        elif section == "context":
            base["context"] = context_report
        elif section == "selective_context":
            base["selective_context"] = selective_context.report()
        elif section == "nervous":
            base["nervous"] = nervous_report
        elif section == "quality":
            base["quality"] = nervous_report.get("quality_metrics", {})
        elif section == "outcomes":
            base["outcomes"] = nervous_report.get("outcomes", {})
        else:  # all
            base.update({
                "receipts": receipt_report,
                "gate": gate_report,
                "context": context_report,
                "selective_context": selective_context.report(),
                "nervous": nervous_report,
            })

        encoded = _ok(base)
        # Default/targeted calls are bounded defensively. A caller can still ask
        # for all + recent, but one accidental stats call cannot inject ~30 KB.
        hard_cap = 16000
        if len(encoded) > hard_cap and section in {"summary", "quality", "gate", "receipts", "context", "nervous", "outcomes"}:
            trimmed = {
                "contract": "stats/v2", "section": section, "truncated": True,
                "original_chars": len(encoded),
                "note": "Telemetry exceeded the 16k bounded tool-result cap; request a narrower section.",
                "scope_labels": base["scope_labels"],
                "execution": base["execution"],
            }
            if section == "summary":
                trimmed.update({
                    "receipts": _compact_receipts(receipt_report),
                    "gate": _compact_gate(gate_report),
                    "context": _compact_context(context_report),
                    "nervous": {
                        "enabled": nervous_report.get("enabled"),
                        "mode": nervous_report.get("mode"),
                        "admissions": nervous_report.get("admissions"),
                    },
                })
            encoded = _ok(trimmed)
        return encoded
    except Exception as exc:
        return _error(exc)