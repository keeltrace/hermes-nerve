import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hermes_nerve import client, context, engine, gate, ledger, lifecycle, paths, privacy, receipts, tools
from hermes_nerve.context_engine import NerveContextEngine
from hermes_nerve.jsonl import read_jsonl


class FakeProvider:
    def __init__(self, choice="ALLOW", confidence=0.93, probabilities=None, answers=None):
        self.choice = choice
        self.confidence = confidence
        self.probabilities = probabilities or {"ALLOW": confidence, "APPROVAL": 0.05, "BLOCK": 0.02}
        self.answers = answers

    def system_one(self, *, state, questions, model=None):
        if self.answers is not None:
            answers = self.answers
        else:
            key = next(iter(questions))
            answers = {
                key: {
                    "type": "choice",
                    "choice": self.choice,
                    "confidence": self.confidence,
                    "probabilities": self.probabilities,
                }
            }
        return client.JevResponse(
            model="jev-test",
            answers=answers,
            usage={"input_tokens": 10, "output_tokens": 2, "cost": 0.00001},
            latency_ms=12.5,
            request_id="req-test",
            provider="TypeSafe",
        )


class EngineTests(unittest.TestCase):
    def setUp(self):
        client._configured_provider = None
        client._configured_base_url = None
        client._configured_model = None
        client._configured_typesafe_model = None
        client._configured_opencode_model = None
        client._configured_timeout = None

    def test_redacts_secrets_and_hashes_stably(self):
        value = {
            "api_key": "abc",
            "nested": {"token": "secret", "vendor_api_key": "vendor-secret"},
            "text": "[REDACTED]",
            "slack": "xoxb-1234567890-abcdefghijkl",
            "aws": "[REDACTED]",
            "jwt": "eyJabcdefghijk.abcdefghijk.abcdefghijk",
            "query": "https://example.test/cb?[REDACTED]&ok=1",
            "pem": "[REDACTED]\nabc123\n-----END PRIVATE KEY-----",
        }
        safe = privacy.redact(value)
        self.assertEqual(safe["api_key"], "[REDACTED]")
        self.assertEqual(safe["nested"]["token"], "[REDACTED]")
        self.assertEqual(safe["nested"]["vendor_api_key"], "[REDACTED]")
        self.assertNotIn("abcdefghijklmnop", safe["text"])
        self.assertNotIn("xoxb-", safe["slack"])
        self.assertNotIn("[REDACTED]", safe["aws"])
        self.assertNotIn("eyJabcdefghijk", safe["jwt"])
        self.assertNotIn("super-secret-value", safe["query"])
        self.assertNotIn("abc123", safe["pem"])
        self.assertEqual(privacy.canonical_hash({"b": 2, "a": 1}), privacy.canonical_hash({"a": 1, "b": 2}))

    def test_client_wire_protocol_and_metadata(self):
        captured = {}

        def transport(url, headers, body, timeout):
            captured.update(url=url, headers=headers, body=json.loads(body), timeout=timeout)
            return 200, json.dumps({
                "id": "gen-dec-1",
                "provider": "TypeSafe",
                "model": "jev-test",
                "usage": {"input_tokens": 3, "output_tokens": 1, "cost": 0.000001},
                "answers": {"q": {"type": "choice", "choice": "A", "confidence": 0.9, "probabilities": {"A": 0.9, "B": 0.1}}},
            }).encode()

        c = client.JevClient(api_key="secret", transport=transport)
        response = c.system_one(state={"x": 1}, questions={"q": {"type": "choice", "criteria": {"A": None, "B": None}}})
        self.assertEqual(captured["url"], "https://openrouter.ai/api/alpha/decisions")
        self.assertEqual(captured["headers"]["Authorization"], "Bearer secret")
        self.assertEqual(captured["body"]["model"], "typesafe/jev-1.13")
        self.assertEqual(response.answers["q"]["choice"], "A")
        self.assertEqual(response.request_id, "gen-dec-1")
        self.assertEqual(response.provider, "TypeSafe")
        self.assertEqual(response.usage["cost"], 0.000001)

    def test_hermes_config_overrides_model_and_timeout(self):
        client.configure(model="typesafe/jev-next", timeout=7.5)
        captured = {}

        def transport(url, headers, body, timeout):
            captured.update(url=url, body=json.loads(body), timeout=timeout)
            return 200, b'{"model":"x","answers":{"q":{"type":"noul","noul":0.8}},"usage":{}}'

        c = client.JevClient(api_key="secret", transport=transport)
        c.system_one(state="x", questions={"q": {"type": "noul"}})
        self.assertEqual(captured["url"], "https://openrouter.ai/api/alpha/decisions")
        self.assertEqual(captured["body"]["model"], "typesafe/jev-next")
        self.assertEqual(captured["timeout"], 7.5)

    def test_rejects_non_https_base_url(self):
        with self.assertRaises(client.JevError):
            client.JevClient(api_key="secret", base_url="http://router.example")

    def test_decision_receipt_omits_raw_state_by_default(self):
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, {"HERMES_NERVE_RECEIPTS": str(Path(td) / "r.jsonl")}, clear=False):
            result = engine.DecisionEngine(FakeProvider()).decide(
                state={"secret": "raw-value", "task": "x"}, instructions="choose", choices=["ALLOW", "APPROVAL", "BLOCK"], contract="test/v1"
            )
            self.assertEqual(result.value, "ALLOW")
            self.assertEqual(result.request_id, "req-test")
            self.assertEqual(result.provider, "TypeSafe")
            self.assertEqual(result.usage["cost"], 0.00001)
            record = json.loads((Path(td) / "r.jsonl").read_text())
            self.assertIn("state_sha256", record)
            self.assertNotIn("state", record)
            self.assertNotIn("raw-value", json.dumps(record))
            self.assertEqual(record["result"]["request_id"], "req-test")

    def test_rank_orders_probabilities(self):
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, {"HERMES_NERVE_RECEIPTS": str(Path(td) / "r.jsonl")}, clear=False):
            provider = FakeProvider(choice="b", confidence=0.6, probabilities={"a": 0.3, "b": 0.6, "c": 0.1})
            result = engine.DecisionEngine(provider).rank(state={}, instructions="rank", items={"a": None, "b": None, "c": None})
            self.assertEqual([x["label"] for x in result["ranking"]], ["b", "a", "c"])
            self.assertEqual(result["usage"]["cost"], 0.00001)

    def test_multi_question_assess_preserves_native_answer_types(self):
        answers = {
            "urgent": {"type": "noul", "noul": 0.97},
            "team": {"type": "choice", "choice": "technical", "probabilities": {"technical": 0.9, "billing": 0.1}, "confidence": 0.9},
            "severity": {"type": "score", "score": 2, "probabilities": [0.05, 0.15, 0.8]},
        }
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, {"HERMES_NERVE_RECEIPTS": str(Path(td) / "r.jsonl")}, clear=False):
            result = engine.DecisionEngine(FakeProvider(answers=answers)).assess(
                state={"ticket": "service unavailable"},
                questions={
                    "urgent": {"type": "noul", "instructions": "Urgent?", "criteria": {"true": "urgent", "false": "not urgent"}},
                    "team": {"type": "choice", "instructions": "Route", "criteria": {"technical": None, "billing": None}},
                    "severity": {"type": "score", "instructions": "Severity", "criteria": ["low", "medium", "high"]},
                },
                contract="triage/v1",
            )
            self.assertEqual(result["answers"]["urgent"]["noul"], 0.97)
            self.assertEqual(result["answers"]["team"]["choice"], "technical")
            self.assertEqual(result["answers"]["severity"]["score"], 2)
            self.assertEqual(result["request_id"], "req-test")

    def test_assess_rejects_invalid_question_shapes(self):
        provider = FakeProvider(answers={})
        e = engine.DecisionEngine(provider)
        with self.assertRaises(ValueError):
            e.assess(state={}, questions={"x": {"type": "choice", "criteria": {"only": None}}})
        with self.assertRaises(ValueError):
            e.assess(state={}, questions={"x": {"type": "score", "criteria": ["only"]}})
        with self.assertRaises(ValueError):
            e.assess(state={}, questions={"x": {"type": "unknown"}})

    def test_out_of_contract_choice_rejected(self):
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, {"HERMES_NERVE_RECEIPTS": str(Path(td) / "r.jsonl")}, clear=False):
            with self.assertRaises(ValueError):
                engine.DecisionEngine(FakeProvider(choice="MAYBE", probabilities={"MAYBE": 1.0})).decide(
                    state={}, instructions="x", choices=["YES", "NO"]
                )


class FakeCurationEngine:
    def __init__(self, answers_per_call):
        self.answers_per_call = list(answers_per_call)
        self.calls = []

    def assess(self, *, state, questions, contract):
        self.calls.append({"state": state, "questions": questions, "contract": contract})
        answers = self.answers_per_call.pop(0)
        return {
            "answers": answers,
            "model": "jev-test",
            "latency_ms": 10.0,
            "contract": contract,
            "usage": {"input_tokens": 20, "output_tokens": 4, "cost": 0.00002},
            "request_id": f"req-{len(self.calls)}",
            "provider": "TypeSafe",
        }


class ContextCurationTests(unittest.TestCase):
    def setUp(self):
        lifecycle.reset()
        ledger.configure(enabled=False, detail="sanitized")
        context.configure(
            preview_chars=1200,
            anchor_chars=80,
            preserve_tail=0,
            mode="apply",
            drop_max_needed=0.20,
            drop_max_exact=0.20,
            drop_min_superseded=0.75,
            anchor_max_needed=0.55,
            anchor_max_exact=0.45,
            conflict_pin_min=0.70,
        )

    @staticmethod
    def semantic_batch(rows):
        answers = {}
        for slot, row in enumerate(rows):
            needed, exact, superseded, conflict = row
            answers[f"needed_{slot}"] = {"type": "noul", "noul": needed}
            answers[f"exact_{slot}"] = {"type": "noul", "noul": exact}
            answers[f"superseded_{slot}"] = {"type": "noul", "noul": superseded}
            answers[f"conflict_{slot}"] = {"type": "noul", "noul": conflict}
        return answers

    def test_protects_text_pins_and_recent_tail_without_jev(self):
        fake = FakeCurationEngine([])
        result = context.curate_context(
            goal="Keep the important evidence",
            items=[
                {"id": "u1", "kind": "user_text", "content": "Never edit generated files."},
                {"id": "p1", "kind": "tool_result", "content": "critical exact output", "pinned": True},
                {"id": "r1", "kind": "tool_result", "content": "newest result", "recoverable": True},
            ],
            preserve_tail=1,
            engine_factory=lambda: fake,
        )
        self.assertEqual(len(fake.calls), 0)
        self.assertEqual([d["action"] for d in result["decisions"]], ["KEEP_EXACT", "PIN", "PIN"])
        self.assertEqual([x["content"] for x in result["curated_items"]], ["Never edit generated files.", "critical exact output", "newest result"])

    def test_semantic_policy_keep_anchor_drop_unrecoverable_and_conflict(self):
        fake = FakeCurationEngine([
            self.semantic_batch([
                (0.90, 0.92, 0.05, 0.05),  # keep exact
                (0.30, 0.20, 0.40, 0.10),  # anchor
                (0.05, 0.05, 0.95, 0.05),  # drop
                (0.05, 0.05, 0.95, 0.05),  # unrecoverable -> keep exact
            ]),
            self.semantic_batch([
                (0.10, 0.10, 0.30, 0.95),  # conflict -> pin
            ]),
        ])
        long_text = "x" * 400
        result = context.curate_context(
            goal="Finish the current debugging task",
            items=[
                {"id": "keep", "kind": "tool_result", "content": long_text, "recoverable": True},
                {"id": "anchor", "kind": "tool_result", "content": long_text, "recoverable": True},
                {"id": "drop", "kind": "tool_result", "content": long_text, "recoverable": True},
                {"id": "unrecoverable", "kind": "artifact", "content": "unique evidence", "recoverable": False},
                {"id": "conflict", "kind": "observation", "content": "contradictory telemetry", "recoverable": True},
            ],
            preserve_tail=0,
            anchor_chars=60,
            engine_factory=lambda: fake,
        )
        actions = {d["id"]: d for d in result["decisions"]}
        self.assertEqual(actions["keep"]["action"], "KEEP_EXACT")
        self.assertEqual(actions["anchor"]["action"], "ANCHOR")
        self.assertEqual(actions["drop"]["action"], "DROP")
        self.assertEqual(actions["unrecoverable"]["action"], "KEEP_EXACT")
        self.assertEqual(actions["unrecoverable"]["basis"], "safety:unrecoverable")
        self.assertEqual(actions["conflict"]["action"], "PIN")
        self.assertEqual(actions["conflict"]["basis"], "safety:unresolved_conflict")
        ids = [x["id"] for x in result["curated_items"]]
        self.assertEqual(ids, ["keep", "anchor", "unrecoverable", "conflict"])
        anchored = next(x for x in result["curated_items"] if x["id"] == "anchor")
        self.assertIn("NERVE_CONTEXT_ANCHOR", anchored["content"])
        self.assertIn("nerve_context_rehydrate", anchored["content"])
        self.assertGreater(result["stats"]["applied_reduction_ratio"], 0)
        self.assertEqual(result["stats"]["applied"]["drop"], 1)
        self.assertEqual(result["stats"]["applied"]["anchor"], 1)
        self.assertEqual(result["usage"]["input_tokens"], 40)

    def test_shadow_mode_records_proposal_but_keeps_originals(self):
        fake = FakeCurationEngine([self.semantic_batch([
            (0.05, 0.05, 0.95, 0.05),
            (0.30, 0.20, 0.40, 0.10),
        ])])
        items = [
            {"id": "drop", "kind": "tool_result", "content": "d" * 300, "recoverable": True},
            {"id": "anchor", "kind": "tool_result", "content": "a" * 1000, "recoverable": True},
        ]
        result = context.curate_context(goal="g", items=items, preserve_tail=0, mode="shadow", engine_factory=lambda: fake)
        self.assertEqual([d["action"] for d in result["decisions"]], ["KEEP_EXACT", "KEEP_EXACT"])
        self.assertEqual([d["proposed_action"] for d in result["decisions"]], ["DROP", "ANCHOR"])
        self.assertEqual([x["content"] for x in result["curated_items"]], ["d" * 300, "a" * 1000])
        self.assertEqual(result["stats"]["applied_reduction_ratio"], 0.0)
        self.assertGreater(result["stats"]["proposed_reduction_ratio"], 0.0)
    def test_failure_lease_pins_until_verify_pass(self):
        answers = self.semantic_batch([(0.05, 0.05, 0.95, 0.05)])
        fake = FakeCurationEngine([answers])
        result = context.curate_context(
            goal="fix",
            items=[{"id": "failure", "kind": "tool_result", "content": "AssertionError: broken", "recoverable": False}],
            preserve_tail=0,
            engine_factory=lambda: fake,
        )
        self.assertEqual(result["decisions"][0]["action"], "PIN")
        self.assertEqual(result["decisions"][0]["lease"], "until_verification_pass")

        lifecycle.record_verification(value="PASS", contract="verify/v1", confidence=1.0, probabilities={"PASS": 1.0})
        fake2 = FakeCurationEngine([answers])
        result2 = context.curate_context(
            goal="fix",
            items=[{"id": "failure", "kind": "tool_result", "content": "AssertionError: broken", "recoverable": False}],
            preserve_tail=0,
            engine_factory=lambda: fake2,
        )
        self.assertEqual(result2["decisions"][0]["action"], "KEEP_EXACT")
        self.assertEqual(result2["decisions"][0]["basis"], "safety:unrecoverable")

    def test_batches_four_candidates_per_request_and_preserves_order(self):
        batches = []
        for size in (4, 4, 2):
            batches.append(self.semantic_batch([(0.9, 0.9, 0.1, 0.1)] * size))
        fake = FakeCurationEngine(batches)
        items = [{"id": f"i{i:02d}", "kind": "tool_result", "content": f"value-{i}", "recoverable": True} for i in range(10)]
        result = context.curate_context(goal="g", items=items, preserve_tail=0, engine_factory=lambda: fake)
        self.assertEqual(len(fake.calls), 3)
        self.assertLessEqual(max(len(call["questions"]) for call in fake.calls), 16)
        self.assertIn("i00", fake.calls[0]["questions"]["needed_0"]["instructions"])
        self.assertIn("i08", fake.calls[2]["questions"]["needed_0"]["instructions"])
        self.assertEqual([x["id"] for x in result["curated_items"]], [f"i{i:02d}" for i in range(10)])
        self.assertEqual(result["stats"]["provider_requests"], 3)

    def test_rejects_duplicate_ids_and_invalid_kinds(self):
        with self.assertRaises(ValueError):
            context.curate_context(goal="g", items=[{"id": "x", "content": "a"}, {"id": "x", "content": "b"}])
        with self.assertRaises(ValueError):
            context.curate_context(goal="g", items=[{"id": "x", "kind": "bogus", "content": "a"}])



class ToolTests(unittest.TestCase):
    def test_context_curate_handler_returns_structured_result(self):
        original = tools.curate_context
        tools.curate_context = lambda **kwargs: {"contract": "context-curation/v2", "stats": {"mode": "apply"}}
        try:
            payload = json.loads(tools.nerve_context_curate({"goal": "g", "items": [{"id": "x", "content": "y"}]}))
        finally:
            tools.curate_context = original
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["stats"]["mode"], "apply")
        self.assertEqual(payload["execution"]["engine"], "hermes-nerve")

    def test_assess_handler_returns_structured_result(self):
        original = tools._engine_factory
        tools._engine_factory = lambda: engine.DecisionEngine(FakeProvider(answers={"q": {"type": "noul", "noul": 0.8}}))
        try:
            with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, {"HERMES_NERVE_RECEIPTS": str(Path(td) / "r.jsonl")}, clear=False):
                payload = json.loads(tools.nerve_assess({"state": "x", "questions": {"q": {"type": "noul", "instructions": "Is this true?"}}}))
        finally:
            tools._engine_factory = original
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["answers"]["q"]["noul"], 0.8)
        self.assertTrue(payload["execution"]["live_provider_call"])

    def test_verify_updates_lifecycle(self):
        lifecycle.reset()
        original = tools._engine_factory
        tools._engine_factory = lambda: engine.DecisionEngine(FakeProvider(choice="PASS", confidence=0.99, probabilities={"PASS": 0.99, "RETRY": 0.01, "REPLAN": 0.0, "ESCALATE": 0.0}))
        try:
            with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, {"HERMES_NERVE_RECEIPTS": str(Path(td) / "r.jsonl")}, clear=False):
                payload = json.loads(tools.nerve_verify({"state": {}, "instructions": "verify"}))
        finally:
            tools._engine_factory = original
        self.assertTrue(payload["ok"])
        self.assertTrue(lifecycle.verification_passed())

    def test_rehydrate_is_local_and_uses_ledger(self):
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, {"HERMES_NERVE_CONTEXT_LEDGER": str(Path(td) / "ledger.jsonl")}, clear=False):
            ledger.configure(enabled=True, detail="sanitized")
            ledger.record_evidence(evidence_id="e1", content="[REDACTED] useful", kind="tool_result", recoverable=True)
            payload = json.loads(tools.nerve_context_rehydrate({"evidence_id": "e1"}))
            self.assertTrue(payload["ok"])
            self.assertIn("[REDACTED]", payload["rehydrated"]["content"])
            self.assertFalse(payload["execution"]["live_provider_call"])
            self.assertEqual(payload["execution"]["transport"], "local-evidence-ledger")
        ledger.configure(enabled=False, detail="sanitized")


class GateTests(unittest.TestCase):
    def setUp(self):
        gate._configured_mode = None
        gate._configured_min_confidence = None
        gate._configured_min_allow_probability = None
        gate._configured_scope = None

    def test_gate_observations_remain_complete_under_parallel_writes(self):
        from concurrent.futures import ThreadPoolExecutor
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, {
            "HERMES_NERVE_GATE_MODE": "off",
            "HERMES_NERVE_GATE_EVENTS": str(Path(td) / "gate.jsonl"),
        }, clear=False):
            with ThreadPoolExecutor(max_workers=8) as pool:
                list(pool.map(
                    lambda idx: gate.pre_tool_call(
                        "terminal", {"command": "echo hi"}, f"task-{idx}",
                        turn_id=f"turn-{idx}", session_id="session", tool_call_id=f"call-{idx}",
                    ),
                    range(200),
                ))
            report = gate.report(Path(td) / "gate.jsonl", recent_limit=0)
            self.assertEqual(report["hook_observations"], 200)
            self.assertEqual(report["disabled"], 200)

    def test_gate_off_never_calls_provider(self):
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, {
            "HERMES_NERVE_GATE_MODE": "off",
            "HERMES_NERVE_GATE_EVENTS": str(Path(td) / "gate.jsonl"),
        }, clear=False):
            self.assertIsNone(gate.evaluate_tool_call(tool_name="terminal", args={}, task_id="t", engine_factory=lambda: (_ for _ in ()).throw(AssertionError())))
            self.assertIsNone(gate.pre_tool_call(
                "terminal", {"command": "echo hi"}, "task",
                turn_id="turn-1", session_id="session-1", tool_call_id="call-1",
            ))
            report = gate.report(Path(td) / "gate.jsonl")
            self.assertEqual(report["hook_observations"], 1)
            self.assertEqual(report["disabled"], 1)
            self.assertEqual(report["provider_calls"], 0)
            row = report["recent"][0]
            self.assertEqual(row["turn_id"], "turn-1")
            self.assertEqual(row["tool_call_id"], "call-1")

    def test_selective_scope_bypasses_known_read_only_tools_without_provider(self):
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, {
            "HERMES_NERVE_GATE_MODE": "advisory",
            "HERMES_NERVE_GATE_SCOPE": "selective",
            "HERMES_NERVE_GATE_EVENTS": str(Path(td) / "gate.jsonl"),
        }, clear=False):
            original = gate.evaluate_tool_call
            gate.evaluate_tool_call = lambda **kwargs: (_ for _ in ()).throw(AssertionError("provider should not run"))
            try:
                self.assertIsNone(gate.pre_tool_call("read_file", {"path": "README.md"}, "t"))
                self.assertIsNone(gate.pre_tool_call("terminal", {"command": "git status --short"}, "t"))
                self.assertIsNone(gate.pre_tool_call("terminal", {"command": "rg TODO README.md"}, "t"))
            finally:
                gate.evaluate_tool_call = original
            report = gate.report(Path(td) / "gate.jsonl")
            self.assertEqual(report["bypassed"], 3)
            self.assertEqual(report["provider_calls"], 0)
            self.assertEqual(report["estimated_provider_calls_avoided"], 3)

    def test_selective_terminal_bypass_is_conservative(self):
        with patch.dict(os.environ, {"HERMES_NERVE_GATE_SCOPE": "selective"}, clear=False):
            self.assertEqual(gate.bypass_reason("terminal", {"command": "pwd"}), "read-only-terminal")
            self.assertEqual(gate.bypass_reason("terminal", {"command": "git diff -- README.md"}), "read-only-terminal")
            for command in (
                "git push origin main",
                "date --set 2030-01-01",
                "nvidia-smi -pl 200",
                "env rm -f victim.txt",
                "PATH=/tmp ls",
                "/tmp/ls",
                "rg --pre 'rm -f victim.txt' pattern .",
                "git diff --ext-diff",
                "cat input.txt > output.txt",
                "ls | head",
            ):
                self.assertIsNone(gate.bypass_reason("terminal", {"command": command}), command)

    def test_gate_scope_all_preserves_evaluate_everything_compatibility(self):
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, {
            "HERMES_NERVE_GATE_MODE": "advisory",
            "HERMES_NERVE_GATE_SCOPE": "all",
            "HERMES_NERVE_GATE_EVENTS": str(Path(td) / "gate.jsonl"),
        }, clear=False):
            calls = []
            original = gate.evaluate_tool_call
            gate.evaluate_tool_call = lambda **kwargs: (calls.append(kwargs) or engine.DecisionResult(
                "ALLOW", 0.99, {"ALLOW": 0.99}, "jev-test", 123.0, "hermes/pre-tool-gate/v1"
            ))
            try:
                self.assertIsNone(gate.pre_tool_call("read_file", {"path": "README.md"}, "t"))
            finally:
                gate.evaluate_tool_call = original
            self.assertEqual(len(calls), 1)
            report = gate.report(Path(td) / "gate.jsonl")
            self.assertEqual(report["evaluated"], 1)
            self.assertEqual(report["provider_calls"], 1)
            self.assertEqual(report["provider_latency_ms"], 123.0)

    def test_jev_internal_tools_never_recurse_even_in_all_scope(self):
        with patch.dict(os.environ, {"HERMES_NERVE_GATE_SCOPE": "all"}, clear=False):
            self.assertEqual(gate.bypass_reason("nerve_verify", {}), "nerve-internal")

    def test_evaluated_event_logs_answer_distribution(self):
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, {
            "HERMES_NERVE_GATE_MODE": "advisory",
            "HERMES_NERVE_GATE_EVENTS": str(Path(td) / "gate.jsonl"),
        }, clear=False):
            original = gate.evaluate_tool_call
            gate.evaluate_tool_call = lambda **kwargs: engine.DecisionResult(
                "ALLOW", 0.97, {"ALLOW": 0.62, "APPROVAL": 0.3, "BLOCK": 0.08}, "jev-test", 1.0, "x")
            try:
                self.assertIsNone(gate.pre_tool_call("terminal", {"command": "make build"}, "t"))
            finally:
                gate.evaluate_tool_call = original
            row = [r for r in read_jsonl(Path(td) / "gate.jsonl") if r["action"] == "evaluated"][-1]
            self.assertEqual(row["probabilities"], {"ALLOW": 0.62, "APPROVAL": 0.3, "BLOCK": 0.08})
            self.assertEqual(row["p_top"], 0.62)
            self.assertEqual(row["margin"], 0.32)
            self.assertEqual(row["confidence"], 0.97)

    def test_gate_event_tolerates_missing_or_malformed_probabilities(self):
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, {
            "HERMES_NERVE_GATE_EVENTS": str(Path(td) / "gate.jsonl"),
        }, clear=False):
            gate._record_gate_event(tool_name="terminal", action="evaluated", reason="r", provider_call=True,
                                    probabilities=None)
            gate._record_gate_event(tool_name="terminal", action="evaluated", reason="r", provider_call=True,
                                    probabilities={
                                        "ALLOW": "nan?",
                                        "APPROVAL": float("nan"),
                                        "BLOCK": 0.4,
                                        "NEGATIVE": -0.1,
                                        "TOO_HIGH": 1.1,
                                        "INFINITE": float("inf"),
                                    })
            rows = read_jsonl(Path(td) / "gate.jsonl")
            self.assertNotIn("probabilities", rows[0])
            self.assertEqual(rows[1]["probabilities"], {"BLOCK": 0.4})
            self.assertEqual(rows[1]["p_top"], 0.4)
            self.assertNotIn("margin", rows[1])

    def test_enforce_block(self):
        with patch.dict(os.environ, {"HERMES_NERVE_GATE_MODE": "enforce"}, clear=False):
            original = gate.evaluate_tool_call
            gate.evaluate_tool_call = lambda **kwargs: engine.DecisionResult("BLOCK", 0.95, {"BLOCK": 0.95}, "jev-test", 1.0, "x")
            try:
                decision = gate.pre_tool_call("terminal", {"command": "fixture-command"}, "t")
            finally:
                gate.evaluate_tool_call = original
            self.assertEqual(decision["action"], "block")

    def test_enforce_low_confidence_goes_to_human(self):
        with patch.dict(os.environ, {"HERMES_NERVE_GATE_MODE": "enforce", "HERMES_NERVE_MIN_CONFIDENCE": "0.80"}, clear=False):
            original = gate.evaluate_tool_call
            gate.evaluate_tool_call = lambda **kwargs: engine.DecisionResult("ALLOW", 0.51, {"ALLOW": 0.51}, "jev-test", 1.0, "x")
            try:
                decision = gate.pre_tool_call("terminal", {"command": "echo hi"}, "t")
            finally:
                gate.evaluate_tool_call = original
            self.assertEqual(decision["action"], "approve")

    def test_provider_failure_fails_to_human_in_enforce(self):
        with patch.dict(os.environ, {"HERMES_NERVE_GATE_MODE": "enforce"}, clear=False):
            original = gate.evaluate_tool_call
            gate.evaluate_tool_call = lambda **kwargs: (_ for _ in ()).throw(RuntimeError("down"))
            try:
                decision = gate.pre_tool_call("terminal", {}, "t")
            finally:
                gate.evaluate_tool_call = original
            self.assertEqual(decision["action"], "approve")

    def test_enforce_low_allow_probability_goes_to_human_even_when_confident(self):
        # Labeled-replay shape (keeltrace/hermes-nerve#19): a dangerous call that
        # reaches an ALLOW verdict with high calibration confidence but a
        # middling answer probability (held-out chmod 0.81 cluster).
        with patch.dict(os.environ, {"HERMES_NERVE_GATE_MODE": "enforce"}, clear=False):
            original = gate.evaluate_tool_call
            gate.evaluate_tool_call = lambda **kwargs: engine.DecisionResult(
                "ALLOW", 0.95, {"ALLOW": 0.81, "APPROVAL": 0.1, "BLOCK": 0.09}, "jev-test", 1.0, "x")
            try:
                decision = gate.pre_tool_call("terminal", {"command": "chmod 644 ~/.ssh/id_ed25519"}, "t")
            finally:
                gate.evaluate_tool_call = original
            self.assertEqual(decision["action"], "approve")
            self.assertEqual(decision["rule_key"], "nerve:low-allow-probability")

    def test_enforce_allow_passes_with_high_answer_probability(self):
        with patch.dict(os.environ, {"HERMES_NERVE_GATE_MODE": "enforce"}, clear=False):
            original = gate.evaluate_tool_call
            gate.evaluate_tool_call = lambda **kwargs: engine.DecisionResult(
                "ALLOW", 0.93, {"ALLOW": 0.93, "APPROVAL": 0.05, "BLOCK": 0.02}, "jev-test", 1.0, "x")
            try:
                decision = gate.pre_tool_call("terminal", {"command": "make build"}, "t")
            finally:
                gate.evaluate_tool_call = original
            self.assertIsNone(decision)

    def test_enforce_allow_threshold_is_configurable(self):
        with patch.dict(os.environ, {
            "HERMES_NERVE_GATE_MODE": "enforce",
            "HERMES_NERVE_MIN_ALLOW_PROBABILITY": "0.75",
        }, clear=False):
            original = gate.evaluate_tool_call
            gate.evaluate_tool_call = lambda **kwargs: engine.DecisionResult(
                "ALLOW", 0.9, {"ALLOW": 0.81, "APPROVAL": 0.1, "BLOCK": 0.09}, "jev-test", 1.0, "x")
            try:
                decision = gate.pre_tool_call("terminal", {"command": "make build"}, "t")
            finally:
                gate.evaluate_tool_call = original
            self.assertIsNone(decision)

    def test_min_allow_probability_rejects_non_finite_configuration(self):
        for raw in ("nan", "inf", "-inf"):
            gate._configured_min_allow_probability = None
            with patch.dict(os.environ, {"HERMES_NERVE_MIN_ALLOW_PROBABILITY": raw}, clear=False):
                self.assertAlmostEqual(gate.minimum_allow_probability(), 0.90)

        for raw in (float("nan"), float("inf"), float("-inf")):
            gate.configure(mode="enforce", min_confidence=0.80, min_allow_probability=raw, scope="selective")
            self.assertAlmostEqual(gate.minimum_allow_probability(), 0.90)

    def test_enforce_allow_without_distribution_falls_back_to_confidence(self):
        # Providers that return no usable distribution keep the shipped
        # confidence-only semantics rather than failing every allow.
        cases = [
            (engine.DecisionResult("ALLOW", 0.95, {}, "jev-test", 1.0, "x"), None),
            (engine.DecisionResult("ALLOW", 0.51, {}, "jev-test", 1.0, "x"), "approve"),
            (engine.DecisionResult("ALLOW", 0.95, {"ALLOW": "nan"}, "jev-test", 1.0, "x"), None),
            (engine.DecisionResult("ALLOW", 0.95, {"ALLOW": 1.4}, "jev-test", 1.0, "x"), None),
        ]
        for result, expected in cases:
            with patch.dict(os.environ, {"HERMES_NERVE_GATE_MODE": "enforce"}, clear=False):
                original = gate.evaluate_tool_call
                gate.evaluate_tool_call = lambda **kwargs: result
                try:
                    decision = gate.pre_tool_call("terminal", {"command": "make build"}, "t")
                finally:
                    gate.evaluate_tool_call = original
            if expected is None:
                self.assertIsNone(decision)
            else:
                self.assertEqual(decision["action"], expected)
                self.assertEqual(decision["rule_key"], "nerve:low-confidence")

    def test_enforce_block_and_approval_verdicts_ignore_allow_probability_gate(self):
        # p(ALLOW) only guards the automatic-allow path; BLOCK still blocks and
        # APPROVAL still asks regardless of the distribution.
        cases = [
            (engine.DecisionResult("BLOCK", 0.95, {"BLOCK": 0.5, "ALLOW": 0.5}, "jev-test", 1.0, "x"), "block"),
            (engine.DecisionResult("APPROVAL", 0.95, {"APPROVAL": 0.9, "ALLOW": 0.1}, "jev-test", 1.0, "x"), "approve"),
        ]
        for result, expected in cases:
            with patch.dict(os.environ, {"HERMES_NERVE_GATE_MODE": "enforce"}, clear=False):
                original = gate.evaluate_tool_call
                gate.evaluate_tool_call = lambda **kwargs: result
                try:
                    decision = gate.pre_tool_call("terminal", {"command": "fixture-command"}, "t")
                finally:
                    gate.evaluate_tool_call = original
            self.assertEqual(decision["action"], expected)


class RegistrationTests(unittest.TestCase):
    def setUp(self):
        # These tests exercise the controller/non-headless registration surface.
        env = patch.dict(
            os.environ,
            {"HERMES_KANBAN_TASK": "", "HERMES_KANBAN_TASK_ID": ""},
            clear=False,
        )
        env.start()
        self.addCleanup(env.stop)

    def test_registers_vnext_tools_hooks_context_engine_and_config(self):
        root = Path(__file__).resolve().parents[1]
        spec = importlib.util.spec_from_file_location("hermes_nerve_plugin", root / "__init__.py", submodule_search_locations=[str(root)])
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)

        class Ctx:
            def __init__(self):
                self.tools = []
                self.hooks = []
                self.context_engine = None

            def get_config(self, key, default=None):
                reserved = {"model", "plugins", "security", "settings"}
                if not isinstance(key, str) or not key or key.split(".", 1)[0].lower() in reserved:
                    raise ValueError("invalid plugin-relative config key")
                return {
                    "model_id": "typesafe/jev-legacy",
                    "jev_model": "typesafe/jev-custom",
                    "opencode_model": "jev-1.13",
                    "timeout_seconds": 6.0,
                    "gate_mode": "advisory",
                    "gate_scope": "selective",
                    "min_confidence": 0.91,
                    "min_allow_probability": 0.95,
                    "receipt_detail": "sanitized",
                    "context_preview_chars": 900,
                    "context_anchor_chars": 120,
                    "context_preserve_tail": 2,
                    "context_curation_mode": "shadow",
                    "context_ledger_enabled": False,
                    "context_drop_max_needed": 0.11,
                    "context_engine_mode": "apply",
                    "context_engine_threshold_percent": 0.68,
                    "context_engine_fallback_builtin": False,
                }.get(key, default)

            def register_tool(self, **kwargs):
                self.tools.append(kwargs)

            def register_hook(self, name, callback):
                self.hooks.append((name, callback))

            def register_context_engine(self, engine_obj):
                self.context_engine = engine_obj

        ctx = Ctx()
        module.register(ctx)
        self.assertEqual(
            {x["name"] for x in ctx.tools},
            {"nerve_decide", "nerve_rank", "nerve_verify", "nerve_assess", "nerve_context_curate", "nerve_context_rehydrate", "nerve_stats", "nerve_nervous_event", "nerve_supervise_card", "nerve_work_event", "nerve_work_status", "nerve_remote_delegate_task", "nerve_remote_worker_status", "nerve_remote_worker_result", "nerve_remote_worker_cancel", "nerve_remote_worker_control"},
        )
        self.assertEqual([x[0] for x in ctx.hooks], ["pre_tool_call", "post_tool_call", "post_tool_call", "post_tool_call", "pre_llm_call", "transform_tool_result", "pre_verify", "post_api_request", "api_request_error", "post_llm_call", "on_session_end"])
        self.assertTrue(all(callable(x[1]) for x in ctx.hooks))
        self.assertEqual(module.gate.gate_mode(), "advisory")
        self.assertEqual(module.gate.gate_scope(), "selective")
        self.assertAlmostEqual(module.gate.minimum_confidence(), 0.91)
        self.assertAlmostEqual(module.gate.minimum_allow_probability(), 0.95)
        self.assertEqual(module.receipts.receipt_detail(), "sanitized")
        self.assertEqual(module.client._configured_model, "typesafe/jev-custom")
        self.assertEqual(module.client._configured_opencode_model, "jev-1.13")
        self.assertEqual(module.client._configured_timeout, 6.0)
        self.assertEqual(module.context._configured_preview_chars, 900)
        self.assertEqual(module.context._configured_anchor_chars, 120)
        self.assertEqual(module.context._configured_preserve_tail, 2)
        self.assertEqual(module.context._configured_mode, "shadow")
        self.assertAlmostEqual(module.context._configured_drop_max_needed, 0.11)
        self.assertEqual(type(ctx.context_engine).__name__, "NerveContextEngine")
        self.assertEqual(ctx.context_engine.name, "jev")
        self.assertAlmostEqual(ctx.context_engine.threshold_percent, 0.68)
        self.assertFalse(ctx.context_engine.fallback_builtin)

    def test_register_uses_min_allow_probability_env_as_config_fallback(self):
        root = Path(__file__).resolve().parents[1]
        spec = importlib.util.spec_from_file_location("hermes_nerve_plugin_env", root / "__init__.py", submodule_search_locations=[str(root)])
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)

        class Ctx:
            def __init__(self):
                self.tools = []
                self.hooks = []
                self.context_engine = None

            def get_config(self, key, default=None):
                return default

            def register_tool(self, **kwargs):
                self.tools.append(kwargs)

            def register_hook(self, name, callback):
                self.hooks.append((name, callback))

            def register_context_engine(self, engine_obj):
                self.context_engine = engine_obj

        with patch.dict(os.environ, {"HERMES_NERVE_MIN_ALLOW_PROBABILITY": "0.75"}, clear=False):
            module.register(Ctx())
            self.assertAlmostEqual(module.gate.minimum_allow_probability(), 0.75)

    def test_fresh_install_context_defaults_are_shadow(self):
        root = Path(__file__).resolve().parents[1]
        spec = importlib.util.spec_from_file_location("hermes_nerve_plugin_defaults", root / "__init__.py", submodule_search_locations=[str(root)])
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)

        class Ctx:
            def __init__(self):
                self.context_engine = None
            def get_config(self, key, default=None):
                return default
            def register_tool(self, **kwargs):
                pass
            def register_hook(self, *args):
                pass
            def register_context_engine(self, engine_obj):
                self.context_engine = engine_obj

        ctx = Ctx()
        module.register(ctx)
        self.assertEqual(module.context._configured_mode, "shadow")
        self.assertEqual(module.gate.gate_scope(), "selective")
        self.assertIsNotNone(ctx.context_engine)
        self.assertEqual(ctx.context_engine.mode, "shadow")

    def test_legacy_model_id_is_still_accepted(self):
        root = Path(__file__).resolve().parents[1]
        spec = importlib.util.spec_from_file_location("hermes_nerve_plugin_legacy", root / "__init__.py", submodule_search_locations=[str(root)])
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)

        class Ctx:
            def get_config(self, key, default=None):
                if key == "model_id":
                    return "typesafe/jev-legacy"
                return default
            def register_tool(self, **kwargs): pass
            def register_hook(self, *args): pass

        module.register(Ctx())
        self.assertEqual(module.client._configured_model, "typesafe/jev-legacy")


class ProvenanceAndLedgerTests(unittest.TestCase):
    def test_decision_result_has_unmistakable_live_provenance(self):
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, {"HERMES_NERVE_RECEIPTS": str(Path(td) / "r.jsonl")}, clear=False):
            result = engine.DecisionEngine(FakeProvider(choice="A", confidence=0.9, probabilities={"A": 0.9, "B": 0.1})).decide(
                state={}, instructions="choose", choices=["A", "B"]
            ).as_dict()
        self.assertEqual(result["execution"]["engine"], "hermes-nerve")
        self.assertEqual(result["execution"]["version"], "0.3.1rc1")
        self.assertEqual(result["execution"]["transport"], "openrouter-decisions")
        self.assertTrue(result["execution"]["live_provider_call"])

    def test_post_tool_observer_records_sanitized_recoverable_evidence(self):
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, {"HERMES_NERVE_CONTEXT_LEDGER": str(Path(td) / "ledger.jsonl")}, clear=False):
            ledger.configure(enabled=True, detail="sanitized")
            ledger.observe_tool_call(
                tool_name="terminal",
                args={"command": "git status --short"},
                result="[REDACTED]\n M file.py",
                task_id="t1",
                duration_ms=12,
            )
            rows = [json.loads(x) for x in Path(os.environ["HERMES_NERVE_CONTEXT_LEDGER"]).read_text().splitlines()]
            self.assertEqual(len(rows), 1)
            self.assertTrue(rows[0]["recoverable"])
            self.assertIn("[REDACTED]", rows[0]["content"])
            self.assertNotIn("abcdefghijklmnop", rows[0]["content"])
        ledger.configure(enabled=False, detail="sanitized")


    def test_profile_report_home_infers_named_profile_from_plugin_path(self):
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, {}, clear=True):
            plugin = Path(td) / ".hermes" / "profiles" / "muna" / "plugins" / "hermes-nerve"
            plugin.mkdir(parents=True)
            inferred = paths.report_home(plugin)
            self.assertEqual(inferred, Path(td) / ".hermes" / "profiles" / "muna")

    def test_profile_report_home_uses_deepest_profile_inside_profile_scratch(self):
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, {}, clear=True):
            outer = Path(td) / ".hermes" / "profiles" / "freebrain" / "cache" / "scratch" / "tmp"
            plugin = outer / ".hermes" / "profiles" / "muna" / "plugins" / "hermes-nerve"
            plugin.mkdir(parents=True)
            (plugin / "plugin.yaml").write_text("name: hermes-nerve\n")
            inferred = paths.report_home(plugin)
            self.assertEqual(inferred, outer / ".hermes" / "profiles" / "muna")

    def test_profile_report_home_ignores_nested_vendor_profiles_directory(self):
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, {}, clear=True):
            profile = Path(td) / ".hermes" / "profiles" / "muna"
            plugin = profile / "plugins" / "hermes-nerve"
            nested = plugin / "vendor" / "profiles" / "not-a-hermes-profile"
            nested.mkdir(parents=True)
            inferred = paths.report_home(nested)
            self.assertEqual(inferred, profile)

    def test_profile_report_home_ignores_unmarked_nested_dot_hermes_profile(self):
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, {}, clear=True):
            profile = Path(td) / ".hermes" / "profiles" / "muna"
            plugin = profile / "plugins" / "hermes-nerve"
            nested = plugin / "node_modules" / ".hermes" / "profiles" / "evil"
            nested.mkdir(parents=True)
            inferred = paths.report_home(nested)
            self.assertEqual(inferred, profile)

    def test_profile_report_home_does_not_infer_unrelated_profiles_directory(self):
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, {}, clear=True):
            unrelated = Path(td) / "repo" / "profiles" / "dev" / "plugins" / "hermes-nerve"
            unrelated.mkdir(parents=True)
            self.assertIsNone(paths.infer_profile_home_from_path(unrelated))

    def test_profile_report_home_does_not_infer_nonexistent_profile(self):
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, {}, clear=True):
            ghost = Path(td) / ".hermes" / "profiles" / "ghost" / "plugins" / "hermes-nerve"
            self.assertIsNone(paths.infer_profile_home_from_path(ghost))

    def test_profile_report_home_accepts_existing_fresh_default_root_profile(self):
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, {}, clear=True):
            profile = Path(td) / ".hermes" / "profiles" / "brandnew"
            profile.mkdir(parents=True)
            self.assertEqual(paths.infer_profile_home_from_path(profile), profile)
            self.assertEqual(paths.report_home(profile), profile)

    def test_profile_report_home_accepts_marked_alternate_root_profile(self):
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, {}, clear=True):
            profile = Path(td) / "hermesdata" / "profiles" / "muna"
            plugin = profile / "plugins" / "hermes-nerve"
            plugin.mkdir(parents=True)
            (plugin / "plugin.yaml").write_text("name: hermes-nerve\n")
            self.assertEqual(paths.infer_profile_home_from_path(plugin), profile)
            self.assertEqual(paths.report_home(plugin), profile)

    def test_receipt_report_aggregates_cost_tokens_and_contracts(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "receipts.jsonl"
            rows = [
                {
                    "timestamp": "2026-09-17T00:00:00Z", "contract": "decision/v1", "model": "jev-test", "latency_ms": 100,
                    "execution": {"live_provider_call": True},
                    "result": {"value": "A", "request_id": "r1", "usage": {"cost": 0.00001, "input_tokens": 10, "output_tokens": 2}},
                },
                {
                    "timestamp": "2026-09-17T00:00:01Z", "contract": "verify/v1", "model": "jev-test", "latency_ms": 300,
                    "execution": {"live_provider_call": True},
                    "result": {"value": "PASS", "request_id": "r2", "usage": {"cost": 0.00002, "input_tokens": 20, "output_tokens": 3}},
                },
            ]
            path.write_text("\n".join(json.dumps(x) for x in rows) + "\n")
            report = receipts.report(path, recent_limit=1)
            self.assertEqual(report["receipt_count"], 2)
            self.assertEqual(report["provider_calls"], 2)
            self.assertEqual(report["input_tokens"], 30)
            self.assertEqual(report["output_tokens"], 5)
            self.assertAlmostEqual(report["total_cost"], 0.00003)
            self.assertEqual(report["average_latency_ms"], 200.0)
            self.assertEqual(report["by_contract"], {"decision/v1": 1, "verify/v1": 1})
            self.assertEqual(len(report["recent"]), 1)
            self.assertEqual(report["recent"][0]["value"], "PASS")
            self.assertEqual(report["provider_cost_missing_calls"], 0)

    def test_receipt_report_marks_missing_provider_cost_unknown(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "receipts.jsonl"
            rows = [
                {
                    "timestamp": "2026-09-20T00:00:00Z", "contract": "decision/v1", "model": "jev-test", "latency_ms": 100,
                    "execution": {"live_provider_call": True},
                    "result": {"value": "A", "request_id": "r1", "usage": {"cost": 0.00001, "input_tokens": 10, "output_tokens": 2}},
                },
                {
                    "timestamp": "2026-09-20T00:00:01Z", "contract": "verify/v1", "model": "jev-test", "latency_ms": 100,
                    "execution": {"live_provider_call": True},
                    "result": {"value": "PASS", "request_id": "r2", "usage": {"input_tokens": 20, "output_tokens": 3}},
                },
            ]
            path.write_text("\n".join(json.dumps(x) for x in rows) + "\n")
            report = receipts.report(path)
            self.assertIsNone(report["total_cost"])
            self.assertAlmostEqual(report["provider_reported_cost"], 0.00001)
            self.assertEqual(report["provider_cost_reported_calls"], 1)
            self.assertEqual(report["provider_cost_missing_calls"], 1)

    def test_stats_tool_is_local_and_combines_receipts_and_context(self):
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, {
            "HERMES_NERVE_RECEIPTS": str(Path(td) / "receipts.jsonl"),
            "HERMES_NERVE_CONTEXT_LEDGER": str(Path(td) / "ledger.jsonl"),
        }, clear=False):
            ledger.configure(enabled=True, detail="sanitized")
            receipts.write_receipt(
                contract="decision/v1", state={"x": 1},
                result={
                    "value": "A", "request_id": "r1",
                    "usage": {"cost": 0.00001, "input_tokens": 10, "output_tokens": 2},
                    "execution": {"live_provider_call": True},
                },
                model="jev-test", latency_ms=12.5,
            )
            ledger.record_evidence(evidence_id="e1", content="status", kind="tool_result", recoverable=True)
            payload = json.loads(tools.nerve_stats({"recent_limit": 2}))
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["receipts"]["receipt_count"], 1)
            self.assertEqual(payload["context"]["evidence_events"], 1)
            self.assertFalse(payload["execution"]["live_provider_call"])
            self.assertEqual(payload["execution"]["transport"], "local-telemetry")
        ledger.configure(enabled=False, detail="sanitized")
    def test_stats_default_is_bounded_and_recent_is_opt_in(self):
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, {
            "HERMES_NERVE_RECEIPTS": str(Path(td) / "receipts.jsonl"),
            "HERMES_NERVE_GATE_EVENTS": str(Path(td) / "gate.jsonl"),
            "HERMES_NERVE_CONTEXT_LEDGER": str(Path(td) / "ledger.jsonl"),
            "HERMES_NERVE_NERVOUS_EVENTS": str(Path(td) / "nervous.jsonl"),
            "HERMES_NERVE_OUTCOMES": str(Path(td) / "outcomes.jsonl"),
        }, clear=False):
            ledger.configure(enabled=True, detail="sanitized")
            for idx in range(80):
                ledger.record_evidence(
                    evidence_id=f"e{idx}", content=("x" * 400), kind="tool_result",
                    recoverable=True, action="OBSERVED",
                )
            raw = tools.nerve_stats({})
            payload = json.loads(raw)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["contract"], "stats/v2")
            self.assertEqual(payload["section"], "summary")
            self.assertLess(len(raw), 12000)
            self.assertNotIn("recent", payload["gate"])
            self.assertNotIn("recent_router", payload["nervous"])
            detailed = json.loads(tools.nerve_stats({"section": "gate", "include_recent": True, "recent_limit": 2}))
            self.assertEqual(detailed["section"], "gate")
            self.assertLessEqual(len(detailed["gate"]["recent"]), 2)
        ledger.configure(enabled=False, detail="sanitized")

    def test_explicit_rehydration_after_anchor_is_counted_as_recovery_demand(self):
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, {
            "HERMES_NERVE_CONTEXT_LEDGER": str(Path(td) / "ledger.jsonl"),
        }, clear=False):
            ledger.configure(enabled=True, detail="sanitized")
            ledger.record_evidence(
                evidence_id="anchored-evidence", content="important exact output",
                kind="tool_result", recoverable=True, action="ANCHOR",
            )
            restored = ledger.rehydrate("anchored-evidence")
            self.assertEqual(restored["content"], "important exact output")
            report = ledger.report()
            self.assertEqual(report["compacted_unique_evidence"], 1)
            self.assertEqual(report["rehydrations"], 1)
            self.assertEqual(report["rehydrated_compacted_evidence"], 1)
            self.assertEqual(report["recovery_demand_rate"], 1.0)
        ledger.configure(enabled=False, detail="sanitized")

    def test_ledger_hash_mode_refuses_fake_rehydration(self):
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, {"HERMES_NERVE_CONTEXT_LEDGER": str(Path(td) / "ledger.jsonl")}, clear=False):
            ledger.configure(enabled=True, detail="hash")
            ledger.record_evidence(evidence_id="e-hash", content="exact data", kind="tool_result", recoverable=True)
            with self.assertRaises(ValueError):
                ledger.rehydrate("e-hash")
        ledger.configure(enabled=False, detail="sanitized")


class JsonlDurabilityTests(unittest.TestCase):
    def test_parallel_appends_remain_parseable(self):
        from concurrent.futures import ThreadPoolExecutor
        from hermes_nerve.jsonl import append_jsonl, read_jsonl

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "parallel.jsonl"

            def write_row(idx):
                append_jsonl(path, {"idx": idx, "payload": "x" * 256})

            with ThreadPoolExecutor(max_workers=12) as pool:
                list(pool.map(write_row, range(600)))

            rows = read_jsonl(path)
            self.assertEqual(len(rows), 600)
            self.assertEqual({row["idx"] for row in rows}, set(range(600)))


class ContextEngineTests(unittest.TestCase):
    def test_shadow_engine_keeps_builtin_compression_alive(self):
        class Fallback:
            def compress(self, messages, **kwargs):
                return [messages[0], {"role": "assistant", "content": "fallback summary"}]

        e = NerveContextEngine(mode="shadow", threshold_percent=0.5, fallback_builtin=True)
        e.context_length = 1000
        e.threshold_tokens = 500
        e.last_prompt_tokens = 900
        e._fallback = Fallback()
        self.assertTrue(e.should_compress())
        out = e.compress([{"role": "user", "content": "long session"}], current_tokens=900)
        self.assertEqual(out[-1]["content"], "fallback summary")

    def test_shadow_engine_without_fallback_does_not_claim_noop_compression(self):
        e = NerveContextEngine(mode="shadow", threshold_percent=0.5, fallback_builtin=False)
        e.context_length = 1000
        e.threshold_tokens = 500
        e.last_prompt_tokens = 900
        e._fallback = None
        self.assertFalse(e.should_compress())

    def test_apply_engine_preserves_tool_protocol_by_anchoring_drop(self):
        e = NerveContextEngine(mode="apply", threshold_percent=0.5, protect_first_n=0, protect_last_n=1)
        e.update_model("model", 1000)
        messages = [
            {"role": "user", "content": "debug"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "terminal", "arguments": '{"command":"git status --short"}'}}]},
            {"role": "tool", "tool_call_id": "c1", "content": "old status output " * 100},
            {"role": "assistant", "content": "continuing"},
            {"role": "user", "content": "continue"},
        ]
        original = context.curate_context
        context.curate_context = lambda **kwargs: {
            "decisions": [{"id": kwargs["items"][0]["id"], "action": "DROP"}],
            "curated_items": [],
            "stats": {"applied_reduction_ratio": 0.9},
        }
        try:
            out = e.compress(messages, current_tokens=900)
        finally:
            context.curate_context = original
        self.assertEqual(len(out), len(messages))
        self.assertEqual(out[1]["tool_calls"][0]["id"], "c1")
        self.assertEqual(out[2]["tool_call_id"], "c1")
        self.assertIn("NERVE_CONTEXT_ANCHOR", out[2]["content"])
        self.assertEqual(e.compression_count, 1)
        self.assertEqual(e.last_prompt_tokens, -1)

    def test_context_engine_status_exposes_ledger_and_plan(self):
        ledger.configure(enabled=False, detail="sanitized")
        e = NerveContextEngine(mode="apply")
        e.update_model("model", 2000)
        status = e.get_status()
        self.assertEqual(status["engine"], "jev")
        self.assertEqual(status["mode"], "apply")
        self.assertIn("ledger", status)

    def test_context_engine_falls_back_when_no_jev_candidates(self):
        class Fallback:
            def __init__(self): self.calls = 0
            def compress(self, messages, **kwargs):
                self.calls += 1
                return [messages[0], {"role": "assistant", "content": "fallback summary"}]

        e = NerveContextEngine(mode="apply", protect_first_n=3, protect_last_n=6, fallback_builtin=True)
        e.context_length = 1000
        e.threshold_tokens = 500
        fb = Fallback()
        e._fallback = fb
        messages = [{"role": "user", "content": "text-only session"}]
        out = e.compress(messages, current_tokens=900)
        self.assertEqual(fb.calls, 1)
        self.assertEqual(out[-1]["content"], "fallback summary")
        self.assertEqual(e._last_plan["contract"], "context-engine/fallback-built-in/v1")
        self.assertEqual(e.compression_count, 1)

    def test_context_engine_fallback_can_be_disabled(self):
        e = NerveContextEngine(mode="apply", fallback_builtin=False)
        e._model = "m"
        e._build_fallback()
        self.assertIsNone(e._fallback)


class LiveSmokeTests(unittest.TestCase):
    def test_live_smoke_requires_explicit_api_key(self):
        root = Path(__file__).resolve().parents[1]
        smoke_path = root / "scripts" / "live_api_smoke.py"
        spec = importlib.util.spec_from_file_location("hermes_nerve_live_smoke", smoke_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(module.main(), 2)

    def test_live_suite_requires_explicit_api_key(self):
        root = Path(__file__).resolve().parents[1]
        suite_path = root / "scripts" / "live_api_suite.py"
        spec = importlib.util.spec_from_file_location("hermes_nerve_live_suite", suite_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(module.main(), 2)


if __name__ == "__main__":
    unittest.main()