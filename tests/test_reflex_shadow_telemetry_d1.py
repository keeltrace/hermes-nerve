from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from hermes_nerve.reflex import telemetry
from hermes_nerve.reflex.shadow import ShadowProvider
from hermes_nerve.reflex.systemone import D1Client


def _choice_response(payload):
    labels = list(payload["questions"]["decision"]["criteria"])
    return {
        "model": "LiquidAI/d1-3B",
        "provider": "Liquid D1",
        "answers": {
            "decision": {
                "type": "choice",
                "choice": labels[0],
                "probabilities": {labels[0]: 0.93, labels[1]: 0.07},
                "confidence": 0.91,
            }
        },
        "usage": {"input_tokens": 19, "output_tokens": 0},
    }


class ShadowTelemetryD1Tests(unittest.TestCase):
    def test_local_provenance_latency_and_usage_are_reported(self):
        class PrimaryProvider:
            def system_one(self, *, state, questions, model=None):
                return SimpleNamespace(
                    model="jev-latest",
                    answers={
                        "decision": {
                            "type": "choice",
                            "choice": "WATCH",
                            "probabilities": {"WATCH": 0.9, "REPLAN": 0.1},
                            "confidence": 0.8,
                        }
                    },
                    usage={"input_tokens": 11, "output_tokens": 0},
                    latency_ms=12.5,
                    request_id="req-primary",
                    provider="Jev",
                    transport="openrouter-decisions",
                    live_provider_call=True,
                )

        def transport(url, headers, body, timeout):
            return 200, json.dumps(_choice_response(json.loads(body))).encode(), {}

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "shadow.jsonl"
            provider = ShadowProvider(
                PrimaryProvider(),
                D1Client(transport=transport),
                path=path,
                asynchronous=False,
            )
            provider.system_one(
                state={"goal": "continue"},
                questions={
                    "decision": {
                        "type": "choice",
                        "instructions": "Choose",
                        "criteria": {"WATCH": "continue", "REPLAN": "change"},
                    }
                },
            )
            report = telemetry.report(path, recent_limit=1)

        recent = report["recent"][0]
        self.assertEqual(recent["shadow"]["provenance_status"], "LOCAL_ONLY")
        self.assertFalse(recent["shadow"]["live_provider_call"])
        self.assertEqual(recent["shadow"]["usage"]["input_tokens"], 19)
        self.assertEqual(report["shadow_provenance"]["LOCAL_ONLY"], 1)
        self.assertEqual(report["shadow_latency_ms"]["count"], 1)
        self.assertIsNotNone(report["shadow_latency_ms"]["p50"])
        self.assertIsNotNone(report["shadow_latency_ms"]["p95"])
        self.assertEqual(report["shadow_errors"], 0)
        self.assertEqual(report["shadow_timeouts"], 0)


if __name__ == "__main__":
    unittest.main()
