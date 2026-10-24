#!/usr/bin/env python3
"""Smoke-check a local Jev-compatible /v1/systemone server."""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hermes_nerve.reflex.systemone import D1Client, SystemOneHTTPClient

def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--base-url", default="http://127.0.0.1:8080")
    p.add_argument("--path", default="/v1/systemone")
    p.add_argument("--model", default="")
    p.add_argument("--expected-identity", default="")
    p.add_argument("--d1", action="store_true")
    a = p.parse_args()
    cls = D1Client if a.d1 else SystemOneHTTPClient
    kwargs = {"base_url": a.base_url, "path": a.path, "expected_identity": a.expected_identity or None}
    if a.model:
        kwargs["model"] = a.model
    client = cls(**kwargs)
    result = client.system_one(
        state={"probe": "Nerve System One sidecar health check"},
        questions={"healthy": {"type": "noul", "instructions": "Is this a valid health-check request?"}},
    )
    answer = result.answers.get("healthy")
    if not isinstance(answer, dict) or "noul" not in answer:
        raise SystemExit("System One probe returned no noul answer")
    print(json.dumps({"ok": True, "provider": result.provider, "model": result.model, "transport": result.transport, "latency_ms": round(result.latency_ms, 3), "noul": answer.get("noul"), "usage": result.usage}, sort_keys=True))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
