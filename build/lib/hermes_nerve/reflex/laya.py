"""Dependency-free HTTP client for a preloaded Laya System-1 sidecar."""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable

DEFAULT_BASE_URL = "http://127.0.0.1:8765"
DEFAULT_PATH = "/v1/systemone"
DEFAULT_MODEL = "convaiinnovations/laya-typed-decisions"
DEFAULT_TIMEOUT = 5.0


class LayaError(RuntimeError):
    pass


@dataclass(frozen=True)
class LayaResponse:
    model: str
    answers: dict[str, dict[str, Any]]
    usage: dict[str, Any]
    latency_ms: float
    request_id: str = ""
    provider: str = "Laya"
    transport: str = "laya-local-http"
    live_provider_call: bool = False


class LayaClient:
    """Talk to a fixed, preloaded Laya sidecar without importing torch in Hermes."""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        model: str | None = None,
        timeout: float | None = None,
        token: str | None = None,
        transport: Callable | None = None,
    ) -> None:
        self.base_url = str(base_url or os.getenv("HERMES_REFLEX_LAYA_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        if not self.base_url.startswith(("http://127.0.0.1", "http://localhost", "https://")):
            raise LayaError(
                "Laya sidecar must use loopback HTTP or HTTPS. "
                "For a remote/private host, terminate TLS and configure an https:// URL."
            )
        self.model = str(model or os.getenv("HERMES_REFLEX_LAYA_MODEL") or DEFAULT_MODEL).strip() or DEFAULT_MODEL
        raw_timeout = timeout if timeout is not None else os.getenv("HERMES_REFLEX_LAYA_TIMEOUT", str(DEFAULT_TIMEOUT))
        try:
            parsed_timeout = float(raw_timeout)
        except (TypeError, ValueError):
            parsed_timeout = DEFAULT_TIMEOUT
        self.timeout = min(120.0, max(0.25, parsed_timeout))
        self.token = str(token if token is not None else os.getenv("HERMES_REFLEX_LAYA_TOKEN") or "").strip()
        self._transport = transport or self._urllib_transport

    @staticmethod
    def _urllib_transport(url: str, headers: dict[str, str], body: bytes, timeout: float):
        request = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return int(response.status), response.read(), {str(k).lower(): str(v) for k, v in response.headers.items()}
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:500]
            raise LayaError(f"Laya sidecar returned HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise LayaError(f"Laya sidecar connection failed: {exc}") from exc

    def system_one(
        self,
        *,
        state: Any,
        questions: dict[str, dict[str, Any]],
        model: str | None = None,
    ) -> LayaResponse:
        if not isinstance(questions, dict) or not questions:
            raise LayaError("At least one question is required")
        requested_model = str(model or self.model)
        payload = {"state": state, "questions": questions, "model": requested_model}
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False, default=str).encode("utf-8")
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "hermes-jev-reflex/dev17",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        started = time.monotonic()
        raw_result = self._transport(self.base_url + DEFAULT_PATH, headers, body, self.timeout)
        latency_ms = (time.monotonic() - started) * 1000.0
        if not isinstance(raw_result, tuple) or len(raw_result) not in {2, 3}:
            raise LayaError("Laya transport returned an invalid response tuple")
        status, raw = raw_result[0], raw_result[1]
        if int(status) < 200 or int(status) >= 300:
            raise LayaError(f"Laya sidecar returned HTTP {status}: {bytes(raw).decode('utf-8', 'replace')[:500]}")
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError) as exc:
            raise LayaError("Laya sidecar returned invalid JSON") from exc
        if not isinstance(data, dict):
            raise LayaError("Laya sidecar returned a non-object response")
        answers = data.get("answers")
        if not isinstance(answers, dict):
            raise LayaError("Laya sidecar response is missing answers")
        response_model = str(data.get("model") or requested_model)
        if self.model and response_model != self.model:
            raise LayaError(f"Laya sidecar model mismatch: expected {self.model!r}, got {response_model!r}")
        usage = dict(data.get("usage")) if isinstance(data.get("usage"), dict) else {}
        try:
            usage.setdefault("inference_latency_ms", float(data.get("latency_ms")))
        except (TypeError, ValueError):
            pass
        return LayaResponse(
            model=response_model,
            answers=answers,
            usage=usage,
            latency_ms=latency_ms,
            request_id=str(data.get("id") or data.get("request_id") or ""),
            provider=str(data.get("provider") or "Laya"),
            transport=str(data.get("transport") or "laya-local-http"),
            live_provider_call=False,
        )
