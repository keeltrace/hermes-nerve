"""D1/shadow telemetry contract tests for the observatory boundary.

Primary Jev decisions stay authoritative: the shadow provider must return the
primary answer unchanged even when the shadow times out, errors, or returns
nothing. Missing, empty, unavailable and ok states must stay distinct in the
snapshot. All paths are temporary; no live or paid endpoint is called.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from hermes_nerve.observatory import collector
from hermes_nerve.reflex import telemetry
from hermes_nerve.reflex.shadow import ShadowProvider

QUESTIONS = {
    "decision": {
        "type": "choice",
        "instructions": "Choose",
        "criteria": {"WATCH": "continue", "REPLAN": "change"},
    }
}
STATE = {"goal": "continue"}


class _PrimaryJev:
    """Authoritative provider. Its answer is the one the caller must receive."""

    def __init__(self) -> None:
        self.calls = 0

    def system_one(self, *, state, questions, model=None):
        self.calls += 1
        return SimpleNamespace(
            model="jev-latest",
            answers={"decision": {"type": "choice", "choice": "WATCH", "probabilities": {"WATCH": 0.9, "REPLAN": 0.1}, "confidence": 0.8}},
            usage={"input_tokens": 11, "output_tokens": 0},
            latency_ms=12.5,
            request_id="req-primary",
            provider="Jev",
            transport="openrouter-decisions",
            live_provider_call=True,
        )


class _FailingShadow:
    def __init__(self, error: Exception) -> None:
        self.error = error

    def system_one(self, *, state, questions, model=None):
        raise self.error


class ShadowBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "shadow.jsonl"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _run(self, shadow_error: Exception):
        primary = _PrimaryJev()
        provider = ShadowProvider(primary, _FailingShadow(shadow_error), path=self.path, asynchronous=False)
        answer = provider.system_one(state=STATE, questions=QUESTIONS)
        return primary, answer

    def test_shadow_timeout_keeps_primary_answer_and_counts_as_timeout(self) -> None:
        primary, answer = self._run(TimeoutError("read timed out after 5.0s"))
        self.assertEqual(primary.calls, 1)
        self.assertEqual(answer.answers["decision"]["choice"], "WATCH")
        report = telemetry.report(self.path)
        self.assertEqual(report["shadow_errors"], 1)
        self.assertEqual(report["shadow_timeouts"], 1)

    def test_shadow_exception_is_fail_open_and_does_not_raise_to_caller(self) -> None:
        primary, answer = self._run(RuntimeError("upstream 503"))
        self.assertEqual(answer.answers["decision"]["choice"], "WATCH")
        report = telemetry.report(self.path)
        self.assertEqual(report["shadow_errors"], 1)
        self.assertEqual(report["shadow_timeouts"], 0)

    def test_shadow_error_row_never_stores_raw_state_or_secret_text(self) -> None:
        secret = "sk-test-DO-NOT-STORE-0123456789"
        self._run(RuntimeError(f"auth failed for {secret}"))
        stored = self.path.read_text(encoding="utf-8")
        # The row keeps a hash of the state and the error text, so the secret
        # must not appear in the stored record at all.
        self.assertNotIn(secret, stored)
        self.assertNotIn('"goal"', stored)  # raw state is hashed, never stored
        # The observatory snapshot surfaces only the error class, not the message.
        info = collector.collect_shadow(self.path, collector._now())
        self.assertNotIn(secret, json.dumps(info))
        self.assertEqual(info["recent"][0]["shadow"]["error_class"], "RuntimeError")


class SnapshotStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_missing_shadow_file_is_missing_not_ok(self) -> None:
        info = collector.collect_shadow(self.root / "absent.jsonl", collector._now())
        self.assertEqual(info["status"], "missing")
        self.assertEqual(info["authority"], "primary-jev")

    def test_empty_shadow_file_is_empty_not_missing(self) -> None:
        path = self.root / "empty.jsonl"
        path.write_text("", encoding="utf-8")
        info = collector.collect_shadow(path, collector._now())
        self.assertEqual(info["status"], "empty")
        self.assertIsNone(info["freshness"]["age_seconds"])
        self.assertEqual(info["freshness"]["state"], "unknown")

    def test_unreadable_shadow_is_unavailable_and_leaks_no_path(self) -> None:
        path = self.root / "as-directory.jsonl"
        path.mkdir()
        info = collector.collect_shadow(path, collector._now())
        self.assertEqual(info["status"], "unavailable")
        self.assertNotIn(str(self.root), json.dumps(info))

    def test_oversized_shadow_file_is_unavailable_without_being_read(self) -> None:
        path = self.root / "huge.jsonl"
        path.write_text('{"schema": "x"}\n' * 50, encoding="utf-8")
        calls = []
        real_report = collector.shadow_telemetry.report
        original_cap = collector.SHADOW_MAX_BYTES
        collector.SHADOW_MAX_BYTES = 10  # force the cap without a large fixture
        collector.shadow_telemetry.report = lambda *a, **k: calls.append(1) or real_report(*a, **k)
        try:
            info = collector.collect_shadow(path, collector._now())
        finally:
            collector.SHADOW_MAX_BYTES = original_cap
            collector.shadow_telemetry.report = real_report
        self.assertEqual(info["status"], "unavailable")
        self.assertEqual(info["error"], "ShadowTooLarge")
        self.assertEqual(calls, [], "an oversized file must not be read")
        self.assertNotIn(str(self.root), json.dumps(info))

    def test_recent_row_with_old_timestamp_is_stale(self) -> None:
        path = self.root / "old.jsonl"
        row = {
            "schema": "hermes-reflex-shadow/v1",
            "created_at": "2001-01-01T00:00:00+00:00",
            "question_ids": ["decision"],
            "primary": {"provider": "Jev"},
            "shadow": {"provider": "d1", "latency_ms": 5.0, "usage": {}},
            "agreements": {"decision": True},
        }
        path.write_text(json.dumps(row) + "\n", encoding="utf-8")
        info = collector.collect_shadow(path, collector._now())
        self.assertEqual(info["status"], "ok")
        self.assertEqual(info["freshness"]["state"], "stale")
        self.assertEqual(info["authority"], "primary-jev")


if __name__ == "__main__":
    unittest.main()
