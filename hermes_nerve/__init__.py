"""Nerve decision runtime package."""
from .client import JevClient, JevError, JevResponse
from .engine import DecisionEngine, DecisionResult
from .language_contract import (
    LanguageContract,
    LanguageContractConfig,
    LanguageContractMode,
    SessionLanguageState,
    TurnLanguageException,
    detect_language_from_text,
    resolve_turn_language,
)
from .language_detector import (
    ExemptionCategory,
    TextSegment,
    classify_text_segments,
    extract_prose_segments,
    is_exempt,
    prose_for_detection,
)
from .language_telemetry import (
    LanguageTelemetry,
    ObservationResult,
    ObserveOnlyValidator,
    TurnObservation,
)
from .provenance import VERSION
from .reflex import LayaClient, LayaError, LayaResponse, OpenJevClient, OpenJevError, OpenJevResponse, ShadowProvider
try:
    from .context_engine import NerveContextEngine
except Exception:
    NerveContextEngine = None

__all__ = [
    "DecisionEngine", "DecisionResult", "JevClient", "JevError", "JevResponse",
    "LanguageContract", "LanguageContractConfig", "LanguageContractMode",
    "SessionLanguageState", "TurnLanguageException",
    "detect_language_from_text", "resolve_turn_language",
    # phase 2 detector / telemetry
    "ExemptionCategory", "TextSegment",
    "classify_text_segments", "extract_prose_segments", "is_exempt", "prose_for_detection",
    "LanguageTelemetry", "ObservationResult", "ObserveOnlyValidator", "TurnObservation",
    "LayaClient", "LayaError", "LayaResponse", "OpenJevClient", "OpenJevError", "OpenJevResponse", "ShadowProvider", "NerveContextEngine", "VERSION",
]
