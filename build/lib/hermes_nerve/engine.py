"""Provider-independent decision runtime. Jev is the first backend."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
import math
from typing import Any, Protocol

from .client import JevClient, JevError
from .privacy import redact
from .receipts import write_receipt
from .provenance import execution_provenance, result_provenance


class DecisionProvider(Protocol):
    def system_one(self, *, state: Any, questions: dict[str, dict[str, Any]], model: str | None = None): ...


@dataclass(frozen=True)
class DecisionResult:
    value: str
    confidence: float
    probabilities: dict[str, float]
    model: str
    latency_ms: float
    contract: str
    usage: dict[str, Any] = field(default_factory=dict)
    request_id: str = ""
    provider: str = ""
    transport: str = "openrouter-decisions"
    live_provider_call: bool = True
    receipt_id: str = ""
    provenance_status: str = "UNVERIFIED"
    provenance: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        provenance = self.provenance or result_provenance(
            live_provider_call=self.live_provider_call,
            request_id=self.request_id,
            receipt_id=self.receipt_id,
            provider=self.provider,
            transport=self.transport,
            model=self.model,
            contract=self.contract,
        )
        status = provenance.get("provenance_status") or self.provenance_status
        return {
            "value": self.value,
            "confidence": self.confidence,
            "probabilities": self.probabilities,
            "model": self.model,
            "latency_ms": round(self.latency_ms, 3),
            "contract": self.contract,
            "usage": self.usage,
            "request_id": self.request_id,
            "receipt_id": self.receipt_id,
            "provider": self.provider,
            "provenance_status": status,
            "provenance": provenance,
            "execution": execution_provenance(
                live_provider_call=self.live_provider_call,
                transport=self.transport,
                request_id=self.request_id,
                receipt_id=self.receipt_id,
            ),
        }


class DecisionEngine:
    def __init__(self, provider: DecisionProvider | None = None) -> None:
        if provider is None:
            from .reflex.config import get_provider
            provider = get_provider()
        self.provider = provider

    def _system_one(
        self,
        *,
        state: Any,
        questions: dict[str, dict[str, Any]],
        model: str | None = None,
        images: list[str] | tuple[str, ...] | None = None,
    ):
        if images:
            if not bool(getattr(self.provider, "supports_images", False)):
                raise ValueError("configured decision provider does not support image input")
            return self.provider.system_one(
                state=state,
                questions=questions,
                model=model,
                images=list(images),
            )
        return self.provider.system_one(state=state, questions=questions, model=model)

    @staticmethod
    def _validate_questions(questions: dict[str, Any]) -> dict[str, dict[str, Any]]:
        if not isinstance(questions, dict) or not questions:
            raise ValueError("questions must be a non-empty object")
        if len(questions) > 16:
            raise ValueError("questions may contain at most 16 typed questions per request")

        normalized: dict[str, dict[str, Any]] = {}
        for raw_name, raw_question in questions.items():
            name = str(raw_name).strip()
            if not name:
                raise ValueError("question names must be non-empty")
            if not isinstance(raw_question, dict):
                raise ValueError(f"question {name!r} must be an object")
            q = dict(raw_question)
            qtype = str(q.get("type") or "").strip().lower()
            if qtype not in {"noul", "choice", "score"}:
                raise ValueError(f"question {name!r} has unsupported type {qtype!r}")
            q["type"] = qtype

            if "instructions" not in q or q["instructions"] is None:
                raise ValueError(f"question {name!r} requires instructions")
            if not isinstance(q["instructions"], (str, dict, list, tuple)):
                raise ValueError(f"question {name!r} instructions must be JSON-compatible text/context")

            criteria = q.get("criteria")
            if qtype == "choice":
                if not isinstance(criteria, dict) or len(criteria) < 2:
                    raise ValueError(f"choice question {name!r} requires at least two criteria labels")
            elif qtype == "score":
                if not isinstance(criteria, (list, tuple)) or len(criteria) < 2:
                    raise ValueError(f"score question {name!r} requires an ordered criteria list with at least two entries")
            elif "criteria" in q:
                if not isinstance(criteria, dict):
                    raise ValueError(f"noul question {name!r} criteria must be an object when provided")
                if set(criteria) != {"true", "false"}:
                    raise ValueError(f"noul question {name!r} criteria must contain exactly 'true' and 'false' keys")
                for label in ("true", "false"):
                    if not isinstance(criteria[label], (str, dict, list, tuple)):
                        raise ValueError(
                            f"noul question {name!r} criteria.{label} must be JSON-compatible text/context"
                        )
            normalized[name] = q
        return normalized

    def assess(
        self,
        *,
        state: Any,
        questions: dict[str, Any],
        contract: str = "assess/v1",
        images: list[str] | tuple[str, ...] | None = None,
    ) -> dict[str, Any]:
        """Ask one or more native Jev typed questions in a single request."""
        safe_state = redact(state)
        safe_questions = redact(self._validate_questions(questions))
        response = self._system_one(state=safe_state, questions=safe_questions, images=images)
        if not isinstance(getattr(response, "answers", None), dict):
            raise JevError("provider response answers must be an object")
        missing = [name for name in safe_questions if name not in response.answers]
        if missing:
            raise JevError(f"provider response is missing answers for: {', '.join(missing)}")
        transport = getattr(response, "transport", "openrouter-decisions")
        live_provider_call = bool(getattr(response, "live_provider_call", True))
        result = {
            "answers": response.answers,
            "model": response.model,
            "latency_ms": round(response.latency_ms, 3),
            "contract": contract,
            "usage": response.usage,
            "request_id": response.request_id,
            "provider": response.provider,
            "execution": execution_provenance(
                live_provider_call=live_provider_call,
                transport=transport,
                request_id=response.request_id,
            ),
        }
        receipt = write_receipt(contract=contract, state=safe_state, result=result, model=response.model, latency_ms=response.latency_ms)
        return dict(receipt["result"])

    def decide(
        self,
        *,
        state: Any,
        instructions: str,
        choices: list[str],
        criteria: dict[str, Any] | None = None,
        contract: str = "decision/v1",
        images: list[str] | tuple[str, ...] | None = None,
    ) -> DecisionResult:
        labels = [str(item).strip() for item in choices if str(item).strip()]
        if len(labels) < 2 or len(set(labels)) != len(labels):
            raise ValueError("choices must contain at least two unique non-empty labels")
        safe_state = redact(state)
        mapped = {label: (criteria or {}).get(label) for label in labels}
        response = self._system_one(
            state=safe_state,
            questions={"decision": {"type": "choice", "instructions": instructions, "criteria": mapped}},
            images=images,
        )
        if not isinstance(getattr(response, "answers", None), dict):
            raise JevError("provider response answers must be an object")
        answer = response.answers.get("decision") or {}
        if answer is None:
            raise JevError("provider response is missing answer for 'decision'")
        if not isinstance(answer, dict):
            raise JevError("provider response answer for 'decision' must be an object")
        value = str(answer.get("choice") or "")
        if not value.strip():
            raise JevError("provider response is missing a choice")
        raw_probabilities = answer.get("probabilities") or {}
        if not isinstance(raw_probabilities, dict):
            raise JevError("provider response probabilities must be an object")
        try:
            probabilities = {str(k): float(v) for k, v in raw_probabilities.items()}
        except (TypeError, ValueError):
            raise JevError("provider response probabilities must be numeric")
        if value not in mapped:
            raise ValueError(f"provider returned out-of-contract choice: {value!r}")
        try:
            confidence = float(answer.get("confidence", probabilities.get(value, 0.0)))
        except (TypeError, ValueError):
            raise JevError("provider response confidence must be numeric")
        if not math.isfinite(confidence) or any(not math.isfinite(p) for p in probabilities.values()):
            raise JevError("provider response confidence and probabilities must be finite")
        result = DecisionResult(
            value,
            confidence,
            probabilities,
            response.model,
            response.latency_ms,
            contract,
            usage=response.usage,
            request_id=response.request_id,
            provider=response.provider,
            transport=getattr(response, "transport", "openrouter-decisions"),
            live_provider_call=bool(getattr(response, "live_provider_call", True)),
        )
        receipt = write_receipt(contract=contract, state=safe_state, result=result.as_dict(), model=response.model, latency_ms=response.latency_ms)
        stored = receipt["result"]
        return replace(
            result,
            receipt_id=str(receipt.get("receipt_id") or stored.get("receipt_id") or ""),
            provenance_status=str(stored.get("provenance_status") or "UNVERIFIED"),
            provenance=dict(stored.get("provenance") or {}),
        )

    def rank(
        self,
        *,
        state: Any,
        instructions: str,
        items: dict[str, Any],
        contract: str = "rank/v1",
        images: list[str] | tuple[str, ...] | None = None,
    ) -> dict[str, Any]:
        result = self.decide(
            state=state,
            instructions=instructions,
            choices=list(items),
            criteria=items,
            contract=contract,
            images=images,
        )
        ranking = sorted(result.probabilities.items(), key=lambda pair: (-pair[1], pair[0]))
        return {**result.as_dict(), "ranking": [{"label": label, "probability": prob} for label, prob in ranking]}

    def verify(
        self,
        *,
        state: Any,
        instructions: str,
        contract: str = "verify/v1",
        images: list[str] | tuple[str, ...] | None = None,
    ) -> DecisionResult:
        return self.decide(
            state=state,
            instructions=instructions,
            choices=["PASS", "RETRY", "REPLAN", "ESCALATE"],
            criteria={
                "PASS": "Evidence satisfies the requested outcome with no material unresolved issue.",
                "RETRY": "The approach is valid but execution should be tried again with the same plan.",
                "REPLAN": "The current approach is insufficient; change the plan before trying again.",
                "ESCALATE": "Human judgment, permission, missing information, or an external dependency is required.",
            },
            contract=contract,
            images=images,
        )
