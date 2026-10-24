"""Verified Experience for Nerve 0.3.2.

This module stores evidence-backed, scoped experience rather than conversations.
It is deliberately local, deterministic, provider-free, append-only, and advisory:
Hermes remains the authority for task state and policy changes.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import math
import os
from pathlib import Path
import re
from typing import Any, Iterable

from .jsonl import append_jsonl, read_jsonl
from .paths import hermes_home
from .privacy import canonical_hash, redact


CLAIM_TYPES = {"observed", "inferred", "verified", "disproven"}
CLAIM_STATUSES = {"active", "superseded", "contradicted", "expired"}
_SCOPE_KEYS = ("repository", "branch", "task_type", "worker", "backend", "host", "environment")
_WORD_RE = re.compile(r"[a-z0-9_./:-]+")
_configured_enabled = False


def configure(*, enabled: Any = True) -> None:
    global _configured_enabled
    _configured_enabled = bool(enabled)


def enabled() -> bool:
    return _configured_enabled


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_time(value: str | None) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _clamp(value: Any, low: float = 0.0, high: float = 1.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = low
    return max(low, min(high, number))


def _clean_scope(scope: dict[str, Any] | None) -> dict[str, str]:
    if not isinstance(scope, dict):
        return {}
    safe = redact(scope)
    clean: dict[str, str] = {}
    for key in _SCOPE_KEYS:
        value = str(safe.get(key) or "").strip()
        if value:
            clean[key] = value
    return clean


def _tokens(text: str) -> set[str]:
    return set(_WORD_RE.findall(str(text or "").lower()))


@dataclass(frozen=True)
class Claim:
    claim_id: str
    subject: str
    statement: str
    claim_type: str
    evidence_ids: tuple[str, ...]
    confidence: float
    polarity: int
    scope: dict[str, str]
    status: str = "active"
    created_at: str = field(default_factory=utc_now)
    updated_at: str = ""
    valid_from: str = ""
    valid_until: str = ""
    last_verified: str = ""
    supersedes: str = ""
    contradicted_by: tuple[str, ...] = ()
    provenance: dict[str, Any] = field(default_factory=dict)
    estimated_tokens: int = 0
    retrieval_count: int = 0
    useful_outcomes: int = 0
    harmful_outcomes: int = 0
    tokens_saved: int = 0
    tags: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        value = asdict(self)
        for key in ("evidence_ids", "contradicted_by", "tags"):
            value[key] = list(value[key])
        return value


@dataclass(frozen=True)
class RunCapsule:
    capsule_id: str
    task_id: str
    run_id: int | None
    contract_hash: str
    verified_criteria: tuple[str, ...]
    failed_paths: tuple[dict[str, Any], ...]
    discoveries: tuple[dict[str, Any], ...]
    evidence_ids: tuple[str, ...]
    artifacts: tuple[dict[str, Any], ...]
    unresolved: tuple[str, ...]
    next_move: str
    summary: str
    scope: dict[str, str]
    source_checkpoint_id: str = ""
    decision_id: str = ""
    created_at: str = field(default_factory=utc_now)

    def as_dict(self) -> dict[str, Any]:
        value = asdict(self)
        for key in ("verified_criteria", "failed_paths", "discoveries", "evidence_ids", "artifacts", "unresolved"):
            value[key] = list(value[key])
        return value


class ExperienceStore:
    """Append-only local experience store.

    State is reconstructed from events. Records are redacted before persistence.
    Reads/writes use the existing serialized JSONL helper, so this introduces no
    background service, provider call, network activity, or startup scan.
    """

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path).expanduser() if path else experience_path()

    def _append(self, event: str, payload: dict[str, Any]) -> dict[str, Any]:
        row = {
            "schema": "hermes-nerve-experience/v1",
            "event": event,
            "timestamp": utc_now(),
            **redact(payload),
        }
        append_jsonl(self.path, row)
        return row

    def rows(self) -> list[dict[str, Any]]:
        return read_jsonl(self.path)

    def claims(self) -> dict[str, dict[str, Any]]:
        claims: dict[str, dict[str, Any]] = {}
        for row in self.rows():
            event = row.get("event")
            if event == "claim_created" and isinstance(row.get("claim"), dict):
                claim = dict(row["claim"])
                claims[str(claim.get("claim_id") or "")] = claim
            elif event == "claim_status":
                claim = claims.get(str(row.get("claim_id") or ""))
                if claim:
                    claim["status"] = str(row.get("status") or claim.get("status") or "active")
                    claim["updated_at"] = str(row.get("timestamp") or "")
                    if row.get("contradicted_by"):
                        values = list(claim.get("contradicted_by") or [])
                        other = str(row.get("contradicted_by"))
                        if other and other not in values:
                            values.append(other)
                        claim["contradicted_by"] = values
            elif event == "claim_retrieved":
                claim = claims.get(str(row.get("claim_id") or ""))
                if claim:
                    claim["retrieval_count"] = int(claim.get("retrieval_count") or 0) + 1
            elif event == "claim_outcome":
                claim = claims.get(str(row.get("claim_id") or ""))
                if claim:
                    if row.get("successful") is True:
                        claim["useful_outcomes"] = int(claim.get("useful_outcomes") or 0) + 1
                    elif row.get("successful") is False:
                        claim["harmful_outcomes"] = int(claim.get("harmful_outcomes") or 0) + 1
                    claim["tokens_saved"] = int(claim.get("tokens_saved") or 0) + int(row.get("tokens_saved") or 0)
        return claims

    def create_claim(
        self,
        *,
        subject: str,
        statement: str,
        claim_type: str,
        evidence_ids: Iterable[str] = (),
        confidence: float = 1.0,
        polarity: int = 0,
        scope: dict[str, Any] | None = None,
        valid_from: str = "",
        valid_until: str = "",
        last_verified: str = "",
        supersedes: str = "",
        provenance: dict[str, Any] | None = None,
        estimated_tokens: int = 0,
        tags: Iterable[str] = (),
        admit: bool = True,
        expected_reuse: float = 0.5,
        admission_threshold: float = 0.34,
    ) -> dict[str, Any]:
        clean_subject = str(redact(subject)).strip()
        clean_statement = str(redact(statement)).strip()
        kind = str(claim_type or "").strip().lower()
        if not clean_subject or not clean_statement:
            raise ValueError("subject and statement are required")
        if kind not in CLAIM_TYPES:
            raise ValueError("claim_type must be observed, inferred, verified, or disproven")
        evidence = tuple(dict.fromkeys(str(x).strip() for x in evidence_ids if str(x).strip()))
        if kind in {"verified", "disproven"} and not evidence:
            raise ValueError(f"{kind} claims require evidence_ids")
        polarity_value = int(polarity or 0)
        if polarity_value not in {-1, 0, 1}:
            raise ValueError("polarity must be -1, 0, or 1")
        clean_scope = _clean_scope(scope)
        confidence_value = _clamp(confidence)
        estimated = max(0, int(estimated_tokens or 0))
        admission = admission_decision(
            claim_type=kind,
            confidence=confidence_value,
            evidence_count=len(evidence),
            estimated_tokens=estimated,
            expected_reuse=expected_reuse,
            threshold=admission_threshold,
        )
        if admit and not admission["admitted"]:
            return {"admitted": False, "admission": admission}

        now = utc_now()
        existing = self.claims()
        semantic = {
            "subject": clean_subject,
            "statement": clean_statement,
            "claim_type": kind,
            "evidence_ids": list(evidence),
            "confidence": round(confidence_value, 6),
            "polarity": polarity_value,
            "scope": clean_scope,
            "valid_from": str(valid_from or ""),
            "valid_until": str(valid_until or ""),
            "tags": list(dict.fromkeys(str(x).strip() for x in tags if str(x).strip())),
        }
        semantic_sha = canonical_hash(semantic)
        for prior in existing.values():
            prior_semantic = {
                "subject": prior.get("subject"),
                "statement": prior.get("statement"),
                "claim_type": prior.get("claim_type"),
                "evidence_ids": list(prior.get("evidence_ids") or []),
                "confidence": round(float(prior.get("confidence") or 0.0), 6),
                "polarity": int(prior.get("polarity") or 0),
                "scope": _clean_scope(prior.get("scope") if isinstance(prior.get("scope"), dict) else {}),
                "valid_from": str(prior.get("valid_from") or ""),
                "valid_until": str(prior.get("valid_until") or ""),
                "tags": list(prior.get("tags") or []),
            }
            if canonical_hash(prior_semantic) == semantic_sha and effective_status(prior) == "active":
                return {
                    "admitted": True,
                    "admission": admission,
                    "claim": prior,
                    "conflicts": [],
                    "deduplicated": True,
                }
        supersedes_id = str(supersedes or "").strip()
        if supersedes_id and supersedes_id not in existing:
            raise ValueError(f"superseded claim not found: {supersedes_id}")
        identity = {
            "subject": clean_subject,
            "statement": clean_statement,
            "claim_type": kind,
            "evidence_ids": evidence,
            "scope": clean_scope,
            "created_at": now,
        }
        claim_id = "ncl-" + canonical_hash(identity)[:24]
        claim = Claim(
            claim_id=claim_id,
            subject=clean_subject,
            statement=clean_statement,
            claim_type=kind,
            evidence_ids=evidence,
            confidence=confidence_value,
            polarity=polarity_value,
            scope=clean_scope,
            created_at=now,
            updated_at=now,
            valid_from=str(valid_from or ""),
            valid_until=str(valid_until or ""),
            last_verified=str(last_verified or (now if kind == "verified" else "")),
            supersedes=supersedes_id,
            provenance=redact(provenance or {}),
            estimated_tokens=estimated,
            tags=tuple(dict.fromkeys(str(x).strip() for x in tags if str(x).strip())),
        )
        self._append("claim_created", {"claim": claim.as_dict(), "admission": admission})
        if supersedes_id:
            self._append("claim_status", {"claim_id": supersedes_id, "status": "superseded", "superseded_by": claim_id})

        conflicts = self.detect_conflicts(claim.as_dict(), claims=existing)
        for other_id in conflicts:
            self._append("claim_status", {"claim_id": claim_id, "status": "contradicted", "contradicted_by": other_id})
            self._append("claim_status", {"claim_id": other_id, "status": "contradicted", "contradicted_by": claim_id})
        result = claim.as_dict()
        if conflicts:
            result["status"] = "contradicted"
            result["contradicted_by"] = conflicts
        return {"admitted": True, "admission": admission, "claim": result, "conflicts": conflicts}

    def detect_conflicts(
        self, candidate: dict[str, Any], *, claims: dict[str, dict[str, Any]] | None = None, min_confidence: float = 0.70
    ) -> list[str]:
        subject = str(candidate.get("subject") or "").strip().lower()
        polarity = int(candidate.get("polarity") or 0)
        if not subject or polarity == 0 or float(candidate.get("confidence") or 0) < min_confidence:
            return []
        result: list[str] = []
        for claim_id, other in (claims or self.claims()).items():
            if str(other.get("status") or "active") not in {"active", "contradicted"}:
                continue
            if str(other.get("subject") or "").strip().lower() != subject:
                continue
            if int(other.get("polarity") or 0) != -polarity:
                continue
            if float(other.get("confidence") or 0) < min_confidence:
                continue
            if not scopes_overlap(candidate.get("scope") or {}, other.get("scope") or {}):
                continue
            result.append(claim_id)
        return sorted(set(result))

    def record_outcome(
        self,
        claim_id: str,
        *,
        successful: bool,
        decision_id: str = "",
        receipt_id: str = "",
        tokens_saved: int = 0,
        note: str = "",
    ) -> dict[str, Any]:
        if claim_id not in self.claims():
            raise ValueError(f"claim not found: {claim_id}")
        return self._append(
            "claim_outcome",
            {
                "claim_id": claim_id,
                "successful": bool(successful),
                "decision_id": str(decision_id or ""),
                "receipt_id": str(receipt_id or ""),
                "tokens_saved": int(tokens_saved or 0),
                "note": str(redact(note or "")),
            },
        )

    def retrieve(
        self,
        query: str,
        *,
        scope: dict[str, Any] | None = None,
        limit: int = 8,
        max_tokens: int = 1200,
        include_contradicted: bool = False,
        now: str | None = None,
        record: bool = True,
    ) -> list[dict[str, Any]]:
        query_tokens = _tokens(query)
        clean_scope = _clean_scope(scope)
        now_dt = _parse_time(now) or datetime.now(timezone.utc)
        scored: list[dict[str, Any]] = []
        for claim in self.claims().values():
            status = effective_status(claim, now_dt)
            if status in {"superseded", "expired"} or (status == "contradicted" and not include_contradicted):
                continue
            scope_score = scope_match_score(claim.get("scope") or {}, clean_scope)
            if scope_score < 0:
                continue
            haystack = _tokens(f"{claim.get('subject','')} {claim.get('statement','')} {' '.join(claim.get('tags') or [])}")
            relevance = 1.0 if not query_tokens else len(query_tokens & haystack) / max(1, len(query_tokens))
            if query_tokens and relevance <= 0:
                continue
            utility = utility_score(claim, scope_score=scope_score, now=now_dt, max_tokens=max_tokens)
            score = round(0.45 * relevance + 0.55 * utility["score"], 6)
            reasons = []
            if relevance > 0:
                reasons.append(f"text_match={relevance:.2f}")
            reasons.extend(utility["reasons"])
            scored.append({
                "claim": claim,
                "score": score,
                "score_components": {"relevance": round(relevance, 6), **utility["components"]},
                "why_retrieved": reasons,
            })
        scored.sort(key=lambda x: (-x["score"], str(x["claim"].get("claim_id") or "")))
        selected = scored[: max(0, min(50, int(limit or 0)))]
        if record:
            for item in selected:
                self._append("claim_retrieved", {
                    "claim_id": item["claim"]["claim_id"],
                    "query_sha256": canonical_hash(str(query or "")),
                    "scope": clean_scope,
                    "score": item["score"],
                    "why_retrieved": item["why_retrieved"],
                })
        return selected

    def explain(self, claim_id: str) -> dict[str, Any]:
        claim = self.claims().get(str(claim_id or ""))
        if not claim:
            raise ValueError(f"claim not found: {claim_id}")
        related = []
        latest_retrieval = None
        for row in self.rows():
            if str(row.get("claim_id") or "") == claim_id:
                if row.get("event") in {"claim_status", "claim_outcome"}:
                    related.append(row)
                if row.get("event") == "claim_retrieved":
                    latest_retrieval = row
        return {
            "claim": claim,
            "effective_status": effective_status(claim),
            "evidence_ids": list(claim.get("evidence_ids") or []),
            "provenance": claim.get("provenance") or {},
            "supersedes": claim.get("supersedes") or "",
            "contradicted_by": list(claim.get("contradicted_by") or []),
            "latest_retrieval": latest_retrieval,
            "history": related[-20:],
        }

    def create_capsule(
        self,
        checkpoint: dict[str, Any],
        *,
        failed_paths: Iterable[dict[str, Any]] = (),
        discoveries: Iterable[dict[str, Any]] = (),
        evidence_ids: Iterable[str] = (),
        next_move: str = "",
        summary: str = "",
        scope: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not isinstance(checkpoint, dict):
            raise ValueError("checkpoint must be an object")
        identity = checkpoint.get("identity") if isinstance(checkpoint.get("identity"), dict) else {}
        task_id = str(identity.get("task_id") or checkpoint.get("task_id") or "").strip()
        if not task_id:
            raise ValueError("checkpoint identity.task_id is required")
        run_id_raw = identity.get("run_id", checkpoint.get("run_id"))
        try:
            run_id = int(run_id_raw) if run_id_raw is not None else None
        except (TypeError, ValueError):
            run_id = None
        verified = tuple(str(x) for x in checkpoint.get("verified_criteria") or ())
        failed = tuple(redact(dict(x)) for x in failed_paths if isinstance(x, dict))
        discovered = tuple(redact(dict(x)) for x in discoveries if isinstance(x, dict))
        evidence = tuple(dict.fromkeys(str(x).strip() for x in evidence_ids if str(x).strip()))
        unresolved = tuple(dict.fromkeys(
            [str(x) for x in checkpoint.get("frontier") or ()]
            + [str(x) for x in checkpoint.get("failed_criteria") or ()]
        ))
        artifacts = tuple(redact(dict(x)) for x in checkpoint.get("artifacts") or () if isinstance(x, dict))
        identity_data = {
            "task_id": task_id,
            "run_id": run_id,
            "contract_hash": str(identity.get("contract_hash") or checkpoint.get("contract_hash") or ""),
            "checkpoint_id": str(checkpoint.get("checkpoint_id") or ""),
            "created_at": utc_now(),
        }
        capsule = RunCapsule(
            capsule_id="ncap-" + canonical_hash(identity_data)[:24],
            task_id=task_id,
            run_id=run_id,
            contract_hash=identity_data["contract_hash"],
            verified_criteria=verified,
            failed_paths=failed,
            discoveries=discovered,
            evidence_ids=evidence,
            artifacts=artifacts,
            unresolved=unresolved,
            next_move=str(redact(next_move or "")),
            summary=str(redact(summary or checkpoint.get("reason") or "")),
            scope=_clean_scope(scope),
            source_checkpoint_id=identity_data["checkpoint_id"],
            decision_id=str(checkpoint.get("decision_id") or ""),
        )
        self._append("run_capsule", {"capsule": capsule.as_dict()})
        return capsule.as_dict()

    def capsules(self, *, task_id: str = "", scope: dict[str, Any] | None = None, limit: int = 8) -> list[dict[str, Any]]:
        wanted_scope = _clean_scope(scope)
        rows: list[dict[str, Any]] = []
        for row in reversed(self.rows()):
            if row.get("event") != "run_capsule" or not isinstance(row.get("capsule"), dict):
                continue
            capsule = dict(row["capsule"])
            if task_id and str(capsule.get("task_id") or "") != str(task_id):
                continue
            if scope_match_score(capsule.get("scope") or {}, wanted_scope) < 0:
                continue
            rows.append(capsule)
            if len(rows) >= max(0, min(50, int(limit or 0))):
                break
        return rows

    def record_recovery(
        self,
        *,
        fingerprint: str,
        recovery: str,
        successful: bool,
        evidence_ids: Iterable[str] = (),
        scope: dict[str, Any] | None = None,
        symptoms: Iterable[str] = (),
        tokens_saved: int = 0,
    ) -> dict[str, Any]:
        fp = str(fingerprint or "").strip()
        action = str(redact(recovery or "")).strip()
        if not fp or not action:
            raise ValueError("fingerprint and recovery are required")
        return self._append("recovery_observation", {
            "fingerprint": fp,
            "recovery": action,
            "successful": bool(successful),
            "evidence_ids": list(dict.fromkeys(str(x) for x in evidence_ids if str(x))),
            "scope": _clean_scope(scope),
            "symptoms": [str(redact(x)) for x in symptoms if str(x)],
            "tokens_saved": int(tokens_saved or 0),
        })

    def playbooks(self, *, min_successes: int = 2, min_rate: float = 0.67) -> list[dict[str, Any]]:
        groups: dict[tuple[str, str, str], dict[str, Any]] = {}
        for row in self.rows():
            if row.get("event") != "recovery_observation":
                continue
            scope = _clean_scope(row.get("scope") if isinstance(row.get("scope"), dict) else {})
            key = (str(row.get("fingerprint") or ""), str(row.get("recovery") or ""), canonical_hash(scope))
            item = groups.setdefault(key, {
                "fingerprint": key[0], "recovery": key[1], "scope": scope,
                "successes": 0, "failures": 0, "tokens_saved": 0, "evidence_ids": [], "symptoms": [],
            })
            if row.get("successful") is True:
                item["successes"] += 1
            else:
                item["failures"] += 1
            item["tokens_saved"] += int(row.get("tokens_saved") or 0)
            item["evidence_ids"].extend(row.get("evidence_ids") or [])
            item["symptoms"].extend(row.get("symptoms") or [])
        result = []
        for item in groups.values():
            total = item["successes"] + item["failures"]
            rate = item["successes"] / total if total else 0.0
            if item["successes"] < int(min_successes) or rate < float(min_rate):
                continue
            item["success_rate"] = round(rate, 6)
            item["confidence"] = round((item["successes"] + 1) / (total + 2), 6)
            item["playbook_id"] = "npb-" + canonical_hash({
                "fingerprint": item["fingerprint"], "recovery": item["recovery"], "scope": item["scope"]
            })[:24]
            item["evidence_ids"] = list(dict.fromkeys(str(x) for x in item["evidence_ids"] if str(x)))
            item["symptoms"] = list(dict.fromkeys(str(x) for x in item["symptoms"] if str(x)))
            result.append(item)
        result.sort(key=lambda x: (-x["confidence"], -x["tokens_saved"], x["playbook_id"]))
        return result

    def lookup_recovery(self, fingerprint: str, *, scope: dict[str, Any] | None = None, limit: int = 3) -> list[dict[str, Any]]:
        wanted = _clean_scope(scope)
        found = []
        for playbook in self.playbooks():
            if playbook["fingerprint"] != str(fingerprint):
                continue
            score = scope_match_score(playbook.get("scope") or {}, wanted)
            if score < 0:
                continue
            item = dict(playbook)
            item["why_retrieved"] = [
                "exact_failure_fingerprint",
                f"scope_match={score:.2f}",
                f"historical_success_rate={item['success_rate']:.2f}",
            ]
            found.append(item)
        return found[: max(0, min(20, int(limit or 0)))]

    def reconcile(
        self,
        *,
        outcome_report: dict[str, Any] | None = None,
        receipt_report: dict[str, Any] | None = None,
        now: str | None = None,
    ) -> dict[str, Any]:
        """Return review-required recommendations. Never writes policy or config."""
        claims = self.claims()
        now_dt = _parse_time(now) or datetime.now(timezone.utc)
        recommendations: list[dict[str, Any]] = []
        expired = [c["claim_id"] for c in claims.values() if effective_status(c, now_dt) == "expired"]
        conflicts = sorted(c["claim_id"] for c in claims.values() if c.get("status") == "contradicted")
        if expired:
            recommendations.append(_recommendation(
                "retire_stale_claims", "Review and retire temporally expired experience.", expired
            ))
        if conflicts:
            recommendations.append(_recommendation(
                "verify_conflicting_claims", "Re-verify contradictory high-confidence experience before reuse.", conflicts
            ))
        for playbook in self.playbooks():
            if playbook["successes"] >= 3 and playbook["confidence"] >= 0.75:
                recommendations.append(_recommendation(
                    "promote_recovery_playbook",
                    f"Prefer the proven local recovery for failure fingerprint {playbook['fingerprint']} before a Reflex call.",
                    [playbook["playbook_id"], *playbook.get("evidence_ids", [])],
                    details={"recovery": playbook["recovery"], "confidence": playbook["confidence"]},
                ))
        outcomes = outcome_report or {}
        metrics = outcomes.get("metrics") if isinstance(outcomes.get("metrics"), dict) else {}
        useless = int(metrics.get("useless_disagreements") or 0)
        useful = int(metrics.get("useful_disagreements") or 0)
        if useless >= 5 and useless > useful * 2:
            recommendations.append(_recommendation(
                "raise_provider_admission_threshold",
                "Provider-backed disagreements are more often useless than useful; review a higher local admission threshold.",
                ["outcome-report"],
                details={"useful_disagreements": useful, "useless_disagreements": useless},
            ))
        receipts = receipt_report or {}
        provider_calls = int(receipts.get("provider_calls") or 0)
        if provider_calls >= 10 and useful == 0 and useless > 0:
            recommendations.append(_recommendation(
                "audit_provider_calls",
                "Provider calls exist without observed useful disagreements; audit which calls can become local deterministic decisions.",
                ["receipt-report", "outcome-report"],
                details={"provider_calls": provider_calls},
            ))
        return {
            "schema": "hermes-nerve-reconciliation/v1",
            "generated_at": utc_now(),
            "review_required": True,
            "authority": "advisory-only",
            "mutated_policy": False,
            "claim_count": len(claims),
            "playbook_count": len(self.playbooks()),
            "recommendations": recommendations,
        }

    def evidence_graph(self, claim_id: str = "") -> dict[str, Any]:
        """Materialize a bounded claim/evidence/decision/outcome provenance graph."""
        claims = self.claims()
        wanted = str(claim_id or "").strip()
        if wanted:
            claims = {wanted: claims[wanted]} if wanted in claims else {}
        nodes: dict[str, dict[str, Any]] = {}
        edges: list[dict[str, str]] = []
        for cid, claim in claims.items():
            nodes[cid] = {"id": cid, "type": "claim", "claim_type": claim.get("claim_type"), "status": effective_status(claim)}
            for evidence_id in claim.get("evidence_ids") or []:
                eid = str(evidence_id)
                nodes.setdefault(eid, {"id": eid, "type": "evidence"})
                edges.append({"from": cid, "to": eid, "relation": "supported_by"})
            supersedes = str(claim.get("supersedes") or "")
            if supersedes:
                nodes.setdefault(supersedes, {"id": supersedes, "type": "claim"})
                edges.append({"from": cid, "to": supersedes, "relation": "supersedes"})
            for other in claim.get("contradicted_by") or []:
                oid = str(other)
                nodes.setdefault(oid, {"id": oid, "type": "claim"})
                edges.append({"from": cid, "to": oid, "relation": "contradicted_by"})
        for row in self.rows():
            cid = str(row.get("claim_id") or "")
            if row.get("event") != "claim_outcome" or cid not in claims:
                continue
            outcome_id = "nout-" + canonical_hash(row)[:24]
            nodes[outcome_id] = {
                "id": outcome_id,
                "type": "outcome",
                "successful": row.get("successful"),
                "decision_id": row.get("decision_id"),
                "receipt_id": row.get("receipt_id"),
            }
            edges.append({"from": cid, "to": outcome_id, "relation": "evaluated_by"})
            decision_id = str(row.get("decision_id") or "")
            if decision_id:
                nodes.setdefault(decision_id, {"id": decision_id, "type": "decision"})
                edges.append({"from": outcome_id, "to": decision_id, "relation": "attributed_to"})
            receipt_id = str(row.get("receipt_id") or "")
            if receipt_id:
                nodes.setdefault(receipt_id, {"id": receipt_id, "type": "receipt"})
                edges.append({"from": outcome_id, "to": receipt_id, "relation": "receipt"})
        return {
            "schema": "hermes-nerve-experience-graph/v1",
            "nodes": list(nodes.values())[:256],
            "edges": edges[:512],
            "bounded": len(nodes) > 256 or len(edges) > 512,
        }

    def hierarchical_summary(self, *, scope: dict[str, Any] | None = None, limit: int = 8) -> dict[str, Any]:
        """Summarize verified experience at run, task, project, and global levels."""
        clean_scope = _clean_scope(scope)
        claims = []
        for claim in self.claims().values():
            if effective_status(claim) not in {"active", "contradicted"}:
                continue
            match = scope_match_score(claim.get("scope") or {}, clean_scope)
            if match < 0:
                continue
            utility = utility_score(claim, scope_score=match)
            claims.append((utility["score"], claim))
        claims.sort(key=lambda x: (-x[0], str(x[1].get("claim_id") or "")))
        top = [
            {
                "claim_id": claim["claim_id"],
                "subject": claim["subject"],
                "statement": claim["statement"],
                "claim_type": claim["claim_type"],
                "status": effective_status(claim),
                "utility": round(score, 6),
            }
            for score, claim in claims[: max(1, min(50, int(limit or 8)))]
        ]
        capsules = self.capsules(scope=clean_scope, limit=max(1, min(50, int(limit or 8))))
        by_task: dict[str, dict[str, Any]] = {}
        for capsule in capsules:
            task_id = str(capsule.get("task_id") or "")
            entry = by_task.setdefault(task_id, {"task_id": task_id, "runs": 0, "verified_criteria": [], "unresolved": [], "latest_next_move": ""})
            entry["runs"] += 1
            entry["verified_criteria"].extend(capsule.get("verified_criteria") or [])
            entry["unresolved"].extend(capsule.get("unresolved") or [])
            if not entry["latest_next_move"]:
                entry["latest_next_move"] = str(capsule.get("next_move") or "")
        for entry in by_task.values():
            entry["verified_criteria"] = list(dict.fromkeys(entry["verified_criteria"]))
            entry["unresolved"] = list(dict.fromkeys(entry["unresolved"]))
        projects: dict[str, int] = {}
        for _, claim in claims:
            repo = str((claim.get("scope") or {}).get("repository") or "global")
            projects[repo] = projects.get(repo, 0) + 1
        return {
            "schema": "hermes-nerve-experience-summary/v1",
            "scope": clean_scope,
            "run": capsules,
            "task": list(by_task.values()),
            "project": [{"repository": key, "claims": value} for key, value in sorted(projects.items())],
            "global_lessons": top,
        }

    def reconcile_runtime(self) -> dict[str, Any]:
        """Offline reconciliation over existing local receipt/outcome ledgers."""
        from . import receipts
        from .outcomes import OutcomeStore
        return self.reconcile(
            outcome_report=OutcomeStore().report(),
            receipt_report=receipts.report(recent_limit=0),
        )

    def report(self) -> dict[str, Any]:
        claims = self.claims()
        statuses: dict[str, int] = {}
        types: dict[str, int] = {}
        for claim in claims.values():
            status = effective_status(claim)
            statuses[status] = statuses.get(status, 0) + 1
            kind = str(claim.get("claim_type") or "unknown")
            types[kind] = types.get(kind, 0) + 1
        return {
            "path": str(self.path),
            "rows": len(self.rows()),
            "claims": len(claims),
            "by_status": dict(sorted(statuses.items())),
            "by_type": dict(sorted(types.items())),
            "capsules": len(self.capsules(limit=50)),
            "playbooks": len(self.playbooks()),
        }


def experience_path() -> Path:
    explicit = str(os.getenv("HERMES_NERVE_EXPERIENCE") or "").strip()
    return Path(explicit).expanduser() if explicit else hermes_home() / "nerve" / "experience.jsonl"


def default_store() -> ExperienceStore:
    return ExperienceStore()


def effective_status(claim: dict[str, Any], now: datetime | None = None) -> str:
    status = str(claim.get("status") or "active")
    if status in {"superseded", "contradicted"}:
        return status
    now_dt = now or datetime.now(timezone.utc)
    valid_until = _parse_time(str(claim.get("valid_until") or ""))
    if valid_until and valid_until < now_dt:
        return "expired"
    return status if status in CLAIM_STATUSES else "active"


def scopes_overlap(left: dict[str, Any], right: dict[str, Any]) -> bool:
    a, b = _clean_scope(left), _clean_scope(right)
    for key in set(a) & set(b):
        if a[key] != b[key]:
            return False
    return True


def scope_match_score(claim_scope: dict[str, Any], query_scope: dict[str, Any]) -> float:
    claim = _clean_scope(claim_scope)
    query = _clean_scope(query_scope)
    if not claim:
        return 0.55
    matched = 0
    considered = 0
    for key, value in claim.items():
        if key not in query:
            continue
        considered += 1
        if query[key] != value:
            return -1.0
        matched += 1
    if not query:
        return 0.60 if not claim else 0.45
    exact = matched / max(1, len(query))
    specificity = matched / max(1, len(claim))
    return round(0.65 * exact + 0.35 * specificity, 6)


def admission_decision(
    *,
    claim_type: str,
    confidence: float,
    evidence_count: int,
    estimated_tokens: int,
    expected_reuse: float,
    threshold: float = 0.34,
) -> dict[str, Any]:
    verification = {"verified": 1.0, "disproven": 1.0, "observed": 0.72, "inferred": 0.45}.get(claim_type, 0.0)
    evidence = min(1.0, max(0, int(evidence_count)) / 3.0)
    reuse = _clamp(expected_reuse)
    cost_penalty = min(0.45, max(0, int(estimated_tokens)) / 4000.0)
    score = 0.42 * verification + 0.28 * _clamp(confidence) + 0.18 * evidence + 0.12 * reuse - cost_penalty
    admitted = score >= float(threshold)
    reasons = [
        f"verification={verification:.2f}",
        f"confidence={_clamp(confidence):.2f}",
        f"evidence={evidence:.2f}",
        f"expected_reuse={reuse:.2f}",
        f"token_cost_penalty={cost_penalty:.2f}",
    ]
    return {"admitted": admitted, "score": round(score, 6), "threshold": float(threshold), "reasons": reasons}


def utility_score(
    claim: dict[str, Any],
    *,
    scope_score: float,
    now: datetime | None = None,
    max_tokens: int = 1200,
) -> dict[str, Any]:
    kind = str(claim.get("claim_type") or "inferred")
    verification = {"verified": 1.0, "disproven": 0.95, "observed": 0.72, "inferred": 0.45}.get(kind, 0.35)
    confidence = _clamp(claim.get("confidence"))
    created = _parse_time(str(claim.get("last_verified") or claim.get("created_at") or ""))
    now_dt = now or datetime.now(timezone.utc)
    age_days = max(0.0, (now_dt - created).total_seconds() / 86400.0) if created else 365.0
    recency = math.exp(-age_days / 90.0)
    good = int(claim.get("useful_outcomes") or 0)
    bad = int(claim.get("harmful_outcomes") or 0)
    usefulness = (good + 1.0) / (good + bad + 2.0)
    estimated = max(0, int(claim.get("estimated_tokens") or 0))
    token_penalty = min(0.35, estimated / max(1.0, float(max_tokens) * 4.0))
    score = 0.28 * verification + 0.22 * confidence + 0.20 * max(0.0, scope_score) + 0.15 * recency + 0.15 * usefulness - token_penalty
    components = {
        "verification": round(verification, 6),
        "confidence": round(confidence, 6),
        "scope_match": round(max(0.0, scope_score), 6),
        "recency": round(recency, 6),
        "past_usefulness": round(usefulness, 6),
        "token_penalty": round(token_penalty, 6),
    }
    reasons = [
        f"{kind}_experience",
        f"confidence={confidence:.2f}",
        f"scope_match={max(0.0, scope_score):.2f}",
        f"recency={recency:.2f}",
        f"past_usefulness={usefulness:.2f}",
    ]
    if token_penalty:
        reasons.append(f"token_penalty={token_penalty:.2f}")
    return {"score": round(max(0.0, min(1.0, score)), 6), "components": components, "reasons": reasons}


def _recommendation(kind: str, suggestion: str, evidence_refs: Iterable[str], *, details: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "recommendation_id": "nrec-" + canonical_hash([kind, suggestion, list(evidence_refs)])[:24],
        "kind": kind,
        "suggestion": suggestion,
        "evidence_refs": list(dict.fromkeys(str(x) for x in evidence_refs if str(x))),
        "details": redact(details or {}),
        "review_required": True,
        "applies_automatically": False,
    }