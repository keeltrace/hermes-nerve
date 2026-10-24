"""Small fixed-model HTTP sidecar for Laya.

Run with:
    python -m hermes_nerve.reflex.laya_service --device cuda

The Hermes plugin itself remains dependency-free. This process is the only code
path that imports the optional ``laya``/torch stack.
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

MAX_BODY_BYTES = 1_048_576


def evaluate(agent: Any, *, state: Any, questions: dict[str, Any], model_name: str) -> dict[str, Any]:
    started = time.monotonic()
    runner = getattr(agent, "system_one", None) or getattr(agent, "predict", None)
    if not callable(runner):
        raise RuntimeError("Laya agent exposes neither system_one nor predict")
    result = runner(state, questions)
    latency_ms = (time.monotonic() - started) * 1000.0
    if not isinstance(result, dict) or not isinstance(result.get("answers"), dict):
        raise RuntimeError("Laya returned an invalid response")
    usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
    return {
        "id": "laya-" + uuid.uuid4().hex,
        "model": model_name,
        "provider": "Laya",
        "transport": "laya-local-http",
        "answers": result["answers"],
        "usage": usage,
        "latency_ms": round(latency_ms, 3),
    }


class LayaServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, server_address, handler_cls, *, agent: Any, model_name: str, token: str = ""):
        super().__init__(server_address, handler_cls)
        self.agent = agent
        self.model_name = model_name
        self.token = token
        self.inference_lock = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    server_version = "HermesReflexLaya/1"

    def log_message(self, fmt: str, *args) -> None:
        # Keep model traffic quiet; operators can wrap the process if access logs are desired.
        return

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _authorized(self) -> bool:
        token = str(getattr(self.server, "token", "") or "")
        if not token:
            return True
        supplied = str(self.headers.get("Authorization") or "")
        expected = "Bearer " + token
        return secrets.compare_digest(supplied, expected)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        if self.path != "/healthz":
            self._json(404, {"error": "not_found"})
            return
        self._json(200, {"ok": True, "provider": "Laya", "model": self.server.model_name, "transport": "laya-local-http"})

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        if self.path != "/v1/systemone":
            self._json(404, {"error": "not_found"})
            return
        if not self._authorized():
            self._json(401, {"error": "unauthorized"})
            return
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            self._json(400, {"error": "invalid_content_length"})
            return
        if length <= 0 or length > MAX_BODY_BYTES:
            self._json(413 if length > MAX_BODY_BYTES else 400, {"error": "invalid_body_size"})
            return
        try:
            payload = json.loads(self.rfile.read(length))
        except Exception:
            self._json(400, {"error": "invalid_json"})
            return
        if not isinstance(payload, dict) or not isinstance(payload.get("questions"), dict) or not payload["questions"]:
            self._json(400, {"error": "questions_required"})
            return
        # The sidecar is deliberately fixed-model. A caller cannot make it load
        # arbitrary Hub repositories by changing the request body.
        try:
            with self.server.inference_lock:
                result = evaluate(
                    self.server.agent,
                    state=payload.get("state"),
                    questions=payload["questions"],
                    model_name=self.server.model_name,
                )
        except Exception as exc:
            self._json(500, {"error": "inference_failed", "detail": f"{type(exc).__name__}: {exc}"[:500]})
            return
        self._json(200, result)


def build_agent(*, model: str, subfolder: str, device: str | None):
    try:
        import laya
    except ImportError as exc:
        raise SystemExit("Laya is not installed. Install the optional sidecar dependencies with: pip install 'laya==0.3.29'") from exc
    kwargs = {"device": device or None}
    if subfolder:
        kwargs["subfolder"] = subfolder
    return laya.load(model, **kwargs)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Preloaded Laya sidecar for Hermes Reflex")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host. Default is loopback only.")
    parser.add_argument("--port", default=8765, type=int)
    parser.add_argument("--model", default="convaiinnovations/laya-typed-decisions")
    parser.add_argument("--subfolder", default="")
    parser.add_argument("--device", default="", help="cuda, cpu, mps, or blank for Laya auto-detection")
    parser.add_argument("--token-env", default="HERMES_REFLEX_LAYA_TOKEN", help="Optional bearer-token environment variable")
    args = parser.parse_args(argv)
    token = str(os.getenv(args.token_env) or "").strip()
    if args.host not in {"127.0.0.1", "localhost", "::1"} and not token:
        raise SystemExit("Refusing a non-loopback bind without an authentication token")
    model_name = args.model + (":" + args.subfolder if args.subfolder else "")
    agent = build_agent(model=args.model, subfolder=args.subfolder, device=args.device or None)
    server = LayaServer((args.host, args.port), Handler, agent=agent, model_name=model_name, token=token)
    print(f"Hermes Reflex Laya sidecar ready on {args.host}:{args.port} model={model_name}", flush=True)
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
