"""Regression tests for System One transient transport retry.

A local D1/Laya sidecar serializes requests, so a decision issued while another
session holds the server can legitimately exceed the per-request timeout. Before
this behaviour existed, that produced a bare ``provider-unavailable`` gate event
and forced a human-approval prompt on an otherwise ordinary tool call.

Invariants under test (behaviour contracts, not snapshots):

1. A transport-level failure is retried once and succeeds.
2. A persistent transport failure is bounded -- it must not become a retry storm.
3. An HTTP status error is deterministic and must NOT be retried.
4. A model-identity mismatch is deterministic and must NOT be retried.
5. No retry path may fall back to a hosted/paid provider: the client stays on its
   configured transport and the raised error still names the local endpoint.
"""
from __future__ import annotations

import unittest

from hermes_nerve.reflex.systemone import D1Client, SystemOneHTTPClient, SystemOneHTTPError

_CONNECTION_FAILED = "System One endpoint connection failed: timed out"


def _client(**kwargs) -> D1Client:
    return D1Client(
        base_url="http://127.0.0.1:8080",
        model="LiquidAI/d1-3B",
        expected_identity="d1-3B",
        timeout=5.0,
        **kwargs,
    )


class SystemOneRetryTests(unittest.TestCase):
    def test_transport_failure_is_retried_once_and_succeeds(self):
        client = _client()
        calls = {"n": 0}

        def flaky(url, headers, body, timeout):
            calls["n"] += 1
            if calls["n"] == 1:
                raise SystemOneHTTPError(_CONNECTION_FAILED)
            return (200, b'{"answers":{"q":{"value":"ALLOW"}},"model":"d1-3B",'
                        b'"provider":"Liquid D1","transport":"d1-local-systemone"}')

        client._transport = flaky
        result = client.system_one(state="s", questions={"q": {"type": "bool"}})
        self.assertEqual(calls["n"], 2)
        self.assertEqual(result.answers["q"]["value"], "ALLOW")

    def test_persistent_transport_failure_is_bounded_not_a_storm(self):
        client = _client()
        calls = {"n": 0}

        def always_fail(url, headers, body, timeout):
            calls["n"] += 1
            raise SystemOneHTTPError(_CONNECTION_FAILED)

        client._transport = always_fail
        with self.assertRaises(SystemOneHTTPError):
            client.system_one(state="s", questions={"q": {"type": "bool"}})
        self.assertEqual(calls["n"], SystemOneHTTPClient._TRANSIENT_RETRY_ATTEMPTS)

    def test_http_error_is_not_retried(self):
        client = _client()
        calls = {"n": 0}

        def http_error(url, headers, body, timeout):
            calls["n"] += 1
            raise SystemOneHTTPError("System One endpoint returned HTTP 400: bad state")

        client._transport = http_error
        with self.assertRaises(SystemOneHTTPError) as ctx:
            client.system_one(state="s", questions={"q": {"type": "bool"}})
        self.assertEqual(calls["n"], 1)
        self.assertIn("HTTP 400", str(ctx.exception))

    def test_identity_mismatch_is_not_retried(self):
        client = _client()
        calls = {"n": 0}

        def wrong_model(url, headers, body, timeout):
            calls["n"] += 1
            return (200, b'{"answers":{"q":{"value":"ALLOW"}},"model":"typesafe/jev-1.13"}')

        client._transport = wrong_model
        with self.assertRaises(SystemOneHTTPError) as ctx:
            client.system_one(state="s", questions={"q": {"type": "bool"}})
        self.assertEqual(calls["n"], 1)
        self.assertIn("mismatch", str(ctx.exception))

    def test_no_paid_fallback_after_exhausted_retries(self):
        """The error must still name the configured local endpoint, never a paid one."""
        client = _client()

        def refused(url, headers, body, timeout):
            raise SystemOneHTTPError("System One endpoint connection failed: refused")

        client._transport = refused
        with self.assertRaises(SystemOneHTTPError) as ctx:
            client.system_one(state="s", questions={"q": {"type": "bool"}})
        message = str(ctx.exception)
        self.assertNotIn("openrouter", message.lower())
        self.assertNotIn("typesafe", message.lower())
        # Transport and endpoint remain the configured local ones.
        self.assertEqual(client.transport_name, "d1-local-systemone")
        self.assertEqual(client.base_url, "http://127.0.0.1:8080")

    def test_successful_response_is_not_retried(self):
        client = _client()
        calls = {"n": 0}

        def good(url, headers, body, timeout):
            calls["n"] += 1
            return (200, b'{"answers":{"q":{"value":"ALLOW"}},'
                        b'"model":"/home/user/.cache/nerve-d1/models/d1-3B-Q4_K_M.gguf",'
                        b'"provider":"Liquid D1","transport":"d1-local-systemone"}')

        client._transport = good
        result = client.system_one(state="s", questions={"q": {"type": "bool"}})
        self.assertEqual(calls["n"], 1)
        self.assertEqual(result.provider, "Liquid D1")
        self.assertEqual(result.transport, "d1-local-systemone")


if __name__ == "__main__":
    unittest.main()
