import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hermes_nerve import nervous, router


class FakeDecisionResult:
    def __init__(self, value, confidence=0.95, probabilities=None):
        self.value = value
        self.confidence = confidence
        self.probabilities = probabilities or {value: confidence}
        self.request_id = "req-fake"
        self.latency_ms = 501.0


class ScriptedEngine:
    script = []
    calls = []

    def __init__(self):
        pass

    @classmethod
    def reset(cls, *items):
        cls.script = list(items)
        cls.calls = []

    def decide(self, **kwargs):
        self.__class__.calls.append(("decide", kwargs))
        item = self.__class__.script.pop(0)
        if isinstance(item, Exception):
            raise item
        if isinstance(item, FakeDecisionResult):
            return item
        return FakeDecisionResult(str(item))

    def assess(self, **kwargs):
        self.__class__.calls.append(("assess", kwargs))
        item = self.__class__.script.pop(0)
        if isinstance(item, Exception):
            raise item
        choice = str(item)
        return {
            "answers": {
                "intervention": {"type": "noul", "probability": 0.95},
                "next_control": {"type": "choice", "choice": choice, "confidence": 0.95, "probabilities": {choice: 0.95}},
            },
            "request_id": "req-assess",
            "latency_ms": 503.0,
        }


class RouterTests(unittest.TestCase):
    def test_read_only_tool_does_not_trigger_by_count(self):
        event = {"type": "TOOL_RESULT", "tool_name": "git_status", "status": "ok", "materiality": 0.0}
        for _ in range(800):
            result = router.assess_event(event, call_threshold=0.58)
            self.assertFalse(result.worth_calling)

    def test_contradiction_outranks_read_only_shape(self):
        event = {
            "type": "TOOL_RESULT", "tool_name": "read_file", "status": "ok",
            "contradiction": True, "novelty": 0.9, "uncertainty": 0.8,
        }
        result = router.assess_event(event, call_threshold=0.58)
        self.assertTrue(result.worth_calling)
        self.assertIn("contradictory-evidence", result.reasons)

    def test_completion_is_critical(self):
        result = router.assess_event({"type": "COMPLETION_CANDIDATE"}, call_threshold=0.99)
        self.assertTrue(result.worth_calling)
        self.assertTrue(result.critical)

    def test_semantic_hysteresis_suppresses_equivalent_noncritical_state(self):
        event = {"type": "DECISION", "goal": "g", "choices": ["A", "B"], "hermes_decision": "A", "materiality": 0.35}
        fp = router.decision_state_fingerprint(event)
        first = router.assess_event(event, call_threshold=0.58)
        second = router.assess_event(event, last_remote_fingerprint=fp, call_threshold=0.58)
        self.assertTrue(first.worth_calling)
        self.assertFalse(second.worth_calling)
        self.assertIn("semantic-hysteresis", second.reasons)


class NervousSystemTests(unittest.TestCase):
    def make_system(self, td):
        system = nervous.NervousSystem(engine_factory=ScriptedEngine)
        system.configure(enabled=True, admission_enabled=True, mode="correct_next", challenge_confidence=0.86, call_threshold=0.58)
        self.env = patch.dict(os.environ, {"HERMES_NERVE_NERVOUS_EVENTS": str(Path(td) / "nervous.jsonl")}, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)
        return system

    def test_off_admission_logs_but_suppresses_events(self):
        with tempfile.TemporaryDirectory() as td:
            ScriptedEngine.reset(FakeDecisionResult("OFF", 0.98, {"OFF": 0.98, "WATCH": 0.01, "ON": 0.01}))
            system = self.make_system(td)
            tid = system.start_turn(user_message="what's good", session_id="s1", turn_id="t1")
            self.assertTrue(system.drain())
            result = system.emit_event({"turn_id": tid, "type": "DECISION", "goal": "chat", "choices": ["A", "B"], "hermes_decision": "A", "materiality": 1.0})
            self.assertFalse(result["forwarded"])
            self.assertEqual(system.status(turn_id=tid)["admission"], "OFF")
            self.assertEqual(len(ScriptedEngine.calls), 1)

    def test_watch_promotes_on_material_decision(self):
        with tempfile.TemporaryDirectory() as td:
            ScriptedEngine.reset(
                FakeDecisionResult("WATCH", 0.90, {"OFF": 0.05, "WATCH": 0.90, "ON": 0.05}),
                FakeDecisionResult("B", 0.94, {"A": 0.06, "B": 0.94}),
            )
            system = self.make_system(td)
            system.start_turn(user_message="take a look at why this is odd", session_id="s1", turn_id="t1")
            self.assertTrue(system.drain())
            emitted = system.emit_event({
                "turn_id": "t1", "type": "STRATEGY_CHANGE", "goal": "fix issue",
                "choices": ["A", "B"], "hermes_decision": "A", "materiality": 0.9,
                "strategy_changed": True, "state_version": "v1",
            })
            self.assertTrue(emitted["forwarded"])
            self.assertTrue(system.drain())
            st = system.status(turn_id="t1")
            self.assertEqual(st["admission"], "ON")
            self.assertEqual(st["watch_promotions"], 1)
            self.assertEqual(st["pending_challenges"], 1)

    def test_confident_disagreement_injected_into_next_tool_result(self):
        with tempfile.TemporaryDirectory() as td:
            ScriptedEngine.reset(
                FakeDecisionResult("ON", 0.99, {"OFF": 0.0, "WATCH": 0.01, "ON": 0.99}),
                FakeDecisionResult("ROLLBACK", 0.96, {"PATCH": 0.04, "ROLLBACK": 0.96}),
            )
            system = self.make_system(td)
            system.start_turn(user_message="fix and ship it", session_id="s1", turn_id="t1")
            self.assertTrue(system.drain())
            system.emit_event({
                "turn_id": "t1", "type": "DECISION", "goal": "repair",
                "choices": ["PATCH", "ROLLBACK"], "hermes_decision": "PATCH",
                "materiality": 0.9, "state_version": "v1",
            })
            self.assertTrue(system.drain())
            transformed = system.inject_challenge("tool output", turn_id="t1")
            self.assertIsNotNone(transformed)
            self.assertIn("JEV_DECISION_CHALLENGE", transformed)
            self.assertIn("ROLLBACK", transformed)
            self.assertEqual(system.status(turn_id="t1")["pending_challenges"], 0)

    def test_low_confidence_disagreement_is_silent(self):
        with tempfile.TemporaryDirectory() as td:
            ScriptedEngine.reset(
                FakeDecisionResult("ON", 0.99),
                FakeDecisionResult("B", 0.60, {"A": 0.4, "B": 0.6}),
            )
            system = self.make_system(td)
            system.start_turn(user_message="fix it", session_id="s1", turn_id="t1")
            self.assertTrue(system.drain())
            system.emit_event({"turn_id": "t1", "type": "DECISION", "goal": "g", "choices": ["A", "B"], "hermes_decision": "A", "materiality": 0.9})
            self.assertTrue(system.drain())
            self.assertIsNone(system.inject_challenge("x", turn_id="t1"))

    def test_stale_challenge_is_not_delivered(self):
        with tempfile.TemporaryDirectory() as td:
            ScriptedEngine.reset(
                FakeDecisionResult("ON", 0.99),
                FakeDecisionResult("B", 0.95, {"A": 0.05, "B": 0.95}),
            )
            system = self.make_system(td)
            system.start_turn(user_message="fix it", session_id="s1", turn_id="t1")
            self.assertTrue(system.drain())
            system.emit_event({"turn_id": "t1", "type": "DECISION", "goal": "g", "choices": ["A", "B"], "hermes_decision": "A", "materiality": 0.9, "state_version": "v1"})
            self.assertTrue(system.drain())
            # A newer local event advances the state version before the old challenge can be injected.
            system.emit_event({"turn_id": "t1", "type": "OBSERVATION", "goal": "g", "state_version": "v2"})
            self.assertIsNone(system.inject_challenge("x", turn_id="t1"))
            self.assertEqual(system.report()["metrics"].get("challenges_stale"), 1)

    def test_repeated_failure_dedup_and_local_loop_breaker_blocks_fourth_identical_action(self):
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, {
            "HERMES_NERVE_NERVOUS_EVENTS": str(Path(td) / "nervous.jsonl"),
            "HERMES_NERVE_OUTCOMES": str(Path(td) / "outcomes.jsonl"),
        }, clear=False):
            ScriptedEngine.reset("CONTINUE")
            system = nervous.NervousSystem(engine_factory=ScriptedEngine)
            system.configure(enabled=True, admission_enabled=False, mode="correct_next", repeated_failure_local_replan_at=3)
            system.start_turn(user_message="repair it", session_id="s1", turn_id="t1")
            event = dict(
                tool_name="terminal", args={"command": "python broken.py"}, status="error",
                result="Traceback: connection failed after 501 ms", error_message="connection failed after 501 ms",
                session_id="s1", turn_id="t1",
            )
            first = system.observe_tool_call(tool_call_id="c1", **event)
            self.assertTrue(first["forwarded"])
            self.assertTrue(system.drain())
            second = system.observe_tool_call(tool_call_id="c2", **event)
            third = system.observe_tool_call(tool_call_id="c3", **event)
            self.assertFalse(second["forwarded"])
            self.assertFalse(third["forwarded"])
            self.assertEqual(len([c for c in ScriptedEngine.calls if c[0] == "assess"]), 1)
            block = system.before_tool_call(
                tool_name="terminal", args={"command": "python broken.py"},
                session_id="s1", turn_id="t1", tool_call_id="c4",
            )
            self.assertEqual(block["action"], "block")
            self.assertIn("prevents repeating the exact action", block["message"])
            q = system.quality_metrics()
            self.assertEqual(q["local_loop_breakers"], 1)
            self.assertGreaterEqual(q["repeated_failure_provider_calls_avoided"], 2)
            self.assertGreaterEqual(q["control_override_attempts"], 1)

    def test_deterministic_policy_failure_replans_after_first_block_without_provider(self):
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, {
            "HERMES_NERVE_NERVOUS_EVENTS": str(Path(td) / "nervous.jsonl"),
            "HERMES_NERVE_OUTCOMES": str(Path(td) / "outcomes.jsonl"),
        }, clear=False):
            ScriptedEngine.reset()
            system = nervous.NervousSystem(engine_factory=ScriptedEngine)
            system.configure(enabled=True, admission_enabled=False, mode="correct_next")
            system.start_turn(user_message="recover from policy block", session_id="s1", turn_id="t1")
            out = system.observe_tool_call(
                tool_name="execute_code", args={"code": "print('x')"}, status="error",
                result="BLOCKED: execute_code is not allowed in an unattended session; single-query mode requires approval",
                error_message="", tool_call_id="c1", session_id="s1", turn_id="t1",
            )
            self.assertFalse(out["forwarded"])
            self.assertIn("deterministic-failure-local-replan", out["router"]["reasons"])
            state = system.status(turn_id="t1")
            self.assertEqual(state["active_control"]["control"], "REPLAN")
            self.assertEqual(state["active_control"]["source"], "local-deterministic-failure")
            self.assertEqual(ScriptedEngine.calls, [])
            transformed = system.inject_challenge("blocked", turn_id="t1")
            self.assertIn("Do not repeat the same tool with the same arguments", transformed)
            blocked = system.before_tool_call(
                tool_name="execute_code", args={"code": "print('x')"},
                session_id="s1", turn_id="t1", tool_call_id="c2",
            )
            self.assertEqual(blocked["action"], "block")
            allowed = system.before_tool_call(
                tool_name="read_file", args={"path": "README.md"},
                session_id="s1", turn_id="t1", tool_call_id="c3",
            )
            self.assertIsNone(allowed)
            self.assertIsNone(system.status(turn_id="t1")["active_control"])
            self.assertEqual(system.quality_metrics()["deterministic_failure_replans"], 1)

    def test_deterministic_failure_classifier_boundary_matrix(self):
        deterministic = {
            "policy block": ("error", "BLOCKED: execute_code is not allowed", "", "blocked:"),
            "permission": ("error", "permission denied; try again with elevated privileges", "", "permission denied"),
            "approval": ("error", "approval required before this action", "", "approval required"),
            "invalid arg": ("error", "invalid argument: --mdoe", "", "invalid argument"),
            "unknown option": ("error", "unknown option --mdoe", "", "unknown option"),
            "schema": ("error", "schema validation failed for field x", "", "schema validation"),
            "missing file": ("error", '{"error":"File not found: missing.txt"}', "", "file not found"),
            "missing path": ("error", "no such file or directory: /tmp/missing", "", "no such file or directory"),
            "read only": ("error", "read-only file system", "", "read-only file system"),
        }
        for label, (status, result, error, expected) in deterministic.items():
            with self.subTest(label=label):
                self.assertEqual(nervous._deterministic_failure_reason(status, result, error), expected)

        transient = {
            "timeout": ("error", "request timed out after 5s", ""),
            "connection reset": ("error", "connection reset by peer", ""),
            "connection refused": ("error", "connection refused", ""),
            "connection failed": ("error", "connection failed after 501 ms", "connection failed"),
            "temporary unavailable": ("error", "temporarily unavailable", ""),
            "temporary failure": ("error", "temporary failure in name resolution", ""),
            "rate limit": ("error", "rate limit exceeded", ""),
            "429": ("error", "too many requests", ""),
            "503": ("error", "service unavailable", ""),
            "resource busy": ("error", "resource busy", ""),
            "database lock": ("error", "database is locked", ""),
        }
        for label, (status, result, error) in transient.items():
            with self.subTest(label=label):
                self.assertEqual(nervous._deterministic_failure_reason(status, result, error), "")

        self.assertEqual(
            nervous._deterministic_failure_reason("blocked", "temporary-looking text", ""),
            "blocked-status",
        )

    def test_remote_replan_enforces_next_action_and_attributes_followup(self):
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, {
            "HERMES_NERVE_NERVOUS_EVENTS": str(Path(td) / "nervous.jsonl"),
            "HERMES_NERVE_OUTCOMES": str(Path(td) / "outcomes.jsonl"),
        }, clear=False):
            ScriptedEngine.reset("REPLAN")
            system = nervous.NervousSystem(engine_factory=ScriptedEngine)
            system.configure(enabled=True, admission_enabled=False, mode="correct_next", challenge_confidence=0.86)
            system.start_turn(user_message="repair it", session_id="s1", turn_id="t1")
            out = system.observe_tool_call(
                tool_name="terminal", args={"command": "python broken.py"}, status="error",
                result="same failure", error_message="same failure", tool_call_id="c1",
                session_id="s1", turn_id="t1",
            )
            self.assertTrue(out["forwarded"])
            self.assertTrue(system.drain())
            state = system.status(turn_id="t1")
            self.assertEqual(state["active_control"]["control"], "REPLAN")
            decision_id = state["active_control"]["decision_id"]
            block = system.before_tool_call(
                tool_name="terminal", args={"command": "python broken.py"},
                session_id="s1", turn_id="t1", tool_call_id="c2",
            )
            self.assertEqual(block["action"], "block")
            # A materially different diagnostic demonstrates the control affected trajectory.
            allowed = system.before_tool_call(
                tool_name="read_file", args={"path": "config.yaml"},
                session_id="s1", turn_id="t1", tool_call_id="c3",
            )
            self.assertIsNone(allowed)
            self.assertIsNone(system.status(turn_id="t1")["active_control"])
            report = system._outcomes.report()
            self.assertGreaterEqual(report["metrics"].get("controls_enforced", 0), 1)
            self.assertGreaterEqual(report["metrics"].get("controls_followed", 0), 1)
            self.assertGreaterEqual(report["control_attribution"]["control_decisions_with_next_action"], 1)
            rows = system._outcomes.rows()
            self.assertTrue(any(r.get("decision_id") == decision_id and r.get("stage") == "next_action" for r in rows))

    def test_remote_retry_explicitly_allows_one_identical_retry(self):
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, {
            "HERMES_NERVE_NERVOUS_EVENTS": str(Path(td) / "nervous.jsonl"),
            "HERMES_NERVE_OUTCOMES": str(Path(td) / "outcomes.jsonl"),
        }, clear=False):
            ScriptedEngine.reset("RETRY")
            system = nervous.NervousSystem(engine_factory=ScriptedEngine)
            system.configure(enabled=True, admission_enabled=False, mode="correct_next")
            system.start_turn(user_message="repair it", session_id="s1", turn_id="t1")
            system.observe_tool_call(
                tool_name="terminal", args={"command": "python broken.py"}, status="error",
                result="temporary failure", error_message="temporary failure", tool_call_id="c1",
                session_id="s1", turn_id="t1",
            )
            self.assertTrue(system.drain())
            self.assertEqual(system.status(turn_id="t1")["active_control"]["control"], "RETRY")
            allowed = system.before_tool_call(
                tool_name="terminal", args={"command": "python broken.py"},
                session_id="s1", turn_id="t1", tool_call_id="c2",
            )
            self.assertIsNone(allowed)
            self.assertIsNone(system.status(turn_id="t1")["active_control"])

    def test_failure_fingerprint_normalizes_timing_noise(self):
        a = router.failure_fingerprint(
            tool_name="terminal", args={"command": "python broken.py"}, status="error",
            error_message="request failed in 501 ms id=12345678",
        )
        b = router.failure_fingerprint(
            tool_name="terminal", args={"command": "python broken.py"}, status="error",
            error_message="request failed in 812 ms id=87654321",
        )
        self.assertEqual(a, b)

    def test_provider_budget_is_safety_valve_not_primary_router(self):
        with tempfile.TemporaryDirectory() as td:
            ScriptedEngine.reset(FakeDecisionResult("ON", 0.99), FakeDecisionResult("B", 0.99))
            system = self.make_system(td)
            system.configure(max_provider_calls_per_turn=1)
            system.start_turn(user_message="fix it", session_id="s1", turn_id="t1")
            self.assertTrue(system.drain())
            out = system.emit_event({"turn_id": "t1", "type": "DECISION", "goal": "g", "choices": ["A", "B"], "hermes_decision": "A", "materiality": 1.0})
            self.assertFalse(out["forwarded"])
            self.assertEqual(out["reason"], "provider-budget")

    def test_finish_turn_evicts_state_and_preserves_newer_session_mapping(self):
        with tempfile.TemporaryDirectory() as td:
            system = self.make_system(td)
            system.configure(admission_enabled=False)
            system.start_turn(user_message="first", session_id="s1", turn_id="t1")
            system.start_turn(user_message="second", session_id="s1", turn_id="t2")
            system.finish_turn(turn_id="t1", assistant_response="done first")
            self.assertNotIn("t1", system._turns)
            self.assertEqual(system._session_turn.get("s1"), "t2")
            system.finish_turn(turn_id="t2", assistant_response="done second")
            self.assertNotIn("t2", system._turns)
            self.assertNotIn("s1", system._session_turn)
            self.assertEqual(system.report()["active_turns"], 0)
            self.assertEqual(system.report()["metrics"]["turns_evicted"], 2)

    def test_late_admission_after_finished_turn_is_ignored(self):
        with tempfile.TemporaryDirectory() as td:
            ScriptedEngine.reset()
            system = self.make_system(td)
            system.configure(admission_enabled=False)
            system.start_turn(user_message="work", session_id="s1", turn_id="t1")
            system.finish_turn(turn_id="t1", assistant_response="done")
            system._do_admission({"turn_id": "t1", "user_message": "work"})
            self.assertEqual(ScriptedEngine.calls, [])
            self.assertEqual(system.status(turn_id="t1"), {"active": False})


if __name__ == "__main__":
    unittest.main()

class ProviderCompatibilityTests(unittest.TestCase):
    def test_typesafe_direct_wire_contract_and_request_id(self):
        from hermes_nerve import client
        captured = {}
        def transport(url, headers, body, timeout):
            captured.update(url=url, headers=headers, body=json.loads(body), timeout=timeout)
            payload = {
                "model": "jev-latest",
                "answers": {"q": {"type": "choice", "choice": "B", "confidence": 0.9, "probabilities": {"A": 0.1, "B": 0.9}}},
                "usage": {},
            }
            return 200, json.dumps(payload).encode(), {"x-typesafe-request-id": "ts-req-1"}
        c = client.JevClient(provider="typesafe", api_key="k", transport=transport)
        r = c.system_one(state={}, questions={"q": {"type": "choice", "criteria": {"A": None, "B": None}}})
        self.assertEqual(captured["url"], "https://api.typesafe.ai/v1/systemone")
        self.assertEqual(r.transport, "typesafe-system-one")
        self.assertEqual(r.request_id, "ts-req-1")

    def test_selected_provider_only_requires_selected_key(self):
        from hermes_nerve import client
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "or"}, clear=True):
            self.assertEqual(client.JevClient(provider="openrouter").api_key, "or")
            with self.assertRaisesRegex(client.JevError, "TYPESAFE_API_KEY"):
                client.JevClient(provider="typesafe")
        with patch.dict(os.environ, {"TYPESAFE_API_KEY": "ts"}, clear=True):
            self.assertEqual(client.JevClient(provider="typesafe").api_key, "ts")

class AsyncInvariantTests(unittest.TestCase):
    def test_turn_admission_does_not_block_hermes(self):
        import time
        class SlowEngine(ScriptedEngine):
            def decide(self, **kwargs):
                time.sleep(0.25)
                return FakeDecisionResult("OFF", 0.99)
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, {"HERMES_NERVE_NERVOUS_EVENTS": str(Path(td) / "n.jsonl")}, clear=False):
            system = nervous.NervousSystem(engine_factory=SlowEngine)
            system.configure(enabled=True, admission_enabled=True)
            started = time.monotonic()
            system.start_turn(user_message="hello", session_id="s", turn_id="t")
            elapsed = time.monotonic() - started
            self.assertLess(elapsed, 0.08)
            self.assertTrue(system.drain(timeout=2.0))
            self.assertEqual(system.status(turn_id="t")["admission"], "OFF")

    def test_material_events_batch_behind_inflight_assessment(self):
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, {"HERMES_NERVE_NERVOUS_EVENTS": str(Path(td) / "n.jsonl")}, clear=False):
            system = nervous.NervousSystem(engine_factory=ScriptedEngine)
            system.configure(enabled=True, admission_enabled=False)
            system.start_turn(user_message="do work", session_id="s", turn_id="t")
            state = system._resolve_turn(turn_id="t")
            state.assessment_inflight = True
            out1 = system.emit_event({"turn_id": "t", "type": "STRATEGY_CHANGE", "goal": "g", "materiality": 1.0, "strategy_changed": True})
            out2 = system.emit_event({"turn_id": "t", "type": "RECOVERY", "goal": "g", "materiality": 1.0})
            self.assertEqual(out1["reason"], "batched-behind-inflight")
            self.assertEqual(out2["reason"], "batched-behind-inflight")
            self.assertEqual(system.status(turn_id="t")["pending_batch_events"], 2)
            self.assertEqual(system.report()["metrics"].get("events_batched"), 2)

class OutcomeLearningTests(unittest.TestCase):
    def test_outcome_report_tracks_false_pass_and_useful_disagreement(self):
        from hermes_nerve.outcomes import OutcomeStore
        with tempfile.TemporaryDirectory() as td:
            store = OutcomeStore(Path(td) / "outcomes.jsonl")
            store.append({
                "record_type": "decision", "event_id": "e1", "decision_type": "COMPLETION_CANDIDATE",
                "agreement": False, "challenge_issued": True, "jev_confidence": 0.97,
                "latency_ms": 510, "usage": {"input_tokens": 20, "output_tokens": 3, "cost": 0.00001},
            })
            store.append({
                "record_type": "outcome", "event_id": "e1", "challenge_disposition": "accepted",
                "successful": True, "jev_changed_action": True, "premature_done_caught": True,
                "useful_disagreement": True, "false_pass": False,
            })
            report = store.report()
            self.assertEqual(report["metrics"]["useful_disagreements"], 1)
            self.assertEqual(report["metrics"]["premature_done_caught"], 1)
            self.assertEqual(report["metrics"]["decision_correction_success"], 1)
            self.assertGreater(report["average_latency_ms"], 0)

    def test_local_decisions_do_not_make_provider_cost_unknown(self):
        from hermes_nerve.outcomes import OutcomeStore
        with tempfile.TemporaryDirectory() as td:
            store = OutcomeStore(Path(td) / "outcomes.jsonl")
            store.append({
                "record_type": "decision", "event_id": "local", "source": "local-loop-breaker",
                "decision_type": "REPEATED_FAILURE", "jev_decision": "REPLAN",
            })
            store.append({
                "record_type": "decision", "event_id": "remote", "decision_type": "RECOVERY",
                "request_id": "req-1", "usage": {"input_tokens": 10, "output_tokens": 2, "cost": 0.00001},
            })
            report = store.report()
            self.assertAlmostEqual(report["provider_cost"], 0.00001)
            self.assertEqual(report["provider_cost_reported_decisions"], 1)
            self.assertEqual(report["provider_cost_missing_decisions"], 0)

    def test_missing_remote_cost_is_unknown_even_with_local_decisions(self):
        from hermes_nerve.outcomes import OutcomeStore
        with tempfile.TemporaryDirectory() as td:
            store = OutcomeStore(Path(td) / "outcomes.jsonl")
            store.append({"record_type": "decision", "event_id": "local", "source": "local-loop-breaker"})
            store.append({
                "record_type": "decision", "event_id": "remote", "request_id": "req-2",
                "usage": {"input_tokens": 10, "output_tokens": 2},
            })
            report = store.report()
            self.assertIsNone(report["provider_cost"])
            self.assertEqual(report["provider_cost_missing_decisions"], 1)

    def test_historical_model_requires_labeled_sample_floor(self):
        from hermes_nerve.outcomes import HistoricalOutcomeModel, OutcomeStore
        with tempfile.TemporaryDirectory() as td:
            store = OutcomeStore(Path(td) / "outcomes.jsonl")
            model = HistoricalOutcomeModel(store, min_samples=3)
            for idx, useful in enumerate((True, False)):
                eid = f"e{idx}"
                store.append({"record_type": "decision", "event_id": eid, "decision_type": "RECOVERY"})
                store.append({"record_type": "outcome", "event_id": eid, "useful_disagreement": useful})
            prob, samples = model.predict({"decision_type": "RECOVERY"})
            self.assertIsNone(prob)
            self.assertEqual(samples, 2)
            store.append({"record_type": "decision", "event_id": "e2", "decision_type": "RECOVERY"})
            store.append({"record_type": "outcome", "event_id": "e2", "useful_disagreement": True})
            prob, samples = model.predict({"decision_type": "RECOVERY"})
            self.assertEqual(samples, 3)
            self.assertGreater(prob, 0.5)