"""Von Reflex backend tests.

Von serves the TypeSafe ``/v1/systemone`` wire protocol, so these tests assert
the Von preset's identity/timeout defaults and that ``von`` is selectable both as
a primary backend and as a shadow backend behind authoritative Jev.
"""
from __future__ import annotations

import pytest

# Stale: the 'von' reflex backend was removed. reflex/config.py rejects it
# ("reflex backend must be one of: jev, laya, openjev, systemone, d1, shadow")
# and hermes_nerve.reflex.systemone no longer defines VonClient. The module
# cannot import, so it previously broke collection of the whole suite. It is
# skipped (not deleted) until it is either removed or ported to D1Client.
pytest.skip("von reflex backend removed; VonClient no longer exists", allow_module_level=True)

import json
import os
import tempfile
import unittest

from hermes_nerve.reflex import config as reflex_config
from hermes_nerve.reflex.systemone import VonClient


def _choice_response(payload, *, model="von-1.3.0", provider="Von"):
    labels = list(payload["questions"]["decision"]["criteria"])
    return {
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
        "usage": {"input_tokens": 19, "output_tokens": 1},
    }


class VonBackendTests(unittest.TestCase):
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

    def test_von_is_an_accepted_backend(self):
        reflex_config.configure(backend="von")
        self.assertEqual(reflex_config.settings()["backend"], "von")
        self.assertIsInstance(reflex_config.get_provider(), VonClient)

    def test_von_defaults_are_cpu_sized(self):
        """Von is CPU-served, so its default timeout must exceed the GPU presets."""
        reflex_config.configure(backend="von")
        client = reflex_config.get_provider()
        self.assertEqual(client.provider_name, "Von")
        self.assertEqual(client.transport_name, "von-local-systemone")
        self.assertEqual(client.path, "/v1/systemone")
        self.assertEqual(client.model, "von-1.3.0")
        self.assertGreater(client.timeout, 5.0)

    def test_von_posts_systemone_wire_payload(self):
        seen = {}

        def transport(url, headers, body, timeout):
            seen["url"] = url
            seen["payload"] = json.loads(body)
            return 200, json.dumps(_choice_response(seen["payload"])).encode(), {}

        client = VonClient(
            base_url="http://127.0.0.1:8775",
            model="von-1.3.0",
            transport=transport,
        )
        result = client.system_one(
            state={"error": "Disk volume /var/log at 98% capacity."},
            questions={
                "decision": {
                    "type": "choice",
                    "instructions": "Who owns this?",
                    "criteria": {"sre": "Infrastructure and capacity", "app": "Application code"},
                }
            },
        )
        self.assertEqual(seen["url"], "http://127.0.0.1:8775/v1/systemone")
        self.assertEqual(seen["payload"]["model"], "von-1.3.0")
        self.assertEqual(result.answers["decision"]["choice"], "sre")
        self.assertFalse(result.live_provider_call)

    def test_von_identity_pin_rejects_mismatch(self):
        def transport(url, headers, body, timeout):
            return 200, json.dumps(_choice_response(json.loads(body), model="some-other-model")).encode(), {}

        client = VonClient(
            base_url="http://127.0.0.1:8775",
            expected_identity="von-1.3.0",
            transport=transport,
        )
        with self.assertRaises(Exception):
            client.system_one(
                state={"error": "x"},
                questions={
                    "decision": {
                        "type": "choice",
                        "instructions": "pick",
                        "criteria": {"a": "first", "b": "second"},
                    }
                },
            )

    def test_von_selectable_as_shadow_backend(self):
        reflex_config.configure(backend="shadow", shadow_backend="von")
        self.assertEqual(reflex_config.settings()["shadow_backend"], "von")
        client = reflex_config._von_client(reflex_config._raw_settings())
        self.assertIsInstance(client, VonClient)

    def test_unknown_backend_still_rejected(self):
        with self.assertRaises(ValueError):
            reflex_config.configure(backend="not-a-backend")
        with self.assertRaises(ValueError):
            reflex_config.configure(backend="shadow", shadow_backend="not-a-backend")


if __name__ == "__main__":
    unittest.main()
