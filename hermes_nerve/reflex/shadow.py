"""Fail-open provider-neutral shadowing around the authoritative Jev provider."""
from __future__ import annotations

import contextvars
import re
import threading
from pathlib import Path
from typing import Any

from .telemetry import append_shadow_record

# Shadow telemetry is sanitized: provider exception text may embed credentials
# (e.g. "auth failed for sk-..."), so secret-shaped tokens are redacted before
# any record is persisted.
_SECRET_TEXT_RE = re.compile(
    r"(sk-[A-Za-z0-9_\-]{6,}|Bearer\s+\S+|api[_-]?key\s*[=:]\s*\S+|[A-Za-z0-9_\-]{28,})",
    re.IGNORECASE)


def _sanitize_error(exc: BaseException) -> str:
    text = f"{type(exc).__name__}: {exc}"
    return _SECRET_TEXT_RE.sub("[REDACTED]", text)[:400]


class ShadowProvider:
    """Return primary decisions unchanged while measuring a shadow provider.

    By default the shadow call runs on a daemon thread so it does not add shadow-provider
    latency to the authoritative Jev path. Set ``asynchronous=False`` for paired
    benchmarks/tests that must know the record is durable before returning.
    """

    def __init__(self, primary: Any, shadow: Any, *, path: Path | None = None, asynchronous: bool = True) -> None:
        self.primary = primary
        self.shadow = shadow
        self.path = path
        self.asynchronous = bool(asynchronous)

    def _run_shadow(self, *, state: Any, questions: dict[str, dict[str, Any]], model: str | None, primary_response: Any) -> None:
        try:
            shadow_response = self.shadow.system_one(state=state, questions=questions, model=model)
        except Exception as exc:  # Shadowing must never affect the authoritative path.
            append_shadow_record(
                path=self.path,
                state=state,
                questions=questions,
                primary=primary_response,
                shadow=None,
                shadow_error=_sanitize_error(exc),
            )
            return
        append_shadow_record(
            path=self.path,
            state=state,
            questions=questions,
            primary=primary_response,
            shadow=shadow_response,
        )

    def system_one(self, *, state: Any, questions: dict[str, dict[str, Any]], model: str | None = None):
        primary_response = self.primary.system_one(state=state, questions=questions, model=model)
        if self.asynchronous:
            # Capture the caller's context and run the shadow under it so
            # profile-scoped secrets (Hermes ContextVars) survive the thread
            # hop (#35). Captured per call: each shadow runs under the scope
            # of the turn that spawned it.
            task_ctx = contextvars.copy_context()

            def _shadow_under_ctx() -> None:
                task_ctx.run(
                    self._run_shadow,
                    state=state,
                    questions=questions,
                    model=model,
                    primary_response=primary_response,
                )

            thread = threading.Thread(
                target=_shadow_under_ctx,
                name="hermes-reflex-shadow",
                daemon=True,
            )
            thread.start()
        else:
            self._run_shadow(state=state, questions=questions, model=model, primary_response=primary_response)
        return primary_response
