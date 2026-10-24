"""Privacy-minimized paired primary/shadow Reflex telemetry."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..jsonl import append_jsonl, read_jsonl
from ..paths import report_home
from ..provenance import classify_status


def _canonical_hash(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def default_path() -> Path:
    return report_home() / "reflex" / "shadow.jsonl"


def answer_summary(answers: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for qid, raw in (answers or {}).items():
        if not isinstance(raw, dict):
            continue
        qtype = str(raw.get("type") or "")
        if qtype == "choice":
            value = raw.get("choice")
        elif qtype == "score":
            value = raw.get("score")
        elif qtype == "noul":
            value = raw.get("noul")
        else:
            value = raw.get("value")
        out[str(qid)] = {
            "type": qtype,
            "value": value,
            "confidence": raw.get("confidence"),
            "probabilities": raw.get("probabilities") if isinstance(raw.get("probabilities"), dict) else None,
        }
    return out


def _execution_summary(response: Any | None) -> dict[str, Any]:
    if response is None:
        return {"live_provider_call": None, "provenance_status": ""}
    live_provider_call = bool(getattr(response, "live_provider_call", True))
    request_id = str(getattr(response, "request_id", "") or "")
    return {
        "live_provider_call": live_provider_call,
        "provenance_status": classify_status(
            live_provider_call=live_provider_call,
            request_id=request_id,
        ),
    }


def _usage_summary(response: Any | None) -> dict[str, Any]:
    raw = getattr(response, "usage", {}) if response is not None else {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, Any] = {}
    for key, value in raw.items():
        if isinstance(value, (int, float, str, bool)) or value is None:
            out[str(key)] = value
    return out


def _percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(float(v) for v in values)
    if len(ordered) == 1:
        return round(ordered[0], 3)
    pos = (len(ordered) - 1) * min(1.0, max(0.0, float(quantile)))
    lo = int(pos)
    hi = min(len(ordered) - 1, lo + 1)
    frac = pos - lo
    return round(ordered[lo] * (1.0 - frac) + ordered[hi] * frac, 3)


def append_shadow_record(
    *,
    path: Path | None,
    state: Any,
    questions: dict[str, Any],
    primary: Any,
    shadow: Any | None = None,
    shadow_error: str = "",
) -> None:
    primary_answers = answer_summary(getattr(primary, "answers", {}) or {})
    shadow_answers = answer_summary(getattr(shadow, "answers", {}) or {}) if shadow is not None else {}
    compared = sorted(set(primary_answers) | set(shadow_answers))
    agreements: dict[str, bool] = {}
    for qid in compared:
        if qid in primary_answers and qid in shadow_answers:
            agreements[qid] = primary_answers[qid].get("value") == shadow_answers[qid].get("value")
    append_jsonl(
        path or default_path(),
        {
            "schema": "hermes-reflex-shadow/v1",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "state_sha256": _canonical_hash(state),
            "questions_sha256": _canonical_hash(questions),
            "question_ids": sorted(str(k) for k in questions),
            "primary": {
                "provider": str(getattr(primary, "provider", "") or ""),
                "model": str(getattr(primary, "model", "") or ""),
                "transport": str(getattr(primary, "transport", "") or ""),
                "latency_ms": float(getattr(primary, "latency_ms", 0.0) or 0.0),
                "answers": primary_answers,
                "usage": _usage_summary(primary),
                **_execution_summary(primary),
            },
            "shadow": {
                "provider": str(getattr(shadow, "provider", "") or "") if shadow is not None else "",
                "model": str(getattr(shadow, "model", "") or "") if shadow is not None else "",
                "transport": str(getattr(shadow, "transport", "") or "") if shadow is not None else "",
                "latency_ms": float(getattr(shadow, "latency_ms", 0.0) or 0.0) if shadow is not None else 0.0,
                "answers": shadow_answers,
                "usage": _usage_summary(shadow),
                "error": str(shadow_error or ""),
                **_execution_summary(shadow),
            },
            "agreements": agreements,
        },
    )


def report(path: Path | None = None, *, recent_limit: int = 5) -> dict[str, Any]:
    selected = path or default_path()
    rows = read_jsonl(selected)
    paired = 0
    agreed = 0
    disagreed = 0
    shadow_errors = 0
    shadow_timeouts = 0
    shadow_latencies: list[float] = []
    shadow_input_tokens = 0
    shadow_output_tokens = 0
    shadow_provenance: dict[str, int] = {}
    per_question: dict[str, dict[str, int]] = {}
    for row in rows:
        shadow = row.get("shadow") if isinstance(row.get("shadow"), dict) else {}
        error_text = str(shadow.get("error") or "")
        if error_text:
            shadow_errors += 1
            if "timeout" in error_text.lower() or "timed out" in error_text.lower():
                shadow_timeouts += 1
        else:
            try:
                shadow_latencies.append(float(shadow.get("latency_ms") or 0.0))
            except (TypeError, ValueError):
                pass
        provenance_status = str(shadow.get("provenance_status") or "")
        if provenance_status:
            shadow_provenance[provenance_status] = shadow_provenance.get(provenance_status, 0) + 1
        usage = shadow.get("usage") if isinstance(shadow.get("usage"), dict) else {}
        try:
            shadow_input_tokens += int(usage.get("input_tokens") or 0)
        except (TypeError, ValueError):
            pass
        try:
            shadow_output_tokens += int(usage.get("output_tokens") or 0)
        except (TypeError, ValueError):
            pass
        agreements = row.get("agreements") if isinstance(row.get("agreements"), dict) else {}
        for qid, value in agreements.items():
            paired += 1
            bucket = per_question.setdefault(str(qid), {"paired": 0, "agree": 0, "disagree": 0})
            bucket["paired"] += 1
            if bool(value):
                agreed += 1
                bucket["agree"] += 1
            else:
                disagreed += 1
                bucket["disagree"] += 1
    record_count = len(rows)
    return {
        "path": str(selected),
        "records": record_count,
        "paired_answers": paired,
        "agreements": agreed,
        "disagreements": disagreed,
        "agreement_rate": round(agreed / paired, 6) if paired else None,
        "shadow_errors": shadow_errors,
        "shadow_error_rate": round(shadow_errors / record_count, 6) if record_count else None,
        "shadow_timeouts": shadow_timeouts,
        "shadow_timeout_rate": round(shadow_timeouts / record_count, 6) if record_count else None,
        "shadow_latency_ms": {
            "count": len(shadow_latencies),
            "p50": _percentile(shadow_latencies, 0.50),
            "p95": _percentile(shadow_latencies, 0.95),
        },
        "shadow_usage": {
            "input_tokens": shadow_input_tokens,
            "output_tokens": shadow_output_tokens,
        },
        "shadow_provenance": shadow_provenance,
        "per_question": per_question,
        "recent": rows[-max(0, int(recent_limit)) :] if recent_limit else [],
    }
