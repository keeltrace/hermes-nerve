"""Dependency-free HTTP client for a self-hosted OpenJev /v1/systemone helper."""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable

DEFAULT_BASE_URL = "http://127.0.0.1:3000"
DEFAULT_PATH = "/v1/systemone"
DEFAULT_VERSION_PATH = "/v1/version"
DEFAULT_MODEL = "openjev"
DEFAULT_TIMEOUT = 10.0


class OpenJevError(RuntimeError):
    pass


@dataclass(frozen=True)
class OpenJevResponse:
    model: str
    answers: dict[str, dict[str, Any]]
    usage: dict[str, Any]
    latency_ms: float
    request_id: str = ""
    provider: str = "OpenJev"
    transport: str = "openjev-local-http"
    live_provider_call: bool = False


class OpenJevClient:
    """Talk to the OpenJev helper without importing vLLM/torch into Hermes.

    OpenJev's request ``model`` field is only a label; the helper's response
    ``model`` is a detailed served-model identity string.  We therefore pin the
    measured server through ``expected_identity`` (optional substring) rather
    than requiring response.model == request.model.
    """

    def __init__(
        self,
        *,
        base_url: str | None = None,
        model: str | None = None,
        timeout: float | None = None,
        token: str | None = None,
        expected_identity: str | None = None,
        transport: Callable | None = None,
    ) -> None:
        self.base_url = str(base_url or os.getenv("HERMES_REFLEX_OPENJEV_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        if not self.base_url.startswith(("http://127.0.0.1", "http://localhost", "https://")):
            raise OpenJevError(
                "OpenJev helper must use loopback HTTP or HTTPS. For a remote GPU, SSH-forward the helper port "
                "or terminate TLS before configuring a non-loopback endpoint."
            )
        self.model = str(model or os.getenv("HERMES_REFLEX_OPENJEV_MODEL") or DEFAULT_MODEL).strip() or DEFAULT_MODEL
        raw_timeout = timeout if timeout is not None else os.getenv("HERMES_REFLEX_OPENJEV_TIMEOUT", str(DEFAULT_TIMEOUT))
        try:
            parsed_timeout = float(raw_timeout)
        except (TypeError, ValueError):
            parsed_timeout = DEFAULT_TIMEOUT
        self.timeout = min(120.0, max(0.25, parsed_timeout))
        self.token = str(token if token is not None else os.getenv("HERMES_REFLEX_OPENJEV_TOKEN") or "").strip()
        self.expected_identity = str(
            expected_identity if expected_identity is not None else os.getenv("HERMES_REFLEX_OPENJEV_EXPECTED_IDENTITY") or ""
        ).strip()
        self._transport = transport or self._urllib_transport

    @staticmethod
    def _urllib_transport(url: str, headers: dict[str, str], body: bytes, timeout: float):
        request = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return int(response.status), response.read(), {str(k).lower(): str(v) for k, v in response.headers.items()}
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:500]
            raise OpenJevError(f"OpenJev helper returned HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise OpenJevError(f"OpenJev helper connection failed: {exc}") from exc

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json", "Content-Type": "application/json", "User-Agent": "hermes-jev-reflex/dev17"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def version(self) -> dict[str, Any]:
        request = urllib.request.Request(self.base_url + DEFAULT_VERSION_PATH, headers=self._headers(), method="GET")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:500]
            raise OpenJevError(f"OpenJev version endpoint returned HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise OpenJevError(f"OpenJev version endpoint failed: {exc}") from exc
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError) as exc:
            raise OpenJevError("OpenJev version endpoint returned invalid JSON") from exc
        if not isinstance(data, dict):
            raise OpenJevError("OpenJev version endpoint returned a non-object response")
        text = json.dumps(data, sort_keys=True, default=str)
        if self.expected_identity and self.expected_identity not in text:
            raise OpenJevError(
                f"OpenJev served identity mismatch: expected substring {self.expected_identity!r} was not present"
            )
        return data

    def system_one(self, *, state: Any, questions: dict[str, dict[str, Any]], model: str | None = None) -> OpenJevResponse:
        if not isinstance(questions, dict) or not questions:
            raise OpenJevError("At least one question is required")
        requested_model = str(model or self.model)
        payload = {"state": state, "questions": questions, "model": requested_model}
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False, default=str).encode("utf-8")
        started = time.monotonic()
        raw_result = self._transport(self.base_url + DEFAULT_PATH, self._headers(), body, self.timeout)
        latency_ms = (time.monotonic() - started) * 1000.0
        if not isinstance(raw_result, tuple) or len(raw_result) not in {2, 3}:
            raise OpenJevError("OpenJev transport returned an invalid response tuple")
        status, raw = raw_result[0], raw_result[1]
        if int(status) < 200 or int(status) >= 300:
            raise OpenJevError(f"OpenJev helper returned HTTP {status}: {bytes(raw).decode('utf-8', 'replace')[:500]}")
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError) as exc:
            raise OpenJevError("OpenJev helper returned invalid JSON") from exc
        if not isinstance(data, dict):
            raise OpenJevError("OpenJev helper returned a non-object response")
        answers = data.get("answers")
        if not isinstance(answers, dict):
            raise OpenJevError("OpenJev helper response is missing answers")
        response_model = str(data.get("model") or requested_model)
        if self.expected_identity and self.expected_identity not in response_model:
            raise OpenJevError(
                f"OpenJev response identity mismatch: expected substring {self.expected_identity!r}, got {response_model!r}"
            )
        usage = dict(data.get("usage")) if isinstance(data.get("usage"), dict) else {}
        return OpenJevResponse(
            model=response_model,
            answers=answers,
            usage=usage,
            latency_ms=latency_ms,
            request_id=str(data.get("id") or data.get("request_id") or ""),
            provider="OpenJev",
            transport="openjev-local-http",
            live_provider_call=False,
        )
