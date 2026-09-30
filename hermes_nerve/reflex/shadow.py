"""Fail-open Laya shadowing around the current authoritative Jev provider."""
from __future__ import annotations

import contextvars
import threading
from pathlib import Path
from typing import Any

from .telemetry import append_shadow_record


class ShadowProvider:
    """Return primary decisions unchanged while measuring a shadow provider.

    By default the shadow call runs on a daemon thread so it does not add Laya
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
                shadow_error=f"{type(exc).__name__}: {exc}",
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
                name="hermes-reflex-laya-shadow",
                daemon=True,
            )
            thread.start()
        else:
            self._run_shadow(state=state, questions=questions, model=model, primary_response=primary_response)
        return primary_response
