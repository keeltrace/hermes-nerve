"""LanguageContract — session/turn language state for Hermes Nerve.

Phase 1: state model, modes, resolver, config, temporary exceptions, and
persistent control-plane/session state independent of compacted summaries.

Phase 2 additions: observe_response() wires the observe-only validator and
telemetry counters into the façade.  No enforcement occurs in phase 2.

Modes
-----
``user``   — language is determined by resolving the user's natural-language
             messages within the current session.  This is the default mode.
``fixed``  — language is locked to the operator-configured ``fixed_language``
             BCP-47 tag (e.g. ``"en"``, ``"es"``, ``"zh-CN"``).  User messages
             are still tracked but cannot override the fixed language.
``off``    — the contract is disabled; no language is asserted.

Resolution order (user mode, per turn)
---------------------------------------
1. Explicit temporary language exception active for this turn → use exception language.
2. Session-level language detected in an earlier turn of the same session → use it.
3. Language detected in the current turn's user-authored natural-language text → use it.
4. None — no language has been established yet.

Persistence
-----------
State is stored in ``<hermes_home>/nerve/language_contract.json`` via an
atomic write + backup pattern identical to ``profiles.py``, so it survives
process restarts and context compaction independently.

Security / PRIVATE note
-----------------------
Language tags themselves are not sensitive, but the session/turn message
excerpts used during detection are never persisted.  Only the resolved BCP-47
tag and its metadata are written to disk.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from .paths import hermes_home

__all__ = [
    "LanguageContractMode",
    "LanguageContractConfig",
    "TurnLanguageException",
    "SessionLanguageState",
    "LanguageContract",
    "resolve_turn_language",
]

logger = logging.getLogger("hermes_nerve.language_contract")

# ---------------------------------------------------------------------------
# Type aliases
# ---------------------------------------------------------------------------

LanguageContractMode = Literal["user", "fixed", "off"]

_VALID_MODES: frozenset[str] = frozenset({"user", "fixed", "off"})

# Loose BCP-47 tag validator: 2-3 alpha chars optionally followed by subtags.
_BCP47_RE = re.compile(r"^[a-zA-Z]{2,3}(?:-[a-zA-Z0-9]{1,8})*$")

# ---------------------------------------------------------------------------
# Natural-language detection helpers (heuristic, no heavy deps)
# ---------------------------------------------------------------------------

# Representative Unicode ranges keyed to BCP-47 primary language subtag.
_SCRIPT_RANGES: list[tuple[range, str]] = [
    (range(0x4E00, 0x9FFF + 1), "zh"),       # CJK Unified Ideographs (Chinese)
    (range(0x3040, 0x309F + 1), "ja"),        # Hiragana
    (range(0x30A0, 0x30FF + 1), "ja"),        # Katakana
    (range(0xAC00, 0xD7AF + 1), "ko"),        # Hangul syllables
    (range(0x0600, 0x06FF + 1), "ar"),        # Arabic
    (range(0x0400, 0x04FF + 1), "ru"),        # Cyrillic
    (range(0x0900, 0x097F + 1), "hi"),        # Devanagari
    (range(0x0E00, 0x0E7F + 1), "th"),        # Thai
    (range(0x0370, 0x03FF + 1), "el"),        # Greek
    (range(0x0590, 0x05FF + 1), "he"),        # Hebrew
]


def _script_language(text: str) -> str | None:
    """Detect a primary script language from *text* using Unicode block sampling.

    Returns a BCP-47 tag or ``None`` if no clear non-Latin script is detected.
    Only inspects the first 512 characters for performance; callers may pass
    longer strings safely.
    """
    counts: dict[str, int] = {}
    for ch in text[:512]:
        cp = ord(ch)
        for block, tag in _SCRIPT_RANGES:
            if cp in block:
                counts[tag] = counts.get(tag, 0) + 1
                break
    if not counts:
        return None
    dominant = max(counts, key=lambda t: counts[t])
    # Require at least 3 characters in the dominant script before asserting.
    if counts[dominant] < 3:
        return None
    return dominant


def detect_language_from_text(text: str) -> str | None:
    """Attempt to detect the primary language of *text*.

    Strategy (no external model required):
    1. Non-Latin script detection via Unicode block heuristics.
    2. Falls back to ``None`` — Latin-script languages (en, es, fr, de, …) are
       indistinguishable without a full n-gram model.  Callers should treat
       ``None`` as "undetermined" rather than "English".

    This is intentionally minimal: phase 2 may wire a real language identifier.
    """
    stripped = str(text or "").strip()
    if not stripped:
        return None
    return _script_language(stripped)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _validate_bcp47(tag: str | None) -> str | None:
    if tag is None:
        return None
    normalized = str(tag).strip()
    if not normalized:
        return None
    if not _BCP47_RE.match(normalized):
        raise ValueError(f"Invalid BCP-47 language tag: {tag!r}")
    return normalized


@dataclass(frozen=True)
class LanguageContractConfig:
    """Operator-authored configuration for the LanguageContract.

    Fields
    ------
    mode : ``user`` | ``fixed`` | ``off``
        Operating mode.  Defaults to ``user``.
    fixed_language : str | None
        Required when *mode* is ``fixed``.  Must be a valid BCP-47 tag.
    allow_user_exceptions : bool
        When ``True`` (default for ``fixed`` mode) an explicit in-session
        ``/language <tag>`` instruction from the user may install a temporary
        turn-scoped exception even in fixed mode.
    """

    mode: LanguageContractMode = "user"
    fixed_language: str | None = None
    allow_user_exceptions: bool = True

    def __post_init__(self) -> None:
        if self.mode not in _VALID_MODES:
            raise ValueError(f"Unknown LanguageContract mode: {self.mode!r}")
        if self.mode == "fixed":
            if not self.fixed_language:
                raise ValueError(
                    "LanguageContractConfig: fixed_language is required when mode='fixed'"
                )
            # Validate tag via __init__ sidestep — frozen, so use object.__setattr__.
            validated = _validate_bcp47(self.fixed_language)
            object.__setattr__(self, "fixed_language", validated)
        elif self.fixed_language is not None:
            # Validate anyway so stored value is always clean.
            validated = _validate_bcp47(self.fixed_language)
            object.__setattr__(self, "fixed_language", validated)

    def as_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "fixed_language": self.fixed_language,
            "allow_user_exceptions": self.allow_user_exceptions,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "LanguageContractConfig":
        return cls(
            mode=data.get("mode", "user"),
            fixed_language=data.get("fixed_language"),
            allow_user_exceptions=bool(data.get("allow_user_exceptions", True)),
        )


@dataclass
class TurnLanguageException:
    """A temporary language override active for exactly one turn.

    Set by an explicit user instruction (e.g. ``/language es``).  Expires
    after the turn it was applied to; it does not alter the persistent session
    language.

    Attributes
    ----------
    language : str
        BCP-47 tag of the temporary language.
    turn_id : str
        Opaque identifier for the turn this exception applies to.
    reason : str
        Short human-readable reason (e.g. ``"user_instruction"``).
    created_at : str
        ISO-8601 UTC timestamp.
    """

    language: str
    turn_id: str
    reason: str = "user_instruction"
    created_at: str = field(default_factory=_utc_now)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TurnLanguageException":
        return cls(
            language=data["language"],
            turn_id=data["turn_id"],
            reason=data.get("reason", "user_instruction"),
            created_at=data.get("created_at", _utc_now()),
        )


@dataclass
class SessionLanguageState:
    """Persistent session-level language state for one Hermes session.

    This is the unit written to disk.  It captures the operator config in
    effect at session start (so a config change mid-session does not silently
    alter state) plus the resolved language history for the session.

    Attributes
    ----------
    session_id : str
        Hermes session identifier.
    config_snapshot : dict
        Snapshot of ``LanguageContractConfig.as_dict()`` at session start.
    resolved_language : str | None
        Current resolved language tag for the session (``user`` mode only).
        ``None`` until at least one user turn with a detected language.
    detection_turn_id : str | None
        Turn ID on which *resolved_language* was first established.
    pending_exception : TurnLanguageException | None
        Active temporary language exception, if any.
    created_at : str
        ISO-8601 UTC timestamp of session creation.
    updated_at : str
        ISO-8601 UTC timestamp of last mutation.
    version : int
        Schema version (currently 1).
    """

    session_id: str
    config_snapshot: dict[str, Any] = field(default_factory=dict)
    resolved_language: str | None = None
    detection_turn_id: str | None = None
    pending_exception: TurnLanguageException | None = None
    created_at: str = field(default_factory=_utc_now)
    updated_at: str = field(default_factory=_utc_now)
    version: int = 1

    def as_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "version": self.version,
            "session_id": self.session_id,
            "config_snapshot": self.config_snapshot,
            "resolved_language": self.resolved_language,
            "detection_turn_id": self.detection_turn_id,
            "pending_exception": self.pending_exception.as_dict() if self.pending_exception else None,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SessionLanguageState":
        exc_data = data.get("pending_exception")
        return cls(
            session_id=data["session_id"],
            config_snapshot=data.get("config_snapshot", {}),
            resolved_language=data.get("resolved_language"),
            detection_turn_id=data.get("detection_turn_id"),
            pending_exception=TurnLanguageException.from_dict(exc_data) if exc_data else None,
            created_at=data.get("created_at", _utc_now()),
            updated_at=data.get("updated_at", _utc_now()),
            version=data.get("version", 1),
        )


# ---------------------------------------------------------------------------
# Turn-level resolution (stateless helper)
# ---------------------------------------------------------------------------

def resolve_turn_language(
    *,
    config: LanguageContractConfig,
    session_state: SessionLanguageState | None,
    turn_id: str,
    user_text: str | None = None,
) -> str | None:
    """Resolve the effective language for one turn.

    This is a **pure function** — it does not mutate any state.  The caller is
    responsible for persisting resolved state via ``LanguageContract.record_turn``.

    Resolution order
    ----------------
    1. If mode is ``off`` → ``None``.
    2. If mode is ``fixed`` → ``config.fixed_language`` (always).
    3. mode is ``user``:
       a. Active temporary exception matching *turn_id* → exception language.
       b. Session-level resolved language (established in a prior turn) → it.
       c. Language detected from *user_text* (current turn) → detected tag.
       d. ``None`` — no language established yet.

    Parameters
    ----------
    config : LanguageContractConfig
        Operator configuration.
    session_state : SessionLanguageState | None
        Current persistent session state; ``None`` if this is the first turn.
    turn_id : str
        Opaque identifier for the current turn.
    user_text : str | None
        Raw user-authored natural-language text for the current turn.
        Must be user-authored only — never include assistant or system text.

    Returns
    -------
    str | None
        Resolved BCP-47 language tag, or ``None`` if undetermined.
    """
    if config.mode == "off":
        return None

    if config.mode == "fixed":
        return config.fixed_language

    # mode == "user"
    if session_state is not None:
        exc = session_state.pending_exception
        if exc is not None and exc.turn_id == turn_id:
            return exc.language

        if session_state.resolved_language is not None:
            return session_state.resolved_language

    if user_text:
        detected = detect_language_from_text(user_text)
        if detected:
            return detected

    return None


# ---------------------------------------------------------------------------
# Persistence helpers (mirrors profiles.py atomic-write pattern)
# ---------------------------------------------------------------------------

def _contract_state_path(home: str | Path | None = None) -> Path:
    return (Path(home).expanduser() if home is not None else hermes_home()) / "nerve" / "language_contract.json"


@contextmanager
def _flock(path: Path):
    with path.open("a+b") as stream:
        if os.name == "nt":
            import msvcrt
            stream.seek(0); stream.write(b"\0"); stream.flush(); stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if os.name == "nt":
                stream.seek(0); msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _atomic_write(path: Path, payload: bytes) -> None:
    fd, temporary = tempfile.mkstemp(prefix="." + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            try:
                os.unlink(temporary)
            except OSError:
                pass


def _validate_state_doc(data: Any) -> dict:
    if not isinstance(data, dict):
        raise ValueError("Language contract state must be a JSON object")
    if data.get("version") != 1:
        raise ValueError(f"Unsupported language contract state version: {data.get('version')!r}")
    if "session_id" not in data or not data["session_id"]:
        raise ValueError("Language contract state missing session_id")
    return data


def load_session_state(
    session_id: str,
    *,
    home: str | Path | None = None,
) -> SessionLanguageState | None:
    """Load persisted session language state; return ``None`` if not found.

    Silently discards corrupt or mismatched state and returns ``None`` so the
    caller gets a clean slate rather than a hard failure.
    """
    path = _contract_state_path(home)
    try:
        with path.open(encoding="utf-8") as stream:
            data = json.load(stream)
        _validate_state_doc(data)
        state = SessionLanguageState.from_dict(data)
        if state.session_id != session_id:
            # Stale state from a different session — start fresh.
            return None
        return state
    except FileNotFoundError:
        return None
    except (json.JSONDecodeError, ValueError, KeyError, OSError) as exc:
        logger.warning("Discarding invalid language contract state at %s: %s", path, exc)
        return None


def save_session_state(
    state: SessionLanguageState,
    *,
    home: str | Path | None = None,
) -> Path:
    """Persist session language state atomically."""
    payload = (
        json.dumps(state.as_dict(), indent=2, sort_keys=True, allow_nan=False) + "\n"
    ).encode()
    path = _contract_state_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _flock(path.with_suffix(".lock")):
        _atomic_write(path, payload)
        if os.name != "nt":
            fd = os.open(str(path.parent), os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
    return path


def clear_session_state(*, home: str | Path | None = None) -> None:
    """Remove persisted session language state (e.g. at session end)."""
    path = _contract_state_path(home)
    try:
        path.unlink()
    except FileNotFoundError:
        pass


# ---------------------------------------------------------------------------
# LanguageContract — high-level façade
# ---------------------------------------------------------------------------

class LanguageContract:
    """High-level façade for language contract state management.

    Thread-safe.  One instance per Hermes session.  Owns all mutations to
    ``SessionLanguageState`` and handles persistence.

    Parameters
    ----------
    config : LanguageContractConfig
        Operator configuration.
    session_id : str
        Hermes session identifier.  Must be stable for the life of the session.
    home : str | Path | None
        Override for the Hermes home directory (mainly for testing).

    Example usage (no enforcement yet)::

        contract = LanguageContract(
            config=LanguageContractConfig(mode="user"),
            session_id="sess-abc",
        )
        lang = contract.resolve_turn(turn_id="turn-1", user_text="Hola, ¿cómo estás?")
        # lang is None here (Latin-script; no heavy detector in phase 1)
        contract.record_turn(turn_id="turn-1", user_text="Hola, ¿cómo estás?")
    """

    def __init__(
        self,
        config: LanguageContractConfig,
        session_id: str,
        *,
        home: str | Path | None = None,
    ) -> None:
        self._config = config
        self._session_id = str(session_id)
        self._home = home
        self._lock = threading.RLock()
        # Load persisted state or start fresh.
        self._state: SessionLanguageState = (
            load_session_state(self._session_id, home=home)
            or SessionLanguageState(
                session_id=self._session_id,
                config_snapshot=config.as_dict(),
            )
        )
        # Phase 2: observe-only validator (lazy import to avoid circular deps)
        from .language_telemetry import ObserveOnlyValidator  # noqa: PLC0415
        self._observer: "ObserveOnlyValidator" = ObserveOnlyValidator(self._session_id)

    # ------------------------------------------------------------------
    # Read-only properties
    # ------------------------------------------------------------------

    @property
    def config(self) -> LanguageContractConfig:
        return self._config

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def session_state(self) -> SessionLanguageState:
        with self._lock:
            return self._state

    @property
    def resolved_language(self) -> str | None:
        """Session-level resolved language (user mode) or fixed language (fixed mode)."""
        with self._lock:
            if self._config.mode == "off":
                return None
            if self._config.mode == "fixed":
                return self._config.fixed_language
            return self._state.resolved_language

    # ------------------------------------------------------------------
    # Resolution
    # ------------------------------------------------------------------

    def resolve_turn(
        self,
        *,
        turn_id: str,
        user_text: str | None = None,
    ) -> str | None:
        """Return the effective language for *turn_id* without mutating state."""
        with self._lock:
            return resolve_turn_language(
                config=self._config,
                session_state=self._state,
                turn_id=turn_id,
                user_text=user_text,
            )

    # ------------------------------------------------------------------
    # Mutations
    # ------------------------------------------------------------------

    def record_turn(
        self,
        *,
        turn_id: str,
        user_text: str | None = None,
        persist: bool = True,
    ) -> str | None:
        """Process a completed turn and update session state.

        In ``user`` mode this may advance ``resolved_language`` if a language
        is detected in *user_text* and none has been established yet.

        The temporary exception (if any) is consumed only when its *turn_id*
        matches the current *turn_id*; it is NOT consumed for mismatched turns.

        Parameters
        ----------
        turn_id : str
            Opaque turn identifier.
        user_text : str | None
            User-authored natural-language text.  Must NOT include assistant
            or system messages.
        persist : bool
            If ``True`` (default), flush updated state to disk.

        Returns
        -------
        str | None
            The effective language that was in effect for this turn.
        """
        with self._lock:
            lang = resolve_turn_language(
                config=self._config,
                session_state=self._state,
                turn_id=turn_id,
                user_text=user_text,
            )

            # Consume temporary exception if it matched this turn.
            exception_consumed = False
            if (
                self._state.pending_exception is not None
                and self._state.pending_exception.turn_id == turn_id
            ):
                exception_consumed = True

            # In user mode, update resolved_language if not yet set.
            new_resolved = self._state.resolved_language
            new_detection_turn = self._state.detection_turn_id
            if self._config.mode == "user" and new_resolved is None and lang is not None:
                # Only promote a lang that came from detection (not from an exception).
                if not exception_consumed:
                    new_resolved = lang
                    new_detection_turn = turn_id
                elif user_text:
                    # Exception was active, but we still detect for future turns.
                    detected = detect_language_from_text(user_text)
                    if detected:
                        new_resolved = detected
                        new_detection_turn = turn_id

            self._state = SessionLanguageState(
                session_id=self._session_id,
                config_snapshot=self._state.config_snapshot,
                resolved_language=new_resolved,
                detection_turn_id=new_detection_turn,
                pending_exception=None if exception_consumed else self._state.pending_exception,
                created_at=self._state.created_at,
                updated_at=_utc_now(),
                version=self._state.version,
            )

            if persist:
                try:
                    save_session_state(self._state, home=self._home)
                except OSError as exc:
                    logger.warning("Could not persist language contract state: %s", exc)

            return lang

    def set_temporary_exception(
        self,
        *,
        turn_id: str,
        language: str,
        reason: str = "user_instruction",
        persist: bool = True,
    ) -> TurnLanguageException:
        """Install a temporary language exception for *turn_id*.

        The exception is active only while the session's pending exception
        matches *turn_id*.  ``record_turn`` automatically clears it once the
        turn is processed.

        In ``fixed`` mode this is only allowed when
        ``config.allow_user_exceptions`` is ``True``; otherwise raises
        ``PermissionError``.

        Parameters
        ----------
        turn_id : str
            Turn the exception applies to.
        language : str
            BCP-47 tag for the exception language.
        reason : str
            Descriptive reason (default ``"user_instruction"``).
        persist : bool
            If ``True``, flush state to disk.

        Returns
        -------
        TurnLanguageException
        """
        validated = _validate_bcp47(language)
        if validated is None:
            raise ValueError(f"Invalid BCP-47 language tag: {language!r}")

        with self._lock:
            if self._config.mode == "fixed" and not self._config.allow_user_exceptions:
                raise PermissionError(
                    "Temporary language exceptions are disabled in fixed mode "
                    "(allow_user_exceptions=False)"
                )
            if self._config.mode == "off":
                raise PermissionError(
                    "Temporary language exceptions cannot be set when mode='off'"
                )

            exc = TurnLanguageException(
                language=validated,
                turn_id=turn_id,
                reason=reason,
            )
            self._state = SessionLanguageState(
                session_id=self._session_id,
                config_snapshot=self._state.config_snapshot,
                resolved_language=self._state.resolved_language,
                detection_turn_id=self._state.detection_turn_id,
                pending_exception=exc,
                created_at=self._state.created_at,
                updated_at=_utc_now(),
                version=self._state.version,
            )
            if persist:
                try:
                    save_session_state(self._state, home=self._home)
                except OSError as exc_io:
                    logger.warning(
                        "Could not persist language contract exception: %s", exc_io
                    )
            return exc

    def clear_exception(self, *, persist: bool = True) -> None:
        """Discard any pending temporary exception (e.g. on session reset)."""
        with self._lock:
            if self._state.pending_exception is None:
                return
            self._state = SessionLanguageState(
                session_id=self._session_id,
                config_snapshot=self._state.config_snapshot,
                resolved_language=self._state.resolved_language,
                detection_turn_id=self._state.detection_turn_id,
                pending_exception=None,
                created_at=self._state.created_at,
                updated_at=_utc_now(),
                version=self._state.version,
            )
            if persist:
                try:
                    save_session_state(self._state, home=self._home)
                except OSError as exc:
                    logger.warning(
                        "Could not persist language contract after exception clear: %s", exc
                    )

    def reset_session(self, *, persist: bool = True) -> None:
        """Reset language state for a new session (same contract object, new session start).

        Clears resolved language, exceptions, re-snapshots the current config,
        and resets telemetry counters.
        """
        with self._lock:
            self._state = SessionLanguageState(
                session_id=self._session_id,
                config_snapshot=self._config.as_dict(),
            )
            self._observer.reset()
            if persist:
                try:
                    save_session_state(self._state, home=self._home)
                except OSError as exc:
                    logger.warning(
                        "Could not persist language contract on session reset: %s", exc
                    )

    # ------------------------------------------------------------------
    # Phase 2: observe-only validation and telemetry
    # ------------------------------------------------------------------

    def observe_response(
        self,
        *,
        turn_id: str,
        assistant_text: str,
        translation_payload: bool = False,
    ) -> "Any":
        """Observe one assistant response for language contract compliance.

        This is **observe-only** — no enforcement, no blocking.  The resolved
        contract language for *turn_id* is taken from the current session state
        (i.e. call ``record_turn`` for the user side first, then call this).

        Parameters
        ----------
        turn_id : str
            Opaque turn identifier.
        assistant_text : str
            Full text of the assistant response to observe.
        translation_payload : bool
            If ``True``, marks the entire response as a translation payload
            (fully exempt from language detection).

        Returns
        -------
        TurnObservation
            The observation record.  Inspect ``result`` for the outcome.
            ``"mismatch"`` is informational only in phase 2.
        """
        with self._lock:
            contract_lang = self.resolved_language  # already thread-safe via property
            mode = self._config.mode
        return self._observer.observe(
            turn_id=turn_id,
            assistant_text=assistant_text,
            contract_language=contract_lang,
            contract_mode=mode,
            translation_payload=translation_payload,
        )

    @property
    def telemetry(self) -> "Any":
        """Current session language telemetry counters (LanguageTelemetry)."""
        return self._observer.telemetry

    def as_dict(self) -> dict[str, Any]:
        """Snapshot of the full contract state for diagnostics."""
        with self._lock:
            return {
                "config": self._config.as_dict(),
                "session_state": self._state.as_dict(),
                "telemetry": self._observer.telemetry.as_dict(),
            }
