"""Asynchronous Jev nervous system for Hermes Agent.

Public seam: ``NervousSystem``. Hermes continues working while this module
observes structured events, admits turns OFF/WATCH/ON, calls Jev only when the
local relevance router says another judgment can matter, and returns only
high-confidence disagreements through a challenge queue.

v0.2.1 closes the control-loop gap exposed by a real repeated-failure run:
remote REPLAN/GATHER_EVIDENCE decisions now acquire an attributable local
control lease at the pre-tool execution seam, identical failures are
fingerprinted/deduplicated, and a local loop breaker prevents an exact failing
action from running indefinitely even if remote supervision arrives late.
"""

from __future__ import annotations

import contextvars
import json
import os
import queue
import threading
import time
import uuid
from collections import Counter, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .engine import DecisionEngine
from .jsonl import append_jsonl
from .outcomes import HistoricalOutcomeModel, OutcomeStore
from .paths import hermes_home
from .privacy import canonical_hash, redact
from .provenance import execution_provenance
from .router import (
    assess_event,
    decision_state_fingerprint,
    failure_fingerprint,
    infer_tool_risk,
    tool_action_fingerprint,
)

ADMISSION_CHOICES = ["OFF", "WATCH", "ON"]
CONTROL_CHOICES = ["CONTINUE", "GATHER_EVIDENCE", "RETRY", "REPLAN", "ESCALATE"]
SUPERVISION_MODES = {"shadow", "correct_next", "precommit"}
BLOCKING_CONTROLS = {"GATHER_EVIDENCE", "REPLAN", "ESCALATE"}

_TRANSIENT_FAILURE_MARKERS = (
    "timeout", "timed out", "connection reset", "connection refused", "connection failed",
    "temporarily unavailable", "temporary failure", "rate limit", "too many requests",
    "service unavailable", "resource busy", "database is locked",
)
_DETERMINISTIC_FAILURE_MARKERS = (
    "blocked:", "not allowed", "permission denied", "operation not permitted", "access denied",
    "approval required", "requires approval", "not authorized", "unauthorized",
    "invalid argument", "invalid option", "unrecognized argument", "unknown option",
    "missing required", "validation error", "schema validation", "file not found",
    "no such file or directory", "does not exist", "read-only file system",
)


def _deterministic_failure_reason(status: str, result: str, error_message: str) -> str:
    """Return a stable reason when retrying the exact action cannot fix the failure."""
    normalized_status = str(status or "").strip().lower()
    text = " ".join((str(result or ""), str(error_message or ""))).lower()
    if normalized_status == "blocked":
        return "blocked-status"
    if any(marker in text for marker in _TRANSIENT_FAILURE_MARKERS):
        return ""
    for marker in _DETERMINISTIC_FAILURE_MARKERS:
        if marker in text:
            return marker
    return ""


@dataclass
class NervousConfig:
    enabled: bool = True
    admission_enabled: bool = True
    mode: str = "correct_next"
    challenge_confidence: float = 0.86
    call_threshold: float = 0.58
    max_provider_calls_per_turn: int = 96
    event_preview_chars: int = 1200
    retain_recent_events: int = 64
    emit_prompt_hint: bool = False
    local_learning: bool = True
    local_learning_min_samples: int = 8
    repeated_failure_local_replan_at: int = 3


@dataclass
class Challenge:
    challenge_id: str
    decision_id: str
    turn_id: str
    event_id: str
    state_version: str
    decision_version: str
    hermes_decision: str
    jev_decision: str
    confidence: float
    probabilities: dict[str, float]
    reason: str
    created_at: str
    stale: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "challenge_id": self.challenge_id,
            "decision_id": self.decision_id,
            "turn_id": self.turn_id,
            "event_id": self.event_id,
            "state_version": self.state_version,
            "decision_version": self.decision_version,
            "hermes_decision": self.hermes_decision,
            "jev_decision": self.jev_decision,
            "confidence": round(self.confidence, 6),
            "probabilities": self.probabilities,
            "reason": self.reason,
            "created_at": self.created_at,
            "stale": self.stale,
        }


@dataclass
class ControlDirective:
    decision_id: str
    event_id: str
    control: str
    confidence: float
    source: str
    reason: str
    created_at: str
    failure_fingerprint: str = ""
    action_fingerprint: str = ""
    state_version: str = ""
    delivered: bool = False
    attempted_overrides: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "decision_id": self.decision_id,
            "event_id": self.event_id,
            "control": self.control,
            "confidence": round(float(self.confidence), 6),
            "source": self.source,
            "reason": self.reason,
            "created_at": self.created_at,
            "failure_fingerprint": self.failure_fingerprint,
            "action_fingerprint": self.action_fingerprint,
            "state_version": self.state_version,
            "delivered": self.delivered,
            "attempted_overrides": self.attempted_overrides,
        }


@dataclass
class FailureEpisode:
    fingerprint: str
    action_fingerprint: str
    count: int = 0
    first_event_id: str = ""
    last_event_id: str = ""
    last_control: str = ""
    last_decision_id: str = ""
    provider_evaluations: int = 0
    blocked_attempts: int = 0


@dataclass
class TurnState:
    turn_id: str
    session_id: str
    user_message_hash: str
    admission: str = "PENDING"
    admission_confidence: float = 0.0
    admission_probabilities: dict[str, float] = field(default_factory=dict)
    started_at: float = field(default_factory=time.monotonic)
    finished: bool = False
    events_seen: int = 0
    events_suppressed: int = 0
    events_forwarded: int = 0
    provider_calls: int = 0
    provider_errors: int = 0
    watch_promotions: int = 0
    challenge_count: int = 0
    last_remote_fingerprint: str = ""
    last_state_version: str = ""
    last_decision_version: str = ""
    assessment_inflight: bool = False
    recent_events: deque = field(default_factory=lambda: deque(maxlen=64))
    pending_challenges: deque = field(default_factory=deque)
    pending_batch: deque = field(default_factory=lambda: deque(maxlen=64))
    failure_episodes: dict[str, FailureEpisode] = field(default_factory=dict)
    active_control: ControlDirective | None = None


class NervousSystem:
    def __init__(self, *, engine_factory: Callable[[], DecisionEngine] = DecisionEngine) -> None:
        self._engine_factory = engine_factory
        self._config = NervousConfig()
        self._lock = threading.RLock()
        self._turns: dict[str, TurnState] = {}
        self._session_turn: dict[str, str] = {}
        self._tasks: queue.Queue[tuple[str, dict[str, Any], contextvars.Context]] = queue.Queue()
        self._worker: threading.Thread | None = None
        self._stopping = False
        self._metrics: Counter[str] = Counter()
        self._recent_router: deque[dict[str, Any]] = deque(maxlen=50)
        self._recent_challenges: deque[dict[str, Any]] = deque(maxlen=30)
        self._recent_controls: deque[dict[str, Any]] = deque(maxlen=30)
        self._outcomes = OutcomeStore()
        self._historical_model = HistoricalOutcomeModel(self._outcomes, self._config.local_learning_min_samples)

    def configure(self, **kwargs: Any) -> None:
        with self._lock:
            cfg = self._config
            if "enabled" in kwargs:
                cfg.enabled = bool(kwargs["enabled"])
            if "admission_enabled" in kwargs:
                cfg.admission_enabled = bool(kwargs["admission_enabled"])
            if "mode" in kwargs:
                mode = str(kwargs["mode"] or "").strip().lower()
                cfg.mode = mode if mode in SUPERVISION_MODES else "correct_next"
            for key in ("challenge_confidence", "call_threshold"):
                if key in kwargs:
                    try:
                        setattr(cfg, key, min(1.0, max(0.0, float(kwargs[key]))))
                    except (TypeError, ValueError):
                        pass
            if "max_provider_calls_per_turn" in kwargs:
                try:
                    cfg.max_provider_calls_per_turn = max(1, min(1000, int(kwargs["max_provider_calls_per_turn"])))
                except (TypeError, ValueError):
                    pass
            if "event_preview_chars" in kwargs:
                try:
                    cfg.event_preview_chars = max(160, min(8000, int(kwargs["event_preview_chars"])))
                except (TypeError, ValueError):
                    pass
            if "retain_recent_events" in kwargs:
                try:
                    cfg.retain_recent_events = max(8, min(512, int(kwargs["retain_recent_events"])))
                except (TypeError, ValueError):
                    pass
            if "emit_prompt_hint" in kwargs:
                cfg.emit_prompt_hint = bool(kwargs["emit_prompt_hint"])
            if "local_learning" in kwargs:
                cfg.local_learning = bool(kwargs["local_learning"])
            if "local_learning_min_samples" in kwargs:
                try:
                    cfg.local_learning_min_samples = max(3, min(1000, int(kwargs["local_learning_min_samples"])))
                except (TypeError, ValueError):
                    pass
            if "repeated_failure_local_replan_at" in kwargs:
                try:
                    cfg.repeated_failure_local_replan_at = max(2, min(20, int(kwargs["repeated_failure_local_replan_at"])))
                except (TypeError, ValueError):
                    pass
            self._historical_model = HistoricalOutcomeModel(self._outcomes, cfg.local_learning_min_samples)
        if self._config.enabled:
            self._ensure_worker()
        else:
            self._stop_worker()

    def _ensure_worker(self) -> None:
        with self._lock:
            if self._worker and self._worker.is_alive():
                self._stopping = False
                return
            self._stopping = False
            self._worker = threading.Thread(target=self._run, name="hermes-nerve-nervous", daemon=True)
            self._worker.start()

    def _stop_worker(self) -> None:
        with self._lock:
            self._stopping = True
            worker = self._worker
        if worker and worker is not threading.current_thread():
            worker.join(timeout=0.5)
        with self._lock:
            if self._worker is worker and (worker is None or not worker.is_alive()):
                self._worker = None

    def _dispatch_task(self, kind: str, payload: dict[str, Any]) -> None:
        if kind == "admit":
            self._do_admission(payload)
        elif kind == "assess":
            self._do_assessment(payload)

    def _run(self) -> None:
        while not self._stopping:
            try:
                kind, payload, task_ctx = self._tasks.get(timeout=0.25)
            except queue.Empty:
                continue
            try:
                # Run under the caller's captured context so profile-scoped
                # secrets (Hermes ContextVars) survive the thread hop (#35).
                # Captured per task, NOT per worker: one worker serves many
                # profiles, so a worker-lifetime capture would run later
                # profiles under the first profile's scope.
                task_ctx.run(self._dispatch_task, kind, payload)
            except Exception as exc:  # background supervision must never crash Hermes
                self._metrics["worker_errors"] += 1
                self._log("worker_error", {"kind": kind, "error": str(exc)[:500]})
            finally:
                self._tasks.task_done()

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def _log_path(self) -> Path:
        explicit = os.getenv("HERMES_NERVE_NERVOUS_EVENTS", "").strip()
        return Path(explicit).expanduser() if explicit else hermes_home() / "nerve" / "nervous-events.jsonl"

    def _log(self, kind: str, payload: dict[str, Any]) -> None:
        record = {"schema": "hermes-nerve-nervous/v2", "timestamp": self._now(), "kind": kind, **redact(payload)}
        path = self._log_path()
        try:
            append_jsonl(path, record)
        except Exception:
            self._metrics["log_errors"] += 1

    def _resolve_turn(self, *, turn_id: str = "", session_id: str = "") -> TurnState | None:
        with self._lock:
            if turn_id and turn_id in self._turns:
                return self._turns[turn_id]
            if session_id:
                mapped = self._session_turn.get(session_id)
                if mapped:
                    return self._turns.get(mapped)
        return None

    def start_turn(self, *, user_message: str, session_id: str = "", turn_id: str = "") -> str:
        if not self._config.enabled:
            return turn_id or ""
        self._ensure_worker()
        tid = str(turn_id or uuid.uuid4().hex)
        with self._lock:
            state = TurnState(
                turn_id=tid,
                session_id=str(session_id or ""),
                user_message_hash=canonical_hash(user_message),
                recent_events=deque(maxlen=self._config.retain_recent_events),
                pending_batch=deque(maxlen=self._config.retain_recent_events),
            )
            self._turns[tid] = state
            if session_id:
                self._session_turn[str(session_id)] = tid
            self._metrics["turns_started"] += 1
        self._log("turn_start", {"turn_id": tid, "session_id": session_id, "prompt_sha256": state.user_message_hash})
        if self._config.admission_enabled:
            self._tasks.put(("admit", {"turn_id": tid, "user_message": user_message}, contextvars.copy_context()))
        else:
            with self._lock:
                state.admission = "ON"
                self._metrics["admission_on"] += 1
        return tid

    def _do_admission(self, payload: dict[str, Any]) -> None:
        tid = str(payload.get("turn_id") or "")
        with self._lock:
            state = self._turns.get(tid)
            if state is None or state.finished:
                return
        prompt = str(payload.get("user_message") or "")
        engine = self._engine_factory()
        result = engine.decide(
            state={"user_prompt": prompt[:12000]},
            instructions=(
                "Classify whether this Hermes turn contains or is likely to develop an accountable decision plane. "
                "OFF: ordinary conversation, explanation, pure creative/fiction/roleplay, or output where no material "
                "trajectory decision is expected. WATCH: ambiguous research/advisory work that may become execution or "
                "may develop material choices. ON: autonomous work, repair, implementation, consequential operation, "
                "resource routing, recovery, prioritization, or completion judgment is likely."
            ),
            choices=ADMISSION_CHOICES,
            criteria={
                "OFF": "No material accountable decision is likely before this turn ends.",
                "WATCH": "A material decision may emerge; observe locally and wake supervision if it does.",
                "ON": "Material accountable decisions are expected during this turn.",
            },
            contract="hermes/nerve-turn-admission/v1",
        )
        with self._lock:
            state = self._turns.get(tid)
            if state is None or state.finished:
                return
            state.admission = result.value
            state.admission_confidence = result.confidence
            state.admission_probabilities = dict(result.probabilities)
            state.provider_calls += 1
            self._metrics["provider_calls"] += 1
            self._metrics["provider_calls_origin_turn_admission"] += 1
            self._metrics["admission_provider_calls"] += 1
            self._metrics[f"admission_{result.value.lower()}"] += 1
        self._log("turn_admission", {
            "turn_id": tid,
            "admission": result.value,
            "confidence": result.confidence,
            "probabilities": result.probabilities,
            "request_id": result.request_id,
            "latency_ms": result.latency_ms,
        })

    @staticmethod
    def _lease_invalidation_reason(event: dict[str, Any]) -> str:
        et = str(event.get("type") or "").upper()
        if bool(event.get("contradiction")):
            return "contradiction"
        if bool(event.get("strategy_changed")) or et == "STRATEGY_CHANGE":
            return "strategy-change"
        if et in {"COMPLETION", "COMPLETION_CANDIDATE"}:
            return "completion-boundary"
        if et in {"FAILURE", "REPEATED_FAILURE"}:
            return "failure-state-change"
        if et in {"CONSEQUENTIAL_ACTION", "IRREVERSIBLE_ACTION"}:
            return "consequence-boundary"
        return "material-state-change"

    def _record_control_lifecycle(
        self,
        state: TurnState,
        directive: ControlDirective,
        *,
        stage: str,
        disposition: str = "",
        proposed_action_fingerprint: str = "",
        attempted_override: bool = False,
    ) -> None:
        row = {
            "record_type": "control_lifecycle",
            "decision_id": directive.decision_id,
            "event_id": directive.event_id,
            "turn_id": state.turn_id,
            "stage": stage,
            "control": directive.control,
            "source": directive.source,
            "challenge_disposition": disposition,
            "proposed_action_fingerprint": proposed_action_fingerprint,
            "controlled_action_fingerprint": directive.action_fingerprint,
            "failure_fingerprint": directive.failure_fingerprint,
            "attempted_override": attempted_override,
            "timestamp": self._now(),
        }
        self._outcomes.append(row)
        self._log("control_lifecycle", row)

    def _activate_control(self, state: TurnState, directive: ControlDirective) -> None:
        if self._config.mode == "shadow":
            self._metrics["controls_shadow_only"] += 1
            self._record_control_lifecycle(state, directive, stage="created", disposition="shadow")
            return
        previous = state.active_control
        if previous and previous.decision_id != directive.decision_id:
            self._metrics["controls_superseded"] += 1
            self._record_control_lifecycle(state, previous, stage="expired", disposition="superseded")
        state.active_control = directive
        self._metrics["controls_activated"] += 1
        self._metrics[f"control_source_{directive.source}"] += 1
        self._recent_controls.append(directive.as_dict())
        self._record_control_lifecycle(state, directive, stage="created", disposition="active")

    def _activate_local_replan(
        self,
        state: TurnState,
        episode: FailureEpisode,
        *,
        source: str,
        reason: str,
        decision_type: str,
        challenge_reason: str,
        metric: str,
    ) -> None:
        if state.active_control and state.active_control.failure_fingerprint == episode.fingerprint:
            return
        decision_id = f"local-{uuid.uuid4().hex}"
        directive = ControlDirective(
            decision_id=decision_id,
            event_id=episode.last_event_id,
            control="REPLAN",
            confidence=1.0,
            source=source,
            reason=reason,
            created_at=self._now(),
            failure_fingerprint=episode.fingerprint,
            action_fingerprint=episode.action_fingerprint,
            state_version=state.last_state_version,
        )
        episode.last_control = "REPLAN"
        episode.last_decision_id = decision_id
        self._metrics[metric] += 1
        self._metrics["control_replan"] += 1
        self._outcomes.append({
            "record_type": "decision",
            "decision_id": decision_id,
            "turn_id": state.turn_id,
            "event_id": episode.last_event_id,
            "decision_type": decision_type,
            "hermes_decision": "RETRY_SAME_ACTION",
            "jev_decision": "REPLAN",
            "agreement": False,
            "jev_confidence": 1.0,
            "jev_probabilities": {"REPLAN": 1.0},
            "challenge_issued": True,
            "source": source,
            "reason": reason,
            "failure_fingerprint": episode.fingerprint,
            "failure_repeat_count": episode.count,
            "stale": False,
        })
        self._activate_control(state, directive)
        challenge = Challenge(
            challenge_id=uuid.uuid4().hex,
            decision_id=decision_id,
            turn_id=state.turn_id,
            event_id=episode.last_event_id,
            state_version=state.last_state_version,
            decision_version=decision_id,
            hermes_decision="RETRY_SAME_ACTION",
            jev_decision="REPLAN",
            confidence=1.0,
            probabilities={"REPLAN": 1.0},
            reason=challenge_reason,
            created_at=self._now(),
        )
        state.pending_challenges.append(challenge)
        state.challenge_count += 1
        self._metrics["challenges_created"] += 1
        self._recent_challenges.append(challenge.as_dict())
        self._log(source.replace("-", "_"), {**directive.as_dict(), "repeat_count": episode.count})

    def _activate_local_loop_breaker(self, state: TurnState, episode: FailureEpisode) -> None:
        self._activate_local_replan(
            state,
            episode,
            source="local-loop-breaker",
            reason=f"identical failure repeated {episode.count} times",
            decision_type="REPEATED_FAILURE",
            challenge_reason="local-repeated-failure-loop-breaker",
            metric="local_loop_breakers",
        )

    def _activate_deterministic_failure_replan(
        self, state: TurnState, episode: FailureEpisode, deterministic_reason: str
    ) -> None:
        self._activate_local_replan(
            state,
            episode,
            source="local-deterministic-failure",
            reason=f"exact retry cannot fix deterministic failure: {deterministic_reason}",
            decision_type="DETERMINISTIC_FAILURE",
            challenge_reason=f"local-deterministic-failure:{deterministic_reason}",
            metric="deterministic_failure_replans",
        )

    def emit_event(self, event: dict[str, Any]) -> dict[str, Any]:
        if not self._config.enabled:
            return {"accepted": False, "reason": "nervous-disabled"}
        clean = redact(dict(event or {}))
        turn_id = str(clean.get("turn_id") or "")
        session_id = str(clean.get("session_id") or "")
        state = self._resolve_turn(turn_id=turn_id, session_id=session_id)
        if state is None:
            return {"accepted": False, "reason": "no-active-turn"}

        clean.setdefault("event_id", uuid.uuid4().hex)
        clean.setdefault("turn_id", state.turn_id)
        clean.setdefault("session_id", state.session_id)
        clean["type"] = str(clean.get("type") or "OBSERVATION").upper()
        clean.setdefault("origin_event_type", clean["type"])
        clean.setdefault("origin_event_id", clean["event_id"])
        origin_tool = str(clean.get("origin_tool") or "").strip().lower()
        if origin_tool.startswith(("nerve_", "jev_")):
            clean["suppress_remote"] = True
            clean["suppress_reason"] = "nerve-internal"
            with self._lock:
                self._metrics["nerve_internal_seen"] += 1
                self._metrics["nerve_internal_suppressed"] += 1
        if clean["type"] in {"OUTCOME", "CHALLENGE_OUTCOME"}:
            self._outcomes.append({"record_type": "outcome", **clean})
            self._metrics["outcomes_recorded"] += 1
            self._log("outcome", clean)
            return {"accepted": True, "forwarded": False, "reason": "outcome-recorded"}
        state_version = str(clean.get("state_version") or state.events_seen + 1)
        decision_version = str(clean.get("decision_version") or clean.get("event_id"))
        clean["state_version"] = state_version
        clean["decision_version"] = decision_version

        with self._lock:
            state.events_seen += 1
            state.last_state_version = state_version
            state.last_decision_version = decision_version
            state.recent_events.append(clean)
            self._metrics["events_seen"] += 1

        route = assess_event(
            clean,
            last_remote_fingerprint=state.last_remote_fingerprint,
            call_threshold=self._config.call_threshold,
        )
        learned_probability = None
        learned_samples = 0
        if self._config.local_learning:
            learned_probability, learned_samples = self._historical_model.predict(clean)
            if learned_probability is not None and not route.critical:
                learned_threshold = self._config.call_threshold
                if learned_probability >= 0.60:
                    learned_threshold = max(0.20, learned_threshold - 0.12)
                elif learned_probability <= 0.15:
                    learned_threshold = min(0.95, learned_threshold + 0.12)
                from .router import RouteAssessment
                route = RouteAssessment(
                    worth_calling=route.score >= learned_threshold,
                    score=route.score,
                    reasons=tuple(list(route.reasons) + [f"historical-p={learned_probability:.3f}", f"historical-n={learned_samples}"]),
                    critical=route.critical,
                )
        semantic_fp = decision_state_fingerprint(clean)
        router_row = {
            "turn_id": state.turn_id,
            "event_id": clean["event_id"],
            "type": clean["type"],
            "decision_state_fingerprint": semantic_fp,
            **route.as_dict(),
            "historical_useful_disagreement_probability": learned_probability,
            "historical_samples": learned_samples,
        }
        with self._lock:
            self._recent_router.append(router_row)
            if "semantic-hysteresis" in route.reasons:
                self._metrics["decision_lease_reuse"] += 1
            elif state.last_remote_fingerprint and route.worth_calling and semantic_fp != state.last_remote_fingerprint:
                reason = self._lease_invalidation_reason(clean)
                self._metrics["decision_lease_invalidations"] += 1
                self._metrics[f"decision_lease_invalidation_{reason}"] += 1
            if clean["type"] in {"DECISION", "STRATEGY", "ROUTING", "RECOVERY", "COMPLETION", "COMPLETION_CANDIDATE", "COMMIT", "PRIORITIZATION", "HIGH_MATERIALITY_DECISION"}:
                self._metrics["meaningful_decisions"] += 1
        self._log("router", router_row)

        with self._lock:
            admission = state.admission
            if admission == "OFF":
                state.events_suppressed += 1
                self._metrics["events_suppressed"] += 1
                return {"accepted": True, "forwarded": False, "admission": admission, "router": route.as_dict()}
            if admission == "PENDING" and not route.critical:
                state.events_suppressed += 1
                self._metrics["events_suppressed"] += 1
                return {"accepted": True, "forwarded": False, "admission": admission, "router": route.as_dict()}
            if admission == "WATCH" and route.worth_calling:
                state.admission = "ON"
                state.watch_promotions += 1
                self._metrics["watch_promotions"] += 1
                admission = "ON"
                self._log("watch_promoted", {"turn_id": state.turn_id, "event_id": clean["event_id"], "reasons": route.reasons})
            if admission not in {"ON", "PENDING"} or not route.worth_calling:
                state.events_suppressed += 1
                self._metrics["events_suppressed"] += 1
                if "repeated-failure-dedup" in route.reasons:
                    self._metrics["repeated_failure_provider_calls_avoided"] += 1
                return {"accepted": True, "forwarded": False, "admission": admission, "router": route.as_dict()}
            if state.assessment_inflight:
                state.pending_batch.append({
                    "type": clean.get("type"), "event_id": clean.get("event_id"),
                    "goal": clean.get("goal"), "strategy": clean.get("strategy"),
                    "hypothesis": clean.get("hypothesis"), "materiality": clean.get("materiality"),
                    "uncertainty": clean.get("uncertainty"), "novelty": clean.get("novelty"),
                    "consequence": clean.get("consequence"), "risk": clean.get("risk"),
                    "contradiction": clean.get("contradiction"), "status": clean.get("status"),
                    "tool_name": clean.get("tool_name"), "state_version": clean.get("state_version"),
                    "failure_fingerprint": clean.get("failure_fingerprint"),
                    "action_fingerprint": clean.get("action_fingerprint"),
                    "origin_tool": clean.get("origin_tool"),
                    "origin_event_type": clean.get("origin_event_type"),
                    "origin_event_id": clean.get("origin_event_id"),
                    "origin_failure_fingerprint": clean.get("origin_failure_fingerprint"),
                })
                self._metrics["events_batched"] += 1
                return {"accepted": True, "forwarded": False, "reason": "batched-behind-inflight", "router": route.as_dict()}
            if state.provider_calls >= self._config.max_provider_calls_per_turn:
                state.events_suppressed += 1
                self._metrics["budget_suppressed"] += 1
                return {"accepted": True, "forwarded": False, "reason": "provider-budget", "router": route.as_dict()}
            state.assessment_inflight = True
            state.events_forwarded += 1
            self._metrics["events_forwarded"] += 1
            if clean.get("failure_fingerprint"):
                episode = state.failure_episodes.get(str(clean.get("failure_fingerprint")))
                if episode:
                    episode.provider_evaluations += 1

        self._tasks.put(("assess", {"turn_id": state.turn_id, "event": clean, "router": route.as_dict()}, contextvars.copy_context()))
        return {"accepted": True, "forwarded": True, "admission": admission, "router": route.as_dict()}

    def observe_tool_call(self, **kwargs: Any) -> dict[str, Any]:
        tool_name = str(kwargs.get("tool_name") or "")
        status = str(kwargs.get("status") or "")
        args = kwargs.get("args") if isinstance(kwargs.get("args"), dict) else {}
        if tool_name.strip().lower().startswith(("nerve_", "jev_")):
            event_id = str(kwargs.get("tool_call_id") or uuid.uuid4().hex)
            with self._lock:
                self._metrics["nerve_internal_seen"] += 1
                self._metrics["nerve_internal_suppressed"] += 1
            self._log("jev_internal_tool_observation", {
                "event_id": event_id,
                "turn_id": kwargs.get("turn_id"),
                "session_id": kwargs.get("session_id"),
                "origin_tool": tool_name,
                "origin_event_type": "FAILURE" if status.lower() in {"error", "failed", "blocked", "failure"} else "TOOL_RESULT",
                "status": status,
                "reason": "nerve-internal",
            })
            return {
                "accepted": True,
                "forwarded": False,
                "reason": "nerve-internal",
                "origin_tool": tool_name,
                "origin_event_id": event_id,
            }
        risk, risk_reasons = infer_tool_risk(tool_name, status)
        result = str(kwargs.get("result") or "")
        error_message = str(kwargs.get("error_message") or "")
        is_failure = status.lower() in {"error", "failed", "blocked", "failure"} or bool(error_message)
        contradiction = any(token in (result + " " + error_message).lower() for token in (
            "unexpected", "contradict", "mismatch", "failed", "failure", "error", "not found", "denied"
        ))
        event_type = "FAILURE" if is_failure else "TOOL_RESULT"
        event_id = str(kwargs.get("tool_call_id") or uuid.uuid4().hex)
        action_fp = tool_action_fingerprint(tool_name, args)
        fail_fp = ""
        repeat_count = 0
        deterministic_reason = _deterministic_failure_reason(status, result, error_message) if is_failure else ""
        state = self._resolve_turn(turn_id=str(kwargs.get("turn_id") or ""), session_id=str(kwargs.get("session_id") or ""))
        if is_failure and state is not None:
            exit_code = kwargs.get("exit_code", kwargs.get("returncode"))
            fail_fp = failure_fingerprint(
                tool_name=tool_name,
                args=args,
                status=status,
                result=result,
                error_message=error_message,
                exit_code=exit_code,
            )
            with self._lock:
                episode = state.failure_episodes.get(fail_fp)
                if episode is None:
                    episode = FailureEpisode(
                        fingerprint=fail_fp,
                        action_fingerprint=action_fp,
                        first_event_id=event_id,
                    )
                    state.failure_episodes[fail_fp] = episode
                    self._metrics["failure_episodes"] += 1
                episode.count += 1
                episode.last_event_id = event_id
                repeat_count = episode.count
                if repeat_count >= 2:
                    self._metrics["repeated_failures_seen"] += 1
                if deterministic_reason and repeat_count == 1:
                    self._activate_deterministic_failure_replan(state, episode, deterministic_reason)
                elif repeat_count >= self._config.repeated_failure_local_replan_at:
                    self._activate_local_loop_breaker(state, episode)
            if repeat_count >= 2:
                event_type = "REPEATED_FAILURE"

        event = {
            "type": event_type,
            "turn_id": kwargs.get("turn_id"),
            "session_id": kwargs.get("session_id"),
            "event_id": event_id,
            "tool_name": tool_name,
            "origin_tool": tool_name,
            "origin_event_type": event_type,
            "origin_event_id": event_id,
            "origin_failure_fingerprint": fail_fp,
            "status": status,
            "risk": risk,
            "risk_reasons": risk_reasons,
            "novelty": 0.7 if contradiction and repeat_count <= 1 else 0.15,
            "uncertainty": 0.65 if is_failure else 0.15,
            "contradiction": contradiction,
            "repeated_failure": repeat_count >= 2,
            "deterministic_failure": bool(deterministic_reason),
            "deterministic_failure_reason": deterministic_reason,
            "failure_repeat_count": repeat_count,
            "failure_fingerprint": fail_fp,
            "action_fingerprint": action_fp,
            "state": {
                "args": args,
                "result_preview": result[: self._config.event_preview_chars],
                "error": error_message[:500],
                "duration_ms": kwargs.get("duration_ms"),
                "exit_code": kwargs.get("exit_code", kwargs.get("returncode")),
            },
        }
        # Obvious deterministic failures are resolved locally: an exact retry cannot
        # change a policy/schema/path result, so avoid spending a provider call on it.
        # Other first failures remain eligible for remote assessment; exact repeats
        # are intentionally local until state/evidence changes.
        if deterministic_reason:
            event["suppress_remote"] = True
            event["suppress_reason"] = "deterministic-failure-local-replan"
        elif repeat_count >= 2:
            event["suppress_remote"] = True
            event["suppress_reason"] = "repeated-failure-dedup"
        return self.emit_event(event)

    def before_tool_call(self, **kwargs: Any) -> dict[str, Any] | None:
        """Local control-lease enforcement at Hermes' pre-tool execution seam.

        No provider call occurs here. This is the piece v0.2.0 lacked: a remote
        REPLAN/GATHER_EVIDENCE decision can now constrain the next exact action,
        and the local repeated-failure breaker works even when Jev is late.
        """
        if not self._config.enabled or self._config.mode == "shadow":
            return None
        tool_name = str(kwargs.get("tool_name") or "")
        if tool_name.startswith(("nerve_", "jev_")):
            return None
        args = kwargs.get("args") if isinstance(kwargs.get("args"), dict) else {}
        state = self._resolve_turn(turn_id=str(kwargs.get("turn_id") or ""), session_id=str(kwargs.get("session_id") or ""))
        if state is None:
            return None
        proposed_fp = tool_action_fingerprint(tool_name, args)
        with self._lock:
            directive = state.active_control
            if directive is None:
                return None
            same_action = bool(directive.action_fingerprint and proposed_fp == directive.action_fingerprint)
            if not directive.delivered:
                directive.delivered = True
                self._metrics["controls_delivered"] += 1
                self._record_control_lifecycle(state, directive, stage="delivered", disposition="pre-tool")
            if directive.control == "RETRY":
                self._metrics["controls_followed"] += 1
                self._record_control_lifecycle(
                    state, directive, stage="next_action", disposition="followed",
                    proposed_action_fingerprint=proposed_fp,
                )
                state.active_control = None
                return None
            if directive.control in BLOCKING_CONTROLS and same_action:
                directive.attempted_overrides += 1
                self._metrics["control_override_attempts"] += 1
                self._metrics["controls_enforced"] += 1
                if directive.failure_fingerprint in state.failure_episodes:
                    state.failure_episodes[directive.failure_fingerprint].blocked_attempts += 1
                self._record_control_lifecycle(
                    state, directive, stage="next_action", disposition="enforced",
                    proposed_action_fingerprint=proposed_fp, attempted_override=True,
                )
                message = (
                    f"Nerve {directive.control} control {directive.decision_id} prevents repeating the exact action "
                    "that already failed. Choose a materially different diagnostic/recovery step"
                    + (" or escalate." if directive.control == "ESCALATE" else ".")
                )
                self._log("control_enforced", {**directive.as_dict(), "tool_name": tool_name, "proposed_action_fingerprint": proposed_fp})
                return {"action": "block", "message": message}

            # Any materially different tool action demonstrates that the replan /
            # gather-evidence control affected trajectory. Consume the lease.
            self._metrics["controls_followed"] += 1
            self._record_control_lifecycle(
                state, directive, stage="next_action", disposition="followed",
                proposed_action_fingerprint=proposed_fp,
            )
            state.active_control = None
        return None

    def _do_assessment(self, payload: dict[str, Any]) -> None:
        tid = str(payload.get("turn_id") or "")
        event = dict(payload.get("event") or {})
        try:
            with self._lock:
                state = self._turns.get(tid)
                if state is None or state.finished:
                    return
                state.provider_calls += 1
                self._metrics["provider_calls"] += 1
                origin = str(event.get("origin_tool") or event.get("tool_name") or event.get("type") or "unknown").strip().lower()
                origin = origin.replace(" ", "_")[:80] or "unknown"
                self._metrics[f"provider_calls_origin_{origin}"] += 1
            choices = [str(x) for x in (event.get("choices") or []) if str(x).strip()]
            hermes_decision = str(event.get("hermes_decision") or "")
            engine = self._engine_factory()
            if len(set(choices)) >= 2 and hermes_decision:
                result = engine.decide(
                    state=self._assessment_state(state, event),
                    instructions=(
                        "Choose the best bounded action for the stated objective from the supplied alternatives. "
                        "Judge the work state, evidence, risk, and reversibility. Do not invent another label."
                    ),
                    choices=choices,
                    criteria=event.get("criteria") if isinstance(event.get("criteria"), dict) else None,
                    contract="hermes/nerve-nervous-decision/v1",
                )
                jev_decision = result.value
                confidence = result.confidence
                probabilities = result.probabilities
                request_id = result.request_id
                latency_ms = result.latency_ms
                usage = dict(getattr(result, "usage", {}) or {})
            else:
                assessment = engine.assess(
                    state=self._assessment_state(state, event),
                    questions={
                        "intervention": {
                            "type": "noul",
                            "instructions": "Is a Jev intervention likely to improve the next meaningful Hermes action?",
                        },
                        "next_control": {
                            "type": "choice",
                            "instructions": "Select the best control response to the current state.",
                            "criteria": {label: None for label in CONTROL_CHOICES},
                        },
                    },
                    contract="hermes/nerve-nervous-control/v1",
                )
                answer = assessment["answers"].get("next_control") or {}
                jev_decision = str(answer.get("choice") or "CONTINUE")
                probabilities = {str(k): float(v) for k, v in (answer.get("probabilities") or {}).items()}
                confidence = float(answer.get("confidence", probabilities.get(jev_decision, 0.0)))
                request_id = str(assessment.get("request_id") or "")
                latency_ms = float(assessment.get("latency_ms") or 0.0)
                usage = dict(assessment.get("usage") or {})

            semantic_fp = decision_state_fingerprint(event)
            with self._lock:
                state = self._turns.get(tid)
                if state is None:
                    return
                state.last_remote_fingerprint = semantic_fp

            with self._lock:
                self._metrics[f"control_{jev_decision.lower()}"] += 1
                if str(event.get("type") or "").upper() in {"COMPLETION", "COMPLETION_CANDIDATE"}:
                    self._metrics["completion_assessments"] += 1
            disagreement = bool(hermes_decision and jev_decision != hermes_decision)
            intervention = (not hermes_decision and jev_decision not in {"CONTINUE", ""})
            if disagreement:
                self._metrics["disagreements"] += 1
            elif hermes_decision:
                self._metrics["agreements"] += 1
            should_challenge = (disagreement or intervention) and confidence >= self._config.challenge_confidence
            decision_id = f"jev-{uuid.uuid4().hex}"
            decision_row = {
                "record_type": "decision",
                "decision_id": decision_id,
                "turn_id": tid,
                "event_id": str(event.get("event_id") or ""),
                "state_version": str(event.get("state_version") or ""),
                "decision_version": str(event.get("decision_version") or ""),
                "objective_type": str(event.get("scope") or event.get("objective_type") or "work"),
                "decision_type": str(event.get("type") or ""),
                "hermes_decision": hermes_decision,
                "jev_decision": jev_decision,
                "agreement": (jev_decision == hermes_decision) if hermes_decision else None,
                "jev_confidence": confidence,
                "jev_probabilities": probabilities,
                "materiality": event.get("materiality"),
                "reversible": event.get("reversible"),
                "consequence": event.get("consequence"),
                "router_reasons": (payload.get("router") or {}).get("reasons", []),
                "router_score": (payload.get("router") or {}).get("score"),
                "latency_ms": latency_ms,
                "usage": usage,
                "request_id": request_id,
                "origin_tool": str(event.get("origin_tool") or event.get("tool_name") or ""),
                "origin_event_type": str(event.get("origin_event_type") or event.get("type") or ""),
                "origin_event_id": str(event.get("origin_event_id") or event.get("event_id") or ""),
                "origin_failure_fingerprint": str(event.get("origin_failure_fingerprint") or event.get("failure_fingerprint") or ""),
                "challenge_issued": should_challenge,
                "failure_fingerprint": str(event.get("failure_fingerprint") or ""),
                "action_fingerprint": str(event.get("action_fingerprint") or ""),
                "stale": False,
            }
            self._outcomes.append(decision_row)

            if should_challenge:
                directive = ControlDirective(
                    decision_id=decision_id,
                    event_id=str(event.get("event_id") or ""),
                    control=jev_decision,
                    confidence=confidence,
                    source="jev-provider",
                    reason="confident-disagreement" if disagreement else "confident-control-intervention",
                    created_at=self._now(),
                    failure_fingerprint=str(event.get("failure_fingerprint") or ""),
                    action_fingerprint=str(event.get("action_fingerprint") or ""),
                    state_version=str(event.get("state_version") or ""),
                )
                challenge = Challenge(
                    challenge_id=uuid.uuid4().hex,
                    decision_id=decision_id,
                    turn_id=tid,
                    event_id=str(event.get("event_id") or ""),
                    state_version=str(event.get("state_version") or ""),
                    decision_version=str(event.get("decision_version") or ""),
                    hermes_decision=hermes_decision,
                    jev_decision=jev_decision,
                    confidence=confidence,
                    probabilities=probabilities,
                    reason=directive.reason,
                    created_at=self._now(),
                )
                with self._lock:
                    state = self._turns.get(tid)
                    if state:
                        state.pending_challenges.append(challenge)
                        state.challenge_count += 1
                        # Only controls that can govern an actual next tool action
                        # need an execution lease. Bounded abstract disagreements are
                        # still delivered through the normal challenge backchannel.
                        if directive.action_fingerprint and jev_decision in set(CONTROL_CHOICES):
                            self._activate_control(state, directive)
                        fail_fp = directive.failure_fingerprint
                        if fail_fp and fail_fp in state.failure_episodes:
                            episode = state.failure_episodes[fail_fp]
                            episode.last_control = jev_decision
                            episode.last_decision_id = decision_id
                    self._metrics["challenges_created"] += 1
                    if disagreement:
                        self._metrics["high_confidence_disagreements"] += 1
                    self._recent_challenges.append(challenge.as_dict())
                self._log("challenge", challenge.as_dict())
            else:
                self._metrics["agreements_or_silent"] += 1
                self._log("assessment_silent", {
                    "decision_id": decision_id,
                    "turn_id": tid,
                    "event_id": event.get("event_id"),
                    "hermes_decision": hermes_decision,
                    "jev_decision": jev_decision,
                    "confidence": confidence,
                    "request_id": request_id,
                    "latency_ms": latency_ms,
                })
        except Exception as exc:
            with self._lock:
                state = self._turns.get(tid)
                if state:
                    state.provider_errors += 1
                self._metrics["provider_errors"] += 1
            self._log("assessment_error", {"turn_id": tid, "event_id": event.get("event_id"), "error": str(exc)[:500]})
        finally:
            batch_payload = None
            with self._lock:
                state = self._turns.get(tid)
                if state:
                    state.assessment_inflight = False
                    if state.pending_batch and not state.finished and state.provider_calls < self._config.max_provider_calls_per_turn:
                        items = list(state.pending_batch)
                        state.pending_batch.clear()

                        def _mx(key: str) -> float:
                            values = []
                            for item in items:
                                try:
                                    values.append(float(item.get(key) or 0.0))
                                except (TypeError, ValueError):
                                    pass
                            return max(values) if values else 0.0

                        batch_event = {
                            "event_id": uuid.uuid4().hex,
                            "turn_id": tid,
                            "session_id": state.session_id,
                            "type": "BATCH",
                            "goal": next((x.get("goal") for x in reversed(items) if x.get("goal")), ""),
                            "state_version": str(items[-1].get("state_version") or state.last_state_version),
                            "decision_version": uuid.uuid4().hex,
                            "materiality": _mx("materiality"),
                            "uncertainty": _mx("uncertainty"),
                            "novelty": _mx("novelty"),
                            "consequence": _mx("consequence"),
                            "risk": _mx("risk"),
                            "contradiction": any(bool(x.get("contradiction")) for x in items),
                            "state": {
                                "batched_event_count": len(items),
                                "event_types": [str(x.get("type") or "") for x in items],
                                "events": items[-12:],
                            },
                        }
                        state.assessment_inflight = True
                        state.events_forwarded += 1
                        self._metrics["batches_forwarded"] += 1
                        self._metrics["events_forwarded"] += 1
                        batch_payload = {"turn_id": tid, "event": batch_event, "router": {"reasons": ["adaptive-batch"], "score": 1.0}}
            if batch_payload is not None:
                self._tasks.put(("assess", batch_payload, contextvars.copy_context()))

    def _assessment_state(self, state: TurnState, event: dict[str, Any]) -> dict[str, Any]:
        failure_episode = None
        fail_fp = str(event.get("failure_fingerprint") or "")
        if fail_fp:
            episode = state.failure_episodes.get(fail_fp)
            if episode:
                failure_episode = {
                    "fingerprint": episode.fingerprint,
                    "count": episode.count,
                    "provider_evaluations": episode.provider_evaluations,
                    "blocked_attempts": episode.blocked_attempts,
                    "last_control": episode.last_control,
                }
        return {
            "turn": {
                "turn_id": state.turn_id,
                "admission": state.admission,
                "events_seen": state.events_seen,
                "provider_calls": state.provider_calls,
            },
            "event": event,
            "failure_episode": failure_episode,
            "recent_event_types": [str(x.get("type") or "") for x in list(state.recent_events)[-12:]],
        }

    def pop_challenge(self, *, turn_id: str = "", session_id: str = "") -> dict[str, Any] | None:
        state = self._resolve_turn(turn_id=turn_id, session_id=session_id)
        if state is None:
            return None
        with self._lock:
            while state.pending_challenges:
                challenge: Challenge = state.pending_challenges.popleft()
                if challenge.state_version and state.last_state_version and challenge.state_version != state.last_state_version:
                    # A failure-control lease may remain semantically current even
                    # when the monotonic state version advanced. Execution
                    # enforcement uses action/failure fingerprints; prose challenge
                    # delivery remains conservative and stale-by-version.
                    challenge.stale = True
                    self._metrics["challenges_stale"] += 1
                    self._log("challenge_stale", challenge.as_dict())
                    continue
                self._metrics["challenges_delivered"] += 1
                return challenge.as_dict()
        return None

    def inject_challenge(self, result: str, *, turn_id: str = "", session_id: str = "") -> str | None:
        challenge = self.pop_challenge(turn_id=turn_id, session_id=session_id)
        if challenge is None or self._config.mode == "shadow":
            return None
        state = self._resolve_turn(turn_id=turn_id, session_id=session_id)
        if state is not None:
            with self._lock:
                directive = state.active_control
                if directive and directive.decision_id == challenge.get("decision_id") and not directive.delivered:
                    directive.delivered = True
                    self._metrics["controls_delivered"] += 1
                    self._record_control_lifecycle(state, directive, stage="delivered", disposition="model-context")
        reason = str(challenge.get("reason") or "")
        if reason.startswith("local-deterministic-failure:"):
            instruction = (
                "The last tool action failed deterministically. Do not repeat the same tool with the same arguments. "
                "Choose a materially different allowed recovery or diagnostic step; if no viable alternative exists, escalate."
            )
        else:
            instruction = (
                "Reconsider the next action using this independent bounded judgment. Do not blindly undo completed "
                "irreversible work; if the referenced state is no longer current, treat this as advisory evidence."
            )
        payload = {
            "type": "JEV_DECISION_CHALLENGE",
            "instruction": instruction,
            **challenge,
        }
        return str(result) + "\n\n<JEV_NERVOUS_SYSTEM>\n" + json.dumps(payload, sort_keys=True) + "\n</JEV_NERVOUS_SYSTEM>"

    def completion_gate(self, **kwargs: Any) -> dict[str, str] | None:
        state = self._resolve_turn(turn_id=str(kwargs.get("turn_id") or ""), session_id=str(kwargs.get("session_id") or ""))
        if state is None or state.admission not in {"ON", "WATCH"}:
            return None
        event = {
            "type": "COMPLETION_CANDIDATE",
            "turn_id": state.turn_id,
            "session_id": state.session_id,
            "completion_candidate": True,
            "materiality": 1.0,
            "consequence": 0.85,
            "novelty": 0.8,
            "goal": "Finish the current Hermes turn only if its objective and verification obligations are actually satisfied.",
            "hermes_decision": "DONE",
            "choices": ["DONE", "NEEDS_VERIFICATION", "REPLAN", "ESCALATE"],
            "state": {
                "final_response_preview": str(kwargs.get("final_response") or "")[: self._config.event_preview_chars],
                "changed_paths": list(kwargs.get("changed_paths") or []),
                "coding": bool(kwargs.get("coding")),
                "attempt": kwargs.get("attempt"),
            },
        }
        self.emit_event(event)
        if self._config.mode != "precommit":
            return None
        challenge = self.pop_challenge(turn_id=state.turn_id)
        if challenge and challenge.get("jev_decision") not in {"DONE", "CONTINUE"}:
            return {"action": "continue", "message": f"Jev completion challenge: {challenge.get('jev_decision')} ({challenge.get('confidence'):.3f}). Re-verify before finishing."}
        return None

    def finish_turn(self, *, turn_id: str = "", session_id: str = "", assistant_response: str = "") -> None:
        state = self._resolve_turn(turn_id=turn_id, session_id=session_id)
        if state is None:
            return
        with self._lock:
            if state.finished:
                return
            if state.active_control is not None:
                self._metrics["controls_expired"] += 1
                self._record_control_lifecycle(state, state.active_control, stage="expired", disposition="turn-ended")
                state.active_control = None
            state.finished = True
            completion = {
                "turn_id": state.turn_id,
                "session_id": state.session_id,
                "admission": state.admission,
                "events_seen": state.events_seen,
                "events_forwarded": state.events_forwarded,
                "events_suppressed": state.events_suppressed,
                "provider_calls": state.provider_calls,
                "provider_errors": state.provider_errors,
                "challenges": state.challenge_count,
                "failure_episodes": len(state.failure_episodes),
                "response_sha256": canonical_hash(assistant_response),
            }
            self._metrics["turns_finished"] += 1
            self._metrics["turns_evicted"] += 1
            self._turns.pop(state.turn_id, None)
            if state.session_id and self._session_turn.get(state.session_id) == state.turn_id:
                self._session_turn.pop(state.session_id, None)
        self._log("turn_complete", completion)

    def status(self, *, turn_id: str = "", session_id: str = "") -> dict[str, Any]:
        state = self._resolve_turn(turn_id=turn_id, session_id=session_id)
        if state is None:
            return {"active": False}
        with self._lock:
            return {
                "active": not state.finished,
                "turn_id": state.turn_id,
                "session_id": state.session_id,
                "admission": state.admission,
                "admission_confidence": state.admission_confidence,
                "events_seen": state.events_seen,
                "events_forwarded": state.events_forwarded,
                "events_suppressed": state.events_suppressed,
                "provider_calls": state.provider_calls,
                "provider_errors": state.provider_errors,
                "watch_promotions": state.watch_promotions,
                "pending_challenges": len(state.pending_challenges),
                "pending_batch_events": len(state.pending_batch),
                "failure_episodes": len(state.failure_episodes),
                "active_control": state.active_control.as_dict() if state.active_control else None,
                "mode": self._config.mode,
            }

    def drain(self, timeout: float = 5.0) -> bool:
        """Wait for already-queued background work; intended for tests and diagnostics."""
        deadline = time.monotonic() + max(0.0, float(timeout))
        while time.monotonic() < deadline:
            if self._tasks.unfinished_tasks == 0:
                with self._lock:
                    if not any(state.assessment_inflight for state in self._turns.values()):
                        return True
            time.sleep(0.01)
        return False

    def quality_metrics(self) -> dict[str, Any]:
        with self._lock:
            m = dict(self._metrics)
            turns = int(m.get("turns_started", 0))
            events = int(m.get("events_seen", 0))
            calls = int(m.get("provider_calls", 0))
            meaningful = int(m.get("meaningful_decisions", 0))
            suppressed = int(m.get("events_suppressed", 0))
            # repeated_failure_provider_calls_avoided is a labeled subset of
            # events_suppressed; do not double-count it in the aggregate.
            avoided = suppressed + int(m.get("events_batched", 0))
        outcome = self._outcomes.report()
        om = outcome.get("metrics", {})
        useful = int(om.get("useful_disagreements", 0))
        useless = int(om.get("useless_disagreements", 0))
        accepted = int(om.get("controls_followed", 0))
        overridden = int(om.get("controls_overridden", 0))
        success = int(om.get("successful_outcomes", 0))
        outcomes = int(om.get("outcomes", 0))
        challenge_precision = useful / (useful + useless) if (useful + useless) else None
        final_success_rate = success / outcomes if outcomes else None
        return {
            "scope": "current-process + profile outcome ledger (fields labeled below)",
            "total_turns": turns,
            "off_turns": int(m.get("admission_off", 0)),
            "watch_turns": int(m.get("admission_watch", 0)),
            "on_turns": int(m.get("admission_on", 0)),
            "watch_to_on_promotions": int(m.get("watch_promotions", 0)),
            "total_raw_events": events,
            "events_consumed_locally": max(0, events - int(m.get("events_forwarded", 0))),
            "events_suppressed": suppressed,
            "events_batched": int(m.get("events_batched", 0)),
            "events_forwarded_to_jev": int(m.get("events_forwarded", 0)),
            "jev_internal_events_seen": int(m.get("nerve_internal_seen", 0)),
            "nerve_internal_events_suppressed": int(m.get("nerve_internal_suppressed", 0)),
            "provider_calls_by_origin": {
                key.removeprefix("provider_calls_origin_"): value
                for key, value in sorted(m.items()) if key.startswith("provider_calls_origin_")
            },
            "provider_call_avoidance_count": avoided,
            "repeated_failure_provider_calls_avoided": int(m.get("repeated_failure_provider_calls_avoided", 0)),
            "failure_episodes": int(m.get("failure_episodes", 0)),
            "repeated_failures_seen": int(m.get("repeated_failures_seen", 0)),
            "local_loop_breakers": int(m.get("local_loop_breakers", 0)),
            "deterministic_failure_replans": int(m.get("deterministic_failure_replans", 0)),
            "jev_calls_per_turn": round(calls / turns, 4) if turns else 0.0,
            "jev_calls_per_100_events": round(100.0 * calls / events, 4) if events else 0.0,
            "jev_calls_per_meaningful_decision": round(calls / meaningful, 4) if meaningful else 0.0,
            "meaningful_decisions": meaningful,
            "jev_decisions_followed": accepted,
            "jev_decisions_overridden": overridden,
            "controls_activated": int(m.get("controls_activated", 0)),
            "controls_delivered": int(m.get("controls_delivered", 0)),
            "controls_followed": int(m.get("controls_followed", 0)),
            "controls_enforced": int(m.get("controls_enforced", 0)),
            "control_override_attempts": int(m.get("control_override_attempts", 0)),
            "controls_expired": int(m.get("controls_expired", 0)),
            "agreements": int(m.get("agreements", 0)),
            "disagreements": int(m.get("disagreements", 0)),
            "high_confidence_disagreements": int(m.get("high_confidence_disagreements", 0)),
            "challenges_issued": int(m.get("challenges_created", 0)),
            "challenges_accepted": int(om.get("challenges_accepted", 0)),
            "challenges_rejected": int(om.get("challenges_rejected", 0)),
            "stale_challenges": int(m.get("challenges_stale", 0)),
            "useful_disagreements": useful,
            "useless_disagreements": useless,
            "false_positive_challenges": int(om.get("false_positive_challenges", 0)),
            "false_negative_supervision_events": int(om.get("false_negative_supervision", 0)),
            "retry_count": int(m.get("control_retry", 0)),
            "replan_count": int(m.get("control_replan", 0)),
            "escalate_count": int(m.get("control_escalate", 0)),
            "gather_evidence_count": int(m.get("control_gather_evidence", 0)),
            "completion_assessments": int(m.get("completion_assessments", 0)),
            "false_pass": int(om.get("false_pass", 0)),
            "false_replan": int(om.get("false_replan", 0)),
            "premature_done_catches": int(om.get("premature_done_caught", 0)),
            "decision_lease_reuse": int(m.get("decision_lease_reuse", 0)),
            "decision_lease_invalidations": int(m.get("decision_lease_invalidations", 0)),
            "decision_lease_invalidation_reasons": {
                key.removeprefix("decision_lease_invalidation_"): value
                for key, value in sorted(m.items()) if key.startswith("decision_lease_invalidation_")
            },
            "average_jev_latency_ms": outcome.get("average_latency_ms", 0.0),
            "p50_jev_latency_ms": outcome.get("p50_latency_ms", 0.0),
            "p95_jev_latency_ms": outcome.get("p95_latency_ms", 0.0),
            "jev_tokens": outcome.get("jev_tokens", 0),
            # provider_cost / provider_reported_cost are None when the provider
            # returned no cost for the underlying decisions (the typesafe
            # transport ships no cost field); None reads as "cost unreported",
            # a bare 0.0 would read as "Jev is free". Pair with
            # jev_provider_cost_missing_decisions for the exact split.
            "jev_provider_cost": outcome.get("provider_cost"),
            "jev_provider_reported_cost": outcome.get("provider_reported_cost"),
            "jev_provider_cost_missing_decisions": outcome.get("provider_cost_missing_decisions", 0),
            "estimated_avoided_jev_calls": avoided,
            "estimated_avoided_provider_cost": None,
            "avoided_synchronous_wait_ms": round(float(outcome.get("average_latency_ms", 0.0)) * calls, 3),
            "final_task_success_rate": final_success_rate,
            "decision_correction_success": int(om.get("decision_correction_success", 0)),
            "decision_correction_denominator": int(outcome.get("decision_correction_denominator", 0)),
            "decision_correction_success_rate": outcome.get("decision_correction_success_rate"),
            "router_precision": challenge_precision,
            "router_recall": None,
            "challenge_precision": challenge_precision,
            "confidence_calibration": outcome.get("confidence_calibration", {}),
        }

    def report(self, *, recent_limit: int = 5, include_recent: bool = False, include_outcomes: bool = False) -> dict[str, Any]:
        limit = max(0, min(20, int(recent_limit or 0)))
        with self._lock:
            admissions = {key.removeprefix("admission_"): value for key, value in self._metrics.items() if key.startswith("admission_")}
            active = sum(1 for state in self._turns.values() if not state.finished)
            payload: dict[str, Any] = {
                "scope": {
                    "runtime_metrics": "current-process",
                    "outcome_metrics": "profile-ledger",
                    "recent": "rolling-current-process",
                },
                "enabled": self._config.enabled,
                "mode": self._config.mode,
                "admission_enabled": self._config.admission_enabled,
                "challenge_confidence": self._config.challenge_confidence,
                "call_threshold": self._config.call_threshold,
                "max_provider_calls_per_turn": self._config.max_provider_calls_per_turn,
                "repeated_failure_local_replan_at": self._config.repeated_failure_local_replan_at,
                "local_learning": self._config.local_learning,
                "local_learning_min_samples": self._config.local_learning_min_samples,
                "active_turns": active,
                "metrics": dict(sorted(self._metrics.items())),
                "admissions": admissions,
                "quality_metrics": self.quality_metrics(),
                "path": str(self._log_path()),
                "execution": execution_provenance(live_provider_call=False, transport="local-nervous-system"),
            }
            if include_recent:
                payload["recent_router"] = list(self._recent_router)[-limit:] if limit else []
                payload["recent_challenges"] = list(self._recent_challenges)[-limit:] if limit else []
                payload["recent_controls"] = list(self._recent_controls)[-limit:] if limit else []
        if include_outcomes:
            payload["outcomes"] = self._outcomes.report()
        return payload


_default = NervousSystem()


def configure(**kwargs: Any) -> None:
    _default.configure(**kwargs)


def pre_llm_call(**kwargs: Any) -> str | None:
    if not _default._config.enabled:
        return None
    _default.start_turn(
        user_message=str(kwargs.get("user_message") or ""),
        session_id=str(kwargs.get("session_id") or ""),
        turn_id=str(kwargs.get("turn_id") or ""),
    )
    if _default._config.emit_prompt_hint:
        return (
            "Nerve nervous supervision is active for this turn. For a material bounded decision with explicit "
            "alternatives, you may call nerve_nervous_event once with your proposed choice; routine reads and tool calls "
            "do not need explicit Jev calls."
        )
    return None


def pre_tool_call(**kwargs: Any) -> dict[str, Any] | None:
    return _default.before_tool_call(**kwargs)


def post_tool_call(**kwargs: Any) -> None:
    _default.observe_tool_call(**kwargs)


def transform_tool_result(**kwargs: Any) -> str | None:
    return _default.inject_challenge(
        str(kwargs.get("result") or ""),
        turn_id=str(kwargs.get("turn_id") or ""),
        session_id=str(kwargs.get("session_id") or ""),
    )


def pre_verify(**kwargs: Any) -> dict[str, str] | None:
    return _default.completion_gate(**kwargs)


def post_llm_call(**kwargs: Any) -> None:
    _default.finish_turn(
        turn_id=str(kwargs.get("turn_id") or ""),
        session_id=str(kwargs.get("session_id") or ""),
        assistant_response=str(kwargs.get("assistant_response") or ""),
    )


def on_session_end(**kwargs: Any) -> None:
    session_id = str(kwargs.get("session_id") or "")
    state = _default._resolve_turn(session_id=session_id)
    if state:
        _default.finish_turn(turn_id=state.turn_id, session_id=session_id)


def emit_event(event: dict[str, Any]) -> dict[str, Any]:
    return _default.emit_event(event)


def status(turn_id: str = "", session_id: str = "") -> dict[str, Any]:
    return _default.status(turn_id=turn_id, session_id=session_id)


def report(*, recent_limit: int = 5, include_recent: bool = False, include_outcomes: bool = False) -> dict[str, Any]:
    return _default.report(recent_limit=recent_limit, include_recent=include_recent, include_outcomes=include_outcomes)
