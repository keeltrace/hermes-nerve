"""Generic dependency-free client for Jev-compatible /v1/systemone servers.

This is the provider-neutral seam for local decision runtimes such as
llama.cpp. D1 is exposed as a preset, not as a special wire protocol.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable

DEFAULT_BASE_URL = "http://127.0.0.1:8080"
DEFAULT_PATH = "/v1/systemone"
DEFAULT_TIMEOUT = 5.0
DEFAULT_MODEL = ""
D1_DEFAULT_MODEL = "LiquidAI/d1-3B"


class SystemOneHTTPError(RuntimeError):
    pass


@dataclass(frozen=True)
class SystemOneHTTPResponse:
    model: str
    answers: dict[str, dict[str, Any]]
    usage: dict[str, Any]
    latency_ms: float
    request_id: str = ""
    provider: str = "SystemOne"
    transport: str = "systemone-local-http"
    live_provider_call: bool = False


class SystemOneHTTPClient:
    """Talk to a Jev-compatible System One endpoint without runtime dependencies."""

    supports_images = True

    def __init__(
        self,
        *,
        base_url: str | None = None,
        path: str | None = None,
        model: str | None = None,
        timeout: float | None = None,
        token: str | None = None,
        provider_name: str | None = None,
        transport_name: str | None = None,
        expected_identity: str | None = None,
        transport: Callable | None = None,
    ) -> None:
        self.base_url = str(
            base_url or os.getenv("HERMES_REFLEX_SYSTEMONE_BASE_URL") or DEFAULT_BASE_URL
        ).rstrip("/")
        if not self.base_url.startswith(("http://127.0.0.1", "http://localhost", "https://")):
            raise SystemOneHTTPError(
                "System One endpoint must use loopback HTTP or HTTPS. "
                "For a remote/private host, use an SSH tunnel or terminate TLS."
            )
        self.path = str(path or os.getenv("HERMES_REFLEX_SYSTEMONE_PATH") or DEFAULT_PATH).strip()
        if not self.path.startswith("/") or "://" in self.path:
            raise SystemOneHTTPError("System One path must be an absolute URL path such as /v1/systemone")

        env_model = os.getenv("HERMES_REFLEX_SYSTEMONE_MODEL")
        self.model = str(model if model is not None else (env_model or DEFAULT_MODEL)).strip()
        raw_timeout = timeout if timeout is not None else os.getenv(
            "HERMES_REFLEX_SYSTEMONE_TIMEOUT", str(DEFAULT_TIMEOUT)
        )
        try:
            parsed_timeout = float(raw_timeout)
        except (TypeError, ValueError):
            parsed_timeout = DEFAULT_TIMEOUT
        self.timeout = min(120.0, max(0.25, parsed_timeout))
        self.token = str(
            token if token is not None else os.getenv("HERMES_REFLEX_SYSTEMONE_TOKEN") or ""
        ).strip()
        self.provider_name = str(
            provider_name
            if provider_name is not None
            else os.getenv("HERMES_REFLEX_SYSTEMONE_PROVIDER") or "SystemOne"
        ).strip() or "SystemOne"
        self.transport_name = str(transport_name or "systemone-local-http").strip() or "systemone-local-http"
        self.expected_identity = str(
            expected_identity
            if expected_identity is not None
            else os.getenv("HERMES_REFLEX_SYSTEMONE_EXPECTED_IDENTITY") or ""
        ).strip()
        self._transport = transport or self._urllib_transport

    @staticmethod
    def _urllib_transport(url: str, headers: dict[str, str], body: bytes, timeout: float):
        request = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return (
                    int(response.status),
                    response.read(),
                    {str(k).lower(): str(v) for k, v in response.headers.items()},
                )
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:500]
            raise SystemOneHTTPError(f"System One endpoint returned HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise SystemOneHTTPError(f"System One endpoint connection failed: {exc}") from exc

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "hermes-nerve/systemone",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    # A local D1/Laya sidecar serializes requests, so a decision issued while another
    # session holds the server can legitimately exceed the per-request timeout. Retrying
    # once is what keeps a transient queue delay from being reported as
    # "provider-unavailable". Retries are deliberately narrow: only a transport-level
    # failure (connection refused / read timeout / tunnel blip) is retried, never an
    # HTTP status or a schema error, because those are deterministic and would only
    # multiply latency. There is no fallback to any hosted/paid provider here.
    _TRANSIENT_RETRY_ATTEMPTS = 2

    def _transport_with_retry(self, url: str, headers: dict[str, str], body: bytes) -> tuple:
        last_error: Exception | None = None
        for attempt in range(self._TRANSIENT_RETRY_ATTEMPTS):
            try:
                return self._transport(url, headers, body, self.timeout)
            except SystemOneHTTPError as exc:
                message = str(exc)
                # "connection failed" is the transport-level branch raised for
                # URLError/TimeoutError/OSError. HTTP status and payload errors carry
                # their own distinct messages and must not be retried.
                if "connection failed" not in message:
                    raise
                last_error = exc
                if attempt + 1 < self._TRANSIENT_RETRY_ATTEMPTS:
                    time.sleep(0.25 * (attempt + 1))
        assert last_error is not None
        raise last_error

    def system_one(
        self,
        *,
        state: Any,
        questions: dict[str, dict[str, Any]],
        model: str | None = None,
        images: list[str] | tuple[str, ...] | None = None,
    ) -> SystemOneHTTPResponse:
        if not isinstance(questions, dict) or not questions:
            raise SystemOneHTTPError("At least one question is required")

        requested_model = str(model if model is not None else self.model).strip()
        payload: dict[str, Any] = {"state": state, "questions": questions}
        if requested_model:
            payload["model"] = requested_model
        if images:
            normalized_images = [str(item).strip() for item in images if str(item).strip()]
            if normalized_images:
                payload["images"] = normalized_images

        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False, default=str).encode("utf-8")
        started = time.monotonic()
        raw_result = self._transport_with_retry(self.base_url + self.path, self._headers(), body)
        latency_ms = (time.monotonic() - started) * 1000.0
        if not isinstance(raw_result, tuple) or len(raw_result) not in {2, 3}:
            raise SystemOneHTTPError("System One transport returned an invalid response tuple")

        status, raw = raw_result[0], raw_result[1]
        if int(status) < 200 or int(status) >= 300:
            raise SystemOneHTTPError(
                f"System One endpoint returned HTTP {status}: {bytes(raw).decode('utf-8', 'replace')[:500]}"
            )
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError) as exc:
            raise SystemOneHTTPError("System One endpoint returned invalid JSON") from exc
        if not isinstance(data, dict):
            raise SystemOneHTTPError("System One endpoint returned a non-object response")
        answers = data.get("answers")
        if not isinstance(answers, dict):
            raise SystemOneHTTPError("System One response is missing answers")

        response_model = str(data.get("model") or requested_model or "")
        if self.expected_identity and self.expected_identity not in response_model:
            raise SystemOneHTTPError(
                f"System One response identity mismatch: expected substring "
                f"{self.expected_identity!r}, got {response_model!r}"
            )

        usage = dict(data.get("usage")) if isinstance(data.get("usage"), dict) else {}
        try:
            usage.setdefault("inference_latency_ms", float(data.get("latency_ms")))
        except (TypeError, ValueError):
            pass

        return SystemOneHTTPResponse(
            model=response_model,
            answers=answers,
            usage=usage,
            latency_ms=latency_ms,
            request_id=str(data.get("id") or data.get("request_id") or ""),
            provider=str(data.get("provider") or self.provider_name),
            transport=str(data.get("transport") or self.transport_name),
            live_provider_call=False,
        )


class D1Client(SystemOneHTTPClient):
    """Liquid d1-3B preset over the generic System One HTTP transport."""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        path: str | None = None,
        model: str | None = None,
        timeout: float | None = None,
        token: str | None = None,
        expected_identity: str | None = None,
        transport: Callable | None = None,
    ) -> None:
        d1_base = base_url or os.getenv("HERMES_REFLEX_D1_BASE_URL") or DEFAULT_BASE_URL
        d1_path = path or os.getenv("HERMES_REFLEX_D1_PATH") or DEFAULT_PATH
        d1_model = model or os.getenv("HERMES_REFLEX_D1_MODEL") or D1_DEFAULT_MODEL
        d1_timeout = timeout if timeout is not None else os.getenv(
            "HERMES_REFLEX_D1_TIMEOUT", str(DEFAULT_TIMEOUT)
        )
        d1_token = token if token is not None else os.getenv("HERMES_REFLEX_D1_TOKEN") or ""
        d1_identity = (
            expected_identity
            if expected_identity is not None
            else os.getenv("HERMES_REFLEX_D1_EXPECTED_IDENTITY") or ""
        )
        super().__init__(
            base_url=d1_base,
            path=d1_path,
            model=d1_model,
            timeout=d1_timeout,
            token=d1_token,
            provider_name="Liquid D1",
            transport_name="d1-local-systemone",
            expected_identity=d1_identity,
            transport=transport,
        )

