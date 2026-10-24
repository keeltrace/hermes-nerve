"""Normalized outcome dataset and optional local relevance-model seam."""

from __future__ import annotations

import math
import os
from collections import Counter, defaultdict
from pathlib import Path
from statistics import median
from typing import Any, Protocol

from .paths import hermes_home
from .jsonl import append_jsonl, read_jsonl
from .privacy import redact


class LocalRelevanceModel(Protocol):
    """Optional local-only classifier seam.

    Implementations return P(useful Jev disagreement | router feature vector).
    The deterministic router remains authoritative when no model is configured.
    """

    def predict_useful_disagreement(self, features: dict[str, Any]) -> float: ...


def outcome_path() -> Path:
    explicit = os.getenv("HERMES_NERVE_OUTCOMES", "").strip()
    return Path(explicit).expanduser() if explicit else hermes_home() / "nerve" / "decision-outcomes.jsonl"


class OutcomeStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or outcome_path()

    def append(self, row: dict[str, Any]) -> None:
        append_jsonl(self.path, redact(row))

    def rows(self) -> list[dict[str, Any]]:
        return read_jsonl(self.path)

    def report(self) -> dict[str, Any]:
        rows = self.rows()
        decisions = [r for r in rows if r.get("record_type") == "decision"]
        outcomes = [r for r in rows if r.get("record_type") == "outcome"]
        lifecycle = [r for r in rows if r.get("record_type") == "control_lifecycle"]
        counts = Counter()
        latencies: list[float] = []
        reported_cost = 0.0
        cost_missing_decisions = 0
        cost_reported_decisions = 0
        tokens = 0
        confidence_buckets: dict[str, list[int]] = defaultdict(list)
        by_event: dict[str, Counter] = defaultdict(Counter)
        decisions_by_id: dict[str, dict[str, Any]] = {}
        decisions_by_event: dict[str, dict[str, Any]] = {}

        for row in decisions:
            counts["decisions"] += 1
            did = str(row.get("decision_id") or "")
            eid = str(row.get("event_id") or "")
            if did:
                decisions_by_id[did] = row
            if eid:
                decisions_by_event[eid] = row
            if row.get("agreement") is True:
                counts["agreements"] += 1
            elif row.get("agreement") is False:
                counts["disagreements"] += 1
            if row.get("challenge_issued"):
                counts["challenges_issued"] += 1
            try:
                latencies.append(float(row.get("latency_ms") or 0.0))
            except (TypeError, ValueError):
                pass
            usage = row.get("usage") if isinstance(row.get("usage"), dict) else {}
            provider_backed = "usage" in row or bool(row.get("request_id"))
            if provider_backed:
                raw_cost = usage.get("cost")
                if raw_cost is None or raw_cost == "":
                    cost_missing_decisions += 1
                else:
                    try:
                        reported_cost += float(raw_cost)
                        cost_reported_decisions += 1
                    except (TypeError, ValueError):
                        cost_missing_decisions += 1
            try:
                tokens += int(usage.get("input_tokens") or 0) + int(usage.get("output_tokens") or 0)
            except (TypeError, ValueError):
                pass
            event_type = str(row.get("decision_type") or "unknown")
            by_event[event_type]["decisions"] += 1
            if row.get("agreement") is False:
                by_event[event_type]["disagreements"] += 1
            conf = float(row.get("jev_confidence") or 0.0)
            bucket = f"{int(conf * 10) / 10:.1f}-{min(1.0, int(conf * 10) / 10 + 0.1):.1f}"
            confidence_buckets[bucket].append(1 if row.get("agreement") is False else 0)

        lifecycle_by_decision: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in lifecycle:
            counts["control_lifecycle_rows"] += 1
            did = str(row.get("decision_id") or "")
            if did:
                lifecycle_by_decision[did].append(row)
            stage = str(row.get("stage") or "unknown").lower()
            disposition = str(row.get("challenge_disposition") or "").lower()
            counts[f"control_stage_{stage}"] += 1
            if stage == "created" and disposition != "shadow":
                counts["controls_created"] += 1
            if stage == "delivered":
                counts["controls_delivered"] += 1
            if disposition == "followed":
                counts["controls_followed"] += 1
                counts["challenges_accepted"] += 1
            elif disposition in {"overridden", "rejected"}:
                counts["controls_overridden"] += 1
                counts["challenges_rejected"] += 1
            elif disposition == "enforced":
                counts["controls_enforced"] += 1
            elif disposition in {"turn-ended", "superseded", "expired"}:
                counts["controls_expired"] += 1
            if row.get("attempted_override") is True:
                counts["control_override_attempts"] += 1

        correction_denominator = 0
        for row in outcomes:
            counts["outcomes"] += 1
            disposition = str(row.get("challenge_disposition") or "").lower()
            if disposition in {"accepted", "followed"}:
                counts["challenges_accepted"] += 1
            if disposition in {"rejected", "overridden"}:
                counts["challenges_rejected"] += 1
            if row.get("successful") is True:
                counts["successful_outcomes"] += 1
            if row.get("jev_changed_action") is True and "successful" in row:
                correction_denominator += 1
                if row.get("successful") is True:
                    counts["decision_correction_success"] += 1
            if row.get("false_pass") is True:
                counts["false_pass"] += 1
            if row.get("false_replan") is True:
                counts["false_replan"] += 1
            if row.get("premature_done_caught") is True:
                counts["premature_done_caught"] += 1
            if row.get("useful_disagreement") is True:
                counts["useful_disagreements"] += 1
            if row.get("useful_disagreement") is False:
                counts["useless_disagreements"] += 1

        sorted_latency = sorted(latencies)

        def percentile(p: float) -> float:
            if not sorted_latency:
                return 0.0
            pos = (len(sorted_latency) - 1) * p
            lo = int(math.floor(pos))
            hi = int(math.ceil(pos))
            if lo == hi:
                return sorted_latency[lo]
            return sorted_latency[lo] * (hi - pos) + sorted_latency[hi] * (pos - lo)

        attributed = 0
        for did, rows_for_decision in lifecycle_by_decision.items():
            if did in decisions_by_id and any(str(r.get("stage") or "") == "next_action" for r in rows_for_decision):
                attributed += 1
        counts["controls_with_next_action_attribution"] = attributed

        return {
            "scope": "profile-lifetime-ledger",
            "path": str(self.path),
            "rows": len(rows),
            "metrics": dict(sorted(counts.items())),
            "decision_correction_denominator": correction_denominator,
            "decision_correction_success_rate": (
                round(int(counts.get("decision_correction_success", 0)) / correction_denominator, 6)
                if correction_denominator else None
            ),
            "decision_correction_denominator_definition": (
                "Outcome rows where jev_changed_action=true and a boolean successful field is present."
            ),
            "control_attribution": {
                "decisions_with_ids": len(decisions_by_id),
                "control_decisions_with_next_action": attributed,
                "lifecycle_rows": len(lifecycle),
            },
            "average_latency_ms": round(sum(latencies) / len(latencies), 3) if latencies else 0.0,
            "p50_latency_ms": round(median(latencies), 3) if latencies else 0.0,
            "p95_latency_ms": round(percentile(0.95), 3),
            "provider_cost": None if cost_missing_decisions else round(reported_cost, 12),
            "provider_reported_cost": round(reported_cost, 12) if cost_reported_decisions else None,
            "provider_cost_reported_decisions": cost_reported_decisions,
            "provider_cost_missing_decisions": cost_missing_decisions,
            "jev_tokens": tokens,
            "by_event_type": {k: dict(v) for k, v in sorted(by_event.items())},
            "confidence_calibration": {
                k: {"samples": len(v), "disagreement_rate": round(sum(v) / len(v), 4) if v else 0.0}
                for k, v in sorted(confidence_buckets.items())
            },
        }


class HistoricalOutcomeModel:
    """Tiny local empirical model over labeled Jev disagreements.

    It deliberately does no remote inference. With insufficient labeled data it
    returns ``None`` so the deterministic router remains authoritative.
    """

    def __init__(self, store: OutcomeStore, min_samples: int = 8) -> None:
        self.store = store
        self.min_samples = max(3, int(min_samples))

    def predict(self, features: dict[str, Any]) -> tuple[float | None, int]:
        event_type = str(features.get("decision_type") or features.get("type") or "")
        objective_type = str(features.get("objective_type") or features.get("scope") or "")
        rows = self.store.rows()
        decision_rows = {str(r.get("event_id") or ""): r for r in rows if r.get("record_type") == "decision"}
        labels: list[int] = []
        for row in rows:
            if row.get("record_type") != "outcome" or "useful_disagreement" not in row:
                continue
            prior = decision_rows.get(str(row.get("event_id") or ""), {})
            if event_type and str(prior.get("decision_type") or "") != event_type:
                continue
            if objective_type and str(prior.get("objective_type") or "") not in {"", objective_type}:
                continue
            labels.append(1 if row.get("useful_disagreement") is True else 0)
        if len(labels) < self.min_samples:
            return None, len(labels)
        return (sum(labels) + 1.0) / (len(labels) + 2.0), len(labels)
