"""Optional local D1 System One sidecar.

Heavy torch/transformers dependencies are imported only inside this sidecar,
never by Nerve's Hermes plugin. This loads official LiquidAI/d1-3B checkpoint
with trust_remote_code=True ONLY after explicit install approval.
"""
from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import time
import uuid

MODEL_ID = "LiquidAI/d1-3B"
MAX_REQUEST = 131072


def load_model():
    try:
        import torch
        from transformers import AutoModel
    except ImportError as exc:
        raise RuntimeError("Missing D1 optional dependencies. Use the Control Panel local installer.") from exc
    device = "cuda" if torch.cuda.is_available() else "mps" if (
        hasattr(torch.backends, "mps") and torch.backends.mps.is_available()
    ) else "cpu"
    dtype = torch.float32 if device == "cpu" else torch.bfloat16
    print("Loading official " + MODEL_ID + " on " + device, flush=True)
    # Upstream checkpoint contains custom Python. Never use user-supplied IDs.
    model = AutoModel.from_pretrained(MODEL_ID, trust_remote_code=True, dtype=dtype).to(device)
    model.eval()
    return model


class Service(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, model):
        super().__init__(address, Handler)
        self.model = model
        self.inference_lock = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    server_version = "NerveD1/1"

    def log_message(self, fmt, *args):
        return

    def _reply(self, status, data):
        raw = json.dumps(data, ensure_ascii=False, allow_nan=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path in ("/healthz", "/health"):
            return self._reply(200, {"ok": True, "status": "ok", "provider": "Liquid D1",
                                     "model": MODEL_ID})
        if self.path == "/v1/models":
            return self._reply(200, {"models": [{"id": MODEL_ID}], "model": MODEL_ID})
        return self._reply(404, {"error": "not_found"})

    def do_POST(self):
        if self.path != "/v1/systemone":
            return self._reply(404, {"error": "not_found"})
        try:
            length = int(self.headers.get("Content-Length") or "0")
            if length < 1 or length > MAX_REQUEST:
                return self._reply(413, {"error": "invalid_body_size"})
            data = json.loads(self.rfile.read(length))
            questions = data.get("questions") if isinstance(data, dict) else None
            if not isinstance(questions, dict) or not questions or len(questions) > 16:
                return self._reply(400, {"error": "invalid_questions"})
            # Do not expose arbitrary model weights or dynamic file paths.
            requested = data.get("model")
            if requested and requested != MODEL_ID:
                return self._reply(400, {"error": "model_mismatch"})
            for name, question in questions.items():
                if (not isinstance(name, str) or not isinstance(question, dict)
                        or question.get("type") not in ("noul", "choice", "score")):
                    return self._reply(400, {"error": "invalid_question"})
            started = time.monotonic()
            images = data.get("images")
            if images is not None:
                if (not isinstance(images, list) or len(images) > 4
                        or not all(isinstance(i, str) and len(i) <= 8_000_000 for i in images)):
                    return self._reply(400, {"error": "invalid_images"})
            with self.server.inference_lock:
                if images is not None:
                    import inspect
                    try:
                        accepts_images = "images" in inspect.signature(
                            self.server.model.system_one).parameters
                    except (TypeError, ValueError):
                        accepts_images = False
                    if accepts_images:
                        result = self.server.model.system_one(
                            data.get("state"), questions, images=images)
                    else:
                        return self._reply(400, {"error": "model_does_not_accept_images"})
                else:
                    result = self.server.model.system_one(data.get("state"), questions)
            if not isinstance(result, dict) or not isinstance(result.get("answers"), dict):
                raise ValueError("D1 model returned no typed answers")
            return self._reply(200, {
                "id": "d1-" + uuid.uuid4().hex,
                "model": MODEL_ID,
                "provider": "LiquidAI",
                "transport": "d1-local-systemone",
                "answers": result["answers"],
                "usage": result.get("usage", {}) if isinstance(result.get("usage"), dict) else {},
                "latency_ms": round((time.monotonic() - started) * 1000, 1),
            })
        except (ValueError, TypeError, json.JSONDecodeError):
            return self._reply(400, {"error": "invalid_request"})
        except Exception:
            # Model errors stay in the owner's server log; never echo traceback.
            return self._reply(500, {"error": "inference_failed"})


def main(argv=None):
    parser = argparse.ArgumentParser(description="Nerve local D1 System One")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args(argv)
    if args.host != "127.0.0.1" or not 1 <= args.port <= 65535:
        parser.error("D1 must listen on 127.0.0.1 and a valid TCP port.")
    model = load_model()
    server = Service((args.host, args.port), model)
    print("D1 ready on " + args.host + ":" + str(args.port), flush=True)
    try:
        server.serve_forever(poll_interval=.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
