from __future__ import annotations

import json
import os
import tempfile
import unittest

from hermes_nerve.engine import DecisionEngine
from hermes_nerve.receipts import report as receipt_report
from hermes_nerve.reflex import config as reflex_config
from hermes_nerve.reflex.shadow import ShadowProvider
from hermes_nerve.reflex.systemone import D1Client, SystemOneHTTPClient, SystemOneHTTPError


def _choice_response(payload, *, model="served-d1", provider="local-systemone"):
    labels = list(payload["questions"]["decision"]["criteria"])
    return {
        "id": "local-1",
        "model": model,
        "provider": provider,
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


class SystemOneD1Tests(unittest.TestCase):
    def setUp(self):
        self.old_home = os.environ.get("HERMES_HOME")
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["HERMES_HOME"] = self.tmp.name
        reflex_config.configure(backend="jev")

    def tearDown(self):
        reflex_config.configure(backend="jev")
        if self.old_home is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = self.old_home
        self.tmp.cleanup()

    def test_generic_systemone_wire_and_images(self):
        seen = {}

        def transport(url, headers, body, timeout):
            seen["url"] = url
            seen["headers"] = headers
            seen["payload"] = json.loads(body)
            return 200, json.dumps(_choice_response(seen["payload"])).encode(), {}

        client = SystemOneHTTPClient(
            base_url="http://127.0.0.1:8080",
            model="served-d1",
            token="secret",
            provider_name="llama.cpp",
            expected_identity="d1",
            transport=transport,
        )
        response = client.system_one(
            state={"goal": "stay on task"},
            questions={"decision": {"type": "choice", "instructions": "Choose", "criteria": {"WATCH": "continue", "REPLAN": "change"}}},
            images=["data:image/png;base64,AAAA"],
        )
        self.assertEqual(seen["url"], "http://127.0.0.1:8080/v1/systemone")
        self.assertEqual(seen["headers"]["Authorization"], "Bearer secret")
        self.assertEqual(seen["payload"]["images"], ["data:image/png;base64,AAAA"])
        self.assertEqual(response.answers["decision"]["choice"], "WATCH")
        self.assertFalse(response.live_provider_call)

    def test_generic_client_omits_blank_model(self):
        def transport(url, headers, body, timeout):
            payload = json.loads(body)
            self.assertNotIn("model", payload)
            return 200, json.dumps({"answers": {"q": {"type": "noul", "noul": 0.7}}, "usage": {}}).encode(), {}
        response = SystemOneHTTPClient(model="", transport=transport).system_one(
            state="x", questions={"q": {"type": "noul", "instructions": "yes?"}}
        )
        self.assertEqual(response.model, "")

    def test_plaintext_non_loopback_is_rejected(self):
        with self.assertRaises(SystemOneHTTPError):
            SystemOneHTTPClient(base_url="http://192.0.2.1:8080")

    def test_d1_preset_identity_and_multimodal_wire(self):
        seen = {}
        def transport(url, headers, body, timeout):
            payload = json.loads(body)
            seen["payload"] = payload
            self.assertEqual(payload["model"], "LiquidAI/d1-3B")
            return 200, json.dumps(_choice_response(payload, model="LiquidAI/d1-3B", provider="Liquid D1")).encode(), {}
        client = D1Client(transport=transport, expected_identity="d1-3B")
        response = client.system_one(
            state="x",
            questions={"decision": {"type": "choice", "instructions": "Choose", "criteria": {"WATCH": "w", "REPLAN": "r"}}},
            images=["data:image/png;base64,AAAA"],
        )
        self.assertEqual(response.provider, "Liquid D1")
        self.assertEqual(response.transport, "d1-local-systemone")
        self.assertEqual(seen["payload"]["images"], ["data:image/png;base64,AAAA"])

    def test_decision_engine_passes_images_to_capable_provider(self):
        seen = {}
        def transport(url, headers, body, timeout):
            payload = json.loads(body)
            seen["images"] = payload.get("images")
            return 200, json.dumps(_choice_response(payload, model="LiquidAI/d1-3B", provider="Liquid D1")).encode(), {}
        result = DecisionEngine(provider=D1Client(transport=transport)).decide(
            state={"screen": "confirmation"},
            instructions="Choose",
            choices=["ALLOW", "APPROVAL"],
            images=["data:image/png;base64,BBBB"],
        )
        self.assertEqual(result.value, "ALLOW")
        self.assertEqual(seen["images"], ["data:image/png;base64,BBBB"])

    def test_decision_engine_rejects_images_for_legacy_provider(self):
        class LegacyProvider:
            def system_one(self, *, state, questions, model=None):
                raise AssertionError("must not be called")
        with self.assertRaisesRegex(ValueError, "does not support image"):
            DecisionEngine(provider=LegacyProvider()).decide(
                state="x",
                instructions="Choose",
                choices=["A", "B"],
                images=["data:image/png;base64,AAAA"],
            )

    def test_d1_decision_receipt_is_local_only(self):
        def transport(url, headers, body, timeout):
            payload = json.loads(body)
            return 200, json.dumps(_choice_response(payload, model="LiquidAI/d1-3B", provider="Liquid D1")).encode(), {}
        result = DecisionEngine(provider=D1Client(transport=transport)).decide(
            state={"x": 1}, instructions="Choose", choices=["WATCH", "REPLAN"], contract="reflex/d1-local/v1"
        )
        self.assertEqual(result.provider, "Liquid D1")
        self.assertEqual(result.provenance_status, "LOCAL_ONLY")
        self.assertFalse(result.live_provider_call)
        self.assertEqual(receipt_report(recent_limit=0)["provider_calls"], 0)

    def test_configured_d1_requires_no_jev_credentials(self):
        old = {key: os.environ.pop(key, None) for key in ("OPENROUTER_API_KEY", "TYPESAFE_API_KEY", "OPENCODE_API_KEY")}
        try:
            reflex_config.configure(backend="d1")
            self.assertIsInstance(reflex_config.get_provider(), D1Client)
        finally:
            for key, value in old.items():
                if value is not None:
                    os.environ[key] = value

    def test_generic_factory_and_token_redaction(self):
        reflex_config.configure(
            backend="systemone",
            systemone_base_url="http://127.0.0.1:8080",
            systemone_model="custom-decision-model",
            systemone_token="private-token",
            systemone_provider="llama.cpp",
        )
        provider = reflex_config.get_provider()
        self.assertIsInstance(provider, SystemOneHTTPClient)
        self.assertEqual(provider.model, "custom-decision-model")
        report = reflex_config.report(recent_limit=0)
        self.assertTrue(report["settings"]["systemone_token_configured"])
        self.assertNotIn("systemone_token", report["settings"])
        self.assertNotIn("private-token", json.dumps(report))

    def test_shadow_can_select_d1(self):
        old = os.environ.get("OPENROUTER_API_KEY")
        os.environ["OPENROUTER_API_KEY"] = "test-key"
        try:
            reflex_config.configure(backend="shadow", shadow_backend="d1", d1_token="secret")
            provider = reflex_config.get_provider()
            self.assertIsInstance(provider, ShadowProvider)
            self.assertIsInstance(provider.shadow, D1Client)
            report = reflex_config.report(recent_limit=0)
            self.assertTrue(report["settings"]["d1_token_configured"])
            self.assertNotIn("secret", json.dumps(report))
        finally:
            if old is None:
                os.environ.pop("OPENROUTER_API_KEY", None)
            else:
                os.environ["OPENROUTER_API_KEY"] = old


if __name__ == "__main__":
    unittest.main()
