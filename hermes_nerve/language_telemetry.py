"""Language telemetry and structured observation counters for Hermes Nerve (KEE-73 phase 2).

This module implements the observe-only validation pipeline and durable structured
counters for language contract observations.  No enforcement occurs here — this
is a pure telemetry/audit layer.

Observe-only validation
-----------------------
``ObserveOnlyValidator`` takes a resolved language (from the contract) and an
assistant response text, classifies the prose content, and records whether the
detected prose language matches the contract.  It never blocks, rejects, or
modifies any message.

Telemetry counters
------------------
``LanguageTelemetry`` is a lightweight in-memory counter store for one session.
It tracks per-turn observations and per-language counts.  It is serialisable to
a plain dict and can be persisted/loaded independently of the
``SessionLanguageState``.

PRIVATE implementation — KEE-73 phase 2.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

from .language_detector import prose_for_detection
from .language_contract import detect_language_from_text

__all__ = [
    "ObservationResult",
    "TurnObservation",
    "LanguageTelemetry",
    "ObserveOnlyValidator",
]

logger = logging.getLogger("hermes_nerve.language_telemetry")

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

ObservationResult = Literal[
    "match",          # detected prose language matches the contract language
    "mismatch",       # detected prose language differs from the contract language
    "undetermined",   # prose present but language could not be detected
    "no_prose",       # all content was exempt; nothing to validate
    "contract_off",   # contract mode is 'off'; validation skipped
    "no_contract_lang",  # contract has no resolved language yet (user mode, early session)
]


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class TurnObservation:
    """A single per-turn language observation record.

    Attributes
    ----------
    turn_id : str
        Opaque turn identifier.
    result : ObservationResult
        Classification of how this turn's assistant prose compares to the contract.
    contract_language : str | None
        The resolved contract language at the time of observation (may be None).
    detected_language : str | None
        The language detected in the assistant prose (may be None).
    prose_char_count : int
        Number of prose characters extracted from the response.
    observed_at : str
        ISO-8601 UTC timestamp.
    """

    turn_id: str
    result: ObservationResult
    contract_language: str | None
    detected_language: str | None
    prose_char_count: int
    observed_at: str = field(default_factory=_utc_now)

    def as_dict(self) -> dict[str, Any]:
        return {
            "turn_id": self.turn_id,
            "result": self.result,
            "contract_language": self.contract_language,
            "detected_language": self.detected_language,
            "prose_char_count": self.prose_char_count,
            "observed_at": self.observed_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TurnObservation":
        return cls(
            turn_id=data["turn_id"],
            result=data["result"],
            contract_language=data.get("contract_language"),
            detected_language=data.get("detected_language"),
            prose_char_count=int(data.get("prose_char_count", 0)),
            observed_at=data.get("observed_at", _utc_now()),
        )


@dataclass
class LanguageTelemetry:
    """Structured per-session language telemetry counters.

    All fields are non-sensitive (language tags and counts only; no message
    text is stored).

    Attributes
    ----------
    session_id : str
    total_turns_observed : int
        Number of turns passed through the observer.
    match_count : int
        Turns where detected language matched the contract.
    mismatch_count : int
        Turns where detected language differed from the contract.
    undetermined_count : int
        Turns with prose but undetected language.
    no_prose_count : int
        Turns where all content was exempt.
    contract_off_count : int
        Turns skipped because the contract was off.
    no_contract_lang_count : int
        Turns where the contract had no resolved language yet.
    language_counts : dict[str, int]
        Count of detected languages (key = BCP-47 tag, value = occurrence count).
    mismatch_languages : dict[str, int]
        Detected languages on mismatch turns (key = detected tag, value = count).
    recent_observations : list[TurnObservation]
        Ring buffer of the last N observations (default N=50).  Not a truth
        store — for diagnostic inspection only.
    created_at : str
    updated_at : str
    version : int
    """

    session_id: str
    total_turns_observed: int = 0
    match_count: int = 0
    mismatch_count: int = 0
    undetermined_count: int = 0
    no_prose_count: int = 0
    contract_off_count: int = 0
    no_contract_lang_count: int = 0
    language_counts: dict[str, int] = field(default_factory=dict)
    mismatch_languages: dict[str, int] = field(default_factory=dict)
    recent_observations: list[TurnObservation] = field(default_factory=list)
    _recent_max: int = field(default=50, repr=False, compare=False)
    created_at: str = field(default_factory=_utc_now)
    updated_at: str = field(default_factory=_utc_now)
    version: int = 1

    def record(self, obs: TurnObservation) -> None:
        """Incorporate one observation into the counters."""
        self.total_turns_observed += 1
        self.updated_at = _utc_now()

        result = obs.result
        if result == "match":
            self.match_count += 1
        elif result == "mismatch":
            self.mismatch_count += 1
            if obs.detected_language:
                self.mismatch_languages[obs.detected_language] = (
                    self.mismatch_languages.get(obs.detected_language, 0) + 1
                )
        elif result == "undetermined":
            self.undetermined_count += 1
        elif result == "no_prose":
            self.no_prose_count += 1
        elif result == "contract_off":
            self.contract_off_count += 1
        elif result == "no_contract_lang":
            self.no_contract_lang_count += 1

        if obs.detected_language:
            self.language_counts[obs.detected_language] = (
                self.language_counts.get(obs.detected_language, 0) + 1
            )

        # Maintain ring buffer
        self.recent_observations.append(obs)
        if len(self.recent_observations) > self._recent_max:
            self.recent_observations = self.recent_observations[-self._recent_max:]

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "session_id": self.session_id,
            "total_turns_observed": self.total_turns_observed,
            "match_count": self.match_count,
            "mismatch_count": self.mismatch_count,
            "undetermined_count": self.undetermined_count,
            "no_prose_count": self.no_prose_count,
            "contract_off_count": self.contract_off_count,
            "no_contract_lang_count": self.no_contract_lang_count,
            "language_counts": dict(self.language_counts),
            "mismatch_languages": dict(self.mismatch_languages),
            "recent_observations": [o.as_dict() for o in self.recent_observations],
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any], *, recent_max: int = 50) -> "LanguageTelemetry":
        recent = [
            TurnObservation.from_dict(o)
            for o in data.get("recent_observations", [])
        ]
        obj = cls(
            session_id=data["session_id"],
            total_turns_observed=int(data.get("total_turns_observed", 0)),
            match_count=int(data.get("match_count", 0)),
            mismatch_count=int(data.get("mismatch_count", 0)),
            undetermined_count=int(data.get("undetermined_count", 0)),
            no_prose_count=int(data.get("no_prose_count", 0)),
            contract_off_count=int(data.get("contract_off_count", 0)),
            no_contract_lang_count=int(data.get("no_contract_lang_count", 0)),
            language_counts=dict(data.get("language_counts", {})),
            mismatch_languages=dict(data.get("mismatch_languages", {})),
            recent_observations=recent,
            created_at=data.get("created_at", _utc_now()),
            updated_at=data.get("updated_at", _utc_now()),
            version=int(data.get("version", 1)),
        )
        obj._recent_max = recent_max
        return obj

    @property
    def mismatch_rate(self) -> float | None:
        """Fraction of turns with detectable prose that were mismatches.

        Returns ``None`` if no turns with detectable prose have been observed.
        """
        prose_turns = self.match_count + self.mismatch_count + self.undetermined_count
        if prose_turns == 0:
            return None
        return self.mismatch_count / prose_turns


# ---------------------------------------------------------------------------
# Observe-only validator
# ---------------------------------------------------------------------------

class ObserveOnlyValidator:
    """Observe-only language contract validator.

    Validates assistant-authored responses against the resolved language
    contract **without enforcement** — mismatches are logged and recorded in
    telemetry, but no message is blocked or modified.

    Thread-safe.  One instance is typically held by the :class:`LanguageContract`
    façade for the life of a session.

    Parameters
    ----------
    session_id : str
        Hermes session identifier.
    recent_max : int
        Maximum number of recent observations to retain in the telemetry ring
        buffer.  Default: 50.
    """

    def __init__(self, session_id: str, *, recent_max: int = 50) -> None:
        self._session_id = session_id
        self._lock = threading.Lock()
        self._telemetry = LanguageTelemetry(session_id=session_id, _recent_max=recent_max)

    @property
    def telemetry(self) -> LanguageTelemetry:
        """Read-only snapshot of current telemetry counters."""
        with self._lock:
            return self._telemetry

    def observe(
        self,
        *,
        turn_id: str,
        assistant_text: str,
        contract_language: str | None,
        contract_mode: str,
        translation_payload: bool = False,
    ) -> TurnObservation:
        """Observe one assistant response turn and record the result.

        Parameters
        ----------
        turn_id : str
            Opaque turn identifier.
        assistant_text : str
            Full text of the assistant response (before any formatting).
        contract_language : str | None
            Resolved contract language for this turn (from
            ``LanguageContract.resolve_turn()``).
        contract_mode : str
            Current contract mode (``"user"``, ``"fixed"``, or ``"off"``).
        translation_payload : bool
            If ``True``, the entire response is treated as a translation payload
            (exempt from language detection).

        Returns
        -------
        TurnObservation
            The observation record for this turn.
        """
        # Fast-path: contract is off
        if contract_mode == "off":
            obs = TurnObservation(
                turn_id=turn_id,
                result="contract_off",
                contract_language=None,
                detected_language=None,
                prose_char_count=0,
            )
            with self._lock:
                self._telemetry.record(obs)
            return obs

        # Extract prose suitable for language detection
        prose = prose_for_detection(assistant_text, translation_payload=translation_payload)

        if prose is None:
            # All content is exempt
            obs = TurnObservation(
                turn_id=turn_id,
                result="no_prose",
                contract_language=contract_language,
                detected_language=None,
                prose_char_count=0,
            )
            with self._lock:
                self._telemetry.record(obs)
            return obs

        prose_chars = len(prose)

        # Detect language from prose
        detected = detect_language_from_text(prose)

        # Determine observation result
        if contract_language is None:
            # No resolved contract language yet — nothing to validate against
            result: ObservationResult = "no_contract_lang"
        elif detected is None:
            # Prose present but language undetermined (Latin-script; no heavy detector)
            result = "undetermined"
        elif detected == contract_language:
            result = "match"
        else:
            result = "mismatch"
            logger.info(
                "Language contract observation: mismatch on turn %s "
                "(contract=%r, detected=%r, prose_chars=%d) [observe-only]",
                turn_id,
                contract_language,
                detected,
                prose_chars,
            )

        obs = TurnObservation(
            turn_id=turn_id,
            result=result,
            contract_language=contract_language,
            detected_language=detected,
            prose_char_count=prose_chars,
        )

        with self._lock:
            self._telemetry.record(obs)

        return obs

    def reset(self) -> None:
        """Reset telemetry for a new session (same validator object)."""
        with self._lock:
            recent_max = self._telemetry._recent_max
            self._telemetry = LanguageTelemetry(
                session_id=self._session_id,
                _recent_max=recent_max,
            )
