"""Decision receipts: JSONL, privacy-minimized by default."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .privacy import canonical_hash
from .provenance import execution_provenance, result_provenance
from .paths import hermes_home
from .jsonl import append_jsonl, read_jsonl

_configured_detail: str | None = None
_configured_enabled: bool = True


def configure(*, enabled: Any = True, detail: Any = None) -> None:
    """Apply receipt persistence settings."""
    global _configured_detail, _configured_enabled
    _configured_enabled = bool(enabled)
    value = str(detail if detail is not None else "hash").strip().lower()
    _configured_detail = value if value in {"hash", "sanitized"} else "hash"


def enabled() -> bool:
    return _configured_enabled


def receipt_detail() -> str:
    if _configured_detail is not None:
        return _configured_detail
    value = os.getenv("HERMES_NERVE_RECEIPT_DETAIL", "hash").strip().lower()
    return value if value in {"hash", "sanitized"} else "hash"


def receipt_path() -> Path:
    explicit = os.getenv("HERMES_NERVE_RECEIPTS")
    if explicit:
        return Path(explicit).expanduser()
    return hermes_home() / "nerve" / "receipts.jsonl"


def write_receipt(*, contract: str, state: Any, result: dict[str, Any], model: str, latency_ms: float) -> dict[str, Any]:
    """Persist an immutable, content-bound local receipt and return the stored record.

    ``receipt_id`` binds the contract, sanitized subject hash, result hash, model,
    provider request id, and creation timestamp. The outward engine result reuses
    this exact stored provenance block so a model-facing Jev claim can be audited
    one-to-one against the receipt ledger.
    """
    created_at = datetime.now(timezone.utc).isoformat()
    state_sha256 = canonical_hash(state)
    raw_result = dict(result or {})
    result_sha256 = canonical_hash(raw_result)
    execution = raw_result.get("execution") if isinstance(raw_result.get("execution"), dict) else execution_provenance(live_provider_call=True)
    identity = {
        "schema": "hermes-nerve-receipt/v3",
        "created_at": created_at,
        "contract": contract,
        "state_sha256": state_sha256,
        "result_sha256": result_sha256,
        "model": model,
        "request_id": str(raw_result.get("request_id") or ""),
    }
    receipt_id = "jevrec-" + canonical_hash(identity)[:32]
    transport = str(execution.get("transport") or "openrouter-decisions")
    live = bool(execution.get("live_provider_call"))
    provenance = result_provenance(
        live_provider_call=live,
        request_id=str(raw_result.get("request_id") or ""),
        receipt_id=receipt_id,
        provider=str(raw_result.get("provider") or ""),
        transport=transport,
        model=str(raw_result.get("model") or model or ""),
        contract=contract,
        created_at=created_at,
        subject_sha256=state_sha256,
        result_sha256=result_sha256,
    )
    stored_result = dict(raw_result)
    stored_result["receipt_id"] = receipt_id
    stored_result["provenance_status"] = provenance["provenance_status"]
    stored_result["provenance"] = provenance
    stored_result["execution"] = execution_provenance(
        live_provider_call=live,
        transport=transport,
        request_id=str(raw_result.get("request_id") or ""),
        receipt_id=receipt_id,
    )
    record: dict[str, Any] = {
        "schema": "hermes-nerve-receipt/v3",
        "receipt_id": receipt_id,
        "timestamp": created_at,
        "contract": contract,
        "state_sha256": state_sha256,
        "result_sha256": result_sha256,
        "model": model,
        "latency_ms": round(latency_ms, 3),
        "result": stored_result,
        "provenance": provenance,
        "execution": stored_result["execution"],
    }
    if receipt_detail() == "sanitized":
        record["state"] = state
    if enabled():
        append_jsonl(receipt_path(), record)
    return record


def _iter_receipts(path: Path | None = None) -> list[dict[str, Any]]:
    return read_jsonl(path or receipt_path())


def report(path: Path | None = None, *, recent_limit: int = 8) -> dict[str, Any]:
    """Aggregate local Jev decision receipts without making a provider call."""
    selected = path or receipt_path()
    rows = _iter_receipts(selected)
    by_contract: dict[str, int] = {}
    by_model: dict[str, int] = {}
    reported_cost = 0.0
    provider_cost_missing_calls = 0
    provider_cost_reported_calls = 0
    input_tokens = 0
    output_tokens = 0
    total_latency = 0.0
    provider_calls = 0
    recent: list[dict[str, Any]] = []

    for row in rows:
        contract = str(row.get("contract") or "unknown")
        by_contract[contract] = by_contract.get(contract, 0) + 1
        model = str(row.get("model") or "unknown")
        by_model[model] = by_model.get(model, 0) + 1
        try:
            total_latency += float(row.get("latency_ms") or 0.0)
        except (TypeError, ValueError):
            pass
        result = row.get("result") if isinstance(row.get("result"), dict) else {}
        usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
        execution = row.get("execution") if isinstance(row.get("execution"), dict) else {}
        live_provider_call = bool(execution.get("live_provider_call"))
        if live_provider_call:
            provider_calls += 1
            raw_cost = usage.get("cost")
            if raw_cost is None or raw_cost == "":
                provider_cost_missing_calls += 1
            else:
                try:
                    reported_cost += float(raw_cost)
                    provider_cost_reported_calls += 1
                except (TypeError, ValueError):
                    provider_cost_missing_calls += 1
        for key, target in (("input_tokens", "input"), ("output_tokens", "output")):
            try:
                value = int(usage.get(key) or 0)
            except (TypeError, ValueError):
                value = 0
            if target == "input":
                input_tokens += value
            else:
                output_tokens += value
    limit = max(0, min(50, int(recent_limit or 0)))
    for row in rows[-limit:] if limit else []:
        result = row.get("result") if isinstance(row.get("result"), dict) else {}
        usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
        recent.append({
            "timestamp": row.get("timestamp"),
            "contract": row.get("contract"),
            "model": row.get("model"),
            "latency_ms": row.get("latency_ms"),
            "request_id": result.get("request_id"),
            "receipt_id": row.get("receipt_id") or result.get("receipt_id"),
            "provenance_status": result.get("provenance_status"),
            "value": result.get("value"),
            "cost": usage.get("cost"),
            "input_tokens": usage.get("input_tokens"),
            "output_tokens": usage.get("output_tokens"),
        })

    return {
        "path": str(selected),
        "receipt_count": len(rows),
        "provider_calls": provider_calls,
        "by_contract": dict(sorted(by_contract.items())),
        "by_model": dict(sorted(by_model.items())),
        "total_cost": None if provider_cost_missing_calls else round(reported_cost, 12),
        "provider_reported_cost": round(reported_cost, 12) if provider_cost_reported_calls else None,
        "provider_cost_reported_calls": provider_cost_reported_calls,
        "provider_cost_missing_calls": provider_cost_missing_calls,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "average_latency_ms": round(total_latency / len(rows), 3) if rows else 0.0,
        "recent": recent,
    }
