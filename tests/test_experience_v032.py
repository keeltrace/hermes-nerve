from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from hermes_nerve import cli, experience, tools
from hermes_nerve.experience import (
    ExperienceStore,
    admission_decision,
    effective_status,
    scope_match_score,
)
from hermes_nerve.work.models import RunIdentity
from hermes_nerve.work.supervisor import CardSupervisor


class ExperienceV032Tests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.path = Path(self.td.name) / "experience.jsonl"
        self.store = ExperienceStore(self.path)

    def verified(self, **kwargs):
        base = dict(
            subject="retry-owner",
            statement="native completion owns terminal state",
            claim_type="verified",
            evidence_ids=["ev-1"],
            confidence=0.95,
            polarity=1,
            scope={"repository": "keeltrace/hermes-nerve", "branch": "main"},
            admit=False,
        )
        base.update(kwargs)
        return self.store.create_claim(**base)["claim"]

    def test_verified_and_disproven_claims_require_evidence(self):
        with self.assertRaisesRegex(ValueError, "require evidence"):
            self.store.create_claim(subject="x", statement="y", claim_type="verified", admit=False)
        with self.assertRaisesRegex(ValueError, "require evidence"):
            self.store.create_claim(subject="x", statement="y", claim_type="disproven", admit=False)
        claim = self.store.create_claim(
            subject="dispatcher-hypothesis",
            statement="the dispatcher is the source of the retry mutation",
            claim_type="disproven",
            evidence_ids=["test-318", "test-319"],
            confidence=0.98,
            polarity=-1,
            admit=False,
        )["claim"]
        self.assertEqual(claim["claim_type"], "disproven")
        self.assertEqual(claim["evidence_ids"], ["test-318", "test-319"])

    def test_claim_persistence_is_redacted(self):
        claim = self.store.create_claim(
            subject="provider",
            statement="Bearer abcdefghijklmnopqrstuvwxyz should never persist",
            claim_type="observed",
            evidence_ids=["ev"],
            confidence=0.8,
            provenance={"token": "secret-value", "safe": "yes"},
            admit=False,
        )["claim"]
        raw = self.path.read_text()
        self.assertNotIn("abcdefghijklmnopqrstuvwxyz", raw)
        self.assertNotIn("secret-value", raw)
        self.assertIn("[REDACTED]", raw)
        self.assertEqual(claim["provenance"]["token"], "[REDACTED]")

    def test_semantically_identical_claim_is_deduplicated(self):
        first = self.verified()
        second = self.store.create_claim(
            subject="retry-owner",
            statement="native completion owns terminal state",
            claim_type="verified",
            evidence_ids=["ev-1"],
            confidence=0.95,
            polarity=1,
            scope={"repository": "keeltrace/hermes-nerve", "branch": "main"},
            admit=False,
        )
        self.assertTrue(second["deduplicated"])
        self.assertEqual(second["claim"]["claim_id"], first["claim_id"])
        self.assertEqual(len(self.store.claims()), 1)

    def test_supersession_marks_prior_claim(self):
        first = self.verified()
        second = self.verified(
            statement="native completion owns terminal state after controller verification",
            evidence_ids=["ev-2"],
            supersedes=first["claim_id"],
        )
        claims = self.store.claims()
        self.assertEqual(claims[first["claim_id"]]["status"], "superseded")
        self.assertEqual(second["supersedes"], first["claim_id"])

    def test_conflict_detection_marks_both_claims(self):
        positive = self.verified(subject="openjev-available", statement="OpenJev is available", polarity=1)
        negative = self.store.create_claim(
            subject="openjev-available",
            statement="OpenJev is unavailable",
            claim_type="verified",
            evidence_ids=["ev-2"],
            confidence=0.92,
            polarity=-1,
            scope={"repository": "keeltrace/hermes-nerve", "branch": "main"},
            admit=False,
        )
        self.assertEqual(negative["conflicts"], [positive["claim_id"]])
        claims = self.store.claims()
        self.assertEqual(claims[positive["claim_id"]]["status"], "contradicted")
        self.assertEqual(claims[negative["claim"]["claim_id"]]["status"], "contradicted")

    def test_scope_isolation_and_temporal_expiration(self):
        expired_at = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        expired = self.verified(subject="old", statement="old behavior", valid_until=expired_at)
        active = self.verified(subject="new", statement="new behavior")
        self.assertEqual(effective_status(expired), "expired")
        results = self.store.retrieve(
            "behavior",
            scope={"repository": "keeltrace/hermes-nerve", "branch": "main"},
            record=False,
        )
        ids = {r["claim"]["claim_id"] for r in results}
        self.assertIn(active["claim_id"], ids)
        self.assertNotIn(expired["claim_id"], ids)
        self.assertEqual(
            self.store.retrieve("behavior", scope={"repository": "other/repo", "branch": "main"}, record=False),
            [],
        )

    def test_retrieval_exposes_deterministic_why_and_scores(self):
        claim = self.verified(estimated_tokens=80)
        result = self.store.retrieve(
            "native completion terminal state",
            scope={"repository": "keeltrace/hermes-nerve", "branch": "main"},
            record=True,
        )[0]
        self.assertEqual(result["claim"]["claim_id"], claim["claim_id"])
        self.assertLessEqual(result["score"], 1)
        self.assertEqual(result["score_components"]["verification"], 1.0)
        self.assertTrue(any("scope_match" in reason for reason in result["why_retrieved"]))
        explanation = self.store.explain(claim["claim_id"])
        self.assertEqual(explanation["evidence_ids"], ["ev-1"])
        self.assertTrue(explanation["latest_retrieval"]["why_retrieved"])

    def test_token_aware_admission_rejects_low_value_expensive_memory(self):
        decision = admission_decision(
            claim_type="inferred",
            confidence=0.2,
            evidence_count=0,
            estimated_tokens=4000,
            expected_reuse=0.1,
        )
        self.assertFalse(decision["admitted"])
        self.assertTrue(any("token_cost_penalty" in x for x in decision["reasons"]))

    def test_run_capsule_preserves_successor_state(self):
        checkpoint = {
            "checkpoint_id": "cp-1",
            "identity": {"task_id": "T1", "run_id": 7, "contract_hash": "abc"},
            "decision_id": "d-1",
            "reason": "handoff",
            "frontier": ["DOD-03"],
            "verified_criteria": ["DOD-01", "DOD-02"],
            "failed_criteria": ["DOD-03"],
            "artifacts": [{"commit": "123"}],
        }
        capsule = self.store.create_capsule(
            checkpoint,
            failed_paths=[{"fingerprint": "fp-a", "approach": "direct SQL"}],
            discoveries=[{"fact": "kanban_complete is canonical"}],
            evidence_ids=["ev-a"],
            next_move="verify DOD-03",
            scope={"repository": "keeltrace/hermes-nerve"},
        )
        self.assertEqual(capsule["verified_criteria"], ["DOD-01", "DOD-02"])
        self.assertEqual(capsule["unresolved"], ["DOD-03"])
        inherited = self.store.capsules(task_id="T1", scope={"repository": "keeltrace/hermes-nerve"})
        self.assertEqual(inherited[0]["next_move"], "verify DOD-03")
        self.assertEqual(inherited[0]["failed_paths"][0]["fingerprint"], "fp-a")

    def test_recovery_playbook_promotes_after_repeated_success(self):
        for i in range(3):
            self.store.record_recovery(
                fingerprint="same-failure",
                recovery="use repository-native test environment",
                successful=True,
                evidence_ids=[f"ev-{i}"],
                scope={"repository": "keeltrace/hermes-nerve"},
                tokens_saved=1000,
            )
        self.store.record_recovery(
            fingerprint="same-failure",
            recovery="use repository-native test environment",
            successful=False,
            scope={"repository": "keeltrace/hermes-nerve"},
        )
        found = self.store.lookup_recovery("same-failure", scope={"repository": "keeltrace/hermes-nerve"})
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["successes"], 3)
        self.assertEqual(found[0]["failures"], 1)
        self.assertIn("exact_failure_fingerprint", found[0]["why_retrieved"])

    def test_outcome_linking_changes_claim_history(self):
        claim = self.verified()
        self.store.record_outcome(
            claim["claim_id"], successful=True, decision_id="d-9", receipt_id="r-9", tokens_saved=18000
        )
        current = self.store.claims()[claim["claim_id"]]
        self.assertEqual(current["useful_outcomes"], 1)
        self.assertEqual(current["tokens_saved"], 18000)
        explanation = self.store.explain(claim["claim_id"])
        self.assertEqual(explanation["history"][-1]["decision_id"], "d-9")

    def test_reconciliation_is_advisory_and_non_mutating(self):
        past = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        self.verified(subject="stale", statement="stale fact", valid_until=past)
        before = self.path.read_bytes()
        report = self.store.reconcile(
            outcome_report={"metrics": {"useful_disagreements": 1, "useless_disagreements": 6}},
            receipt_report={"provider_calls": 20},
        )
        after = self.path.read_bytes()
        self.assertEqual(before, after)
        self.assertTrue(report["review_required"])
        self.assertFalse(report["mutated_policy"])
        kinds = {x["kind"] for x in report["recommendations"]}
        self.assertIn("retire_stale_claims", kinds)
        self.assertIn("raise_provider_admission_threshold", kinds)

    def test_scope_matching_global_and_mismatch(self):
        self.assertGreater(scope_match_score({}, {"repository": "x"}), 0)
        self.assertLess(scope_match_score({"repository": "x"}, {"repository": "y"}), 0)
        self.assertGreater(scope_match_score({"repository": "x"}, {"repository": "x"}), 0.9)

    def test_evidence_graph_and_hierarchical_summary(self):
        claim = self.verified()
        self.store.record_outcome(claim["claim_id"], successful=True, decision_id="d-1", receipt_id="r-1")
        checkpoint = {
            "checkpoint_id": "cp-1",
            "identity": {"task_id": "T1", "run_id": 1, "contract_hash": "c"},
            "verified_criteria": ["DOD-01"],
            "frontier": ["DOD-02"],
        }
        self.store.create_capsule(checkpoint, next_move="do DOD-02", scope={"repository": "keeltrace/hermes-nerve"})
        graph = self.store.evidence_graph(claim["claim_id"])
        relations = {edge["relation"] for edge in graph["edges"]}
        self.assertIn("supported_by", relations)
        self.assertIn("evaluated_by", relations)
        self.assertIn("attributed_to", relations)
        summary = self.store.hierarchical_summary(scope={"repository": "keeltrace/hermes-nerve"})
        self.assertEqual(summary["task"][0]["task_id"], "T1")
        self.assertEqual(summary["global_lessons"][0]["claim_id"], claim["claim_id"])

    def test_context_explain_tool_is_local_and_returns_retrieval_reason(self):
        with patch.dict(os.environ, {"HERMES_NERVE_EXPERIENCE": str(self.path)}, clear=False):
            claim = self.verified()
            with patch.object(tools, "_engine_factory", side_effect=AssertionError("provider path must not run")):
                payload = json.loads(tools.nerve_context_explain({
                    "query": "native completion",
                    "scope": {"repository": "keeltrace/hermes-nerve", "branch": "main"},
                }))
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["execution"]["live_provider_call"], False)
            self.assertEqual(payload["matches"][0]["claim"]["claim_id"], claim["claim_id"])
            self.assertTrue(payload["matches"][0]["why_retrieved"])

    def test_verified_work_fact_and_checkpoint_flow_into_experience(self):
        work_db = Path(self.td.name) / "work.sqlite3"
        with patch.dict(os.environ, {"HERMES_NERVE_EXPERIENCE": str(self.path)}, clear=False):
            experience.configure(enabled=True)
            self.addCleanup(lambda: experience.configure(enabled=False))
            sup = CardSupervisor(store_path=work_db, mode="shadow")
            bound = sup.bind_contract(
                task_id="task-x",
                goal="ship verified feature",
                criteria=[{"id": "c1", "description": "tests pass", "estimated_tokens": 500}],
                preflight_result={"value": "ACCEPT", "confidence": 1.0, "receipt_id": "preflight"},
            )
            ident = RunIdentity("task-x", 1, bound["contract"]["contract_hash"], "claim", "worker-x")
            sup.bind_run(ident)
            observed = sup.observe_evidence(ident, value={"tests": "pass"}, criterion_id="c1")
            evidence_id = observed["evidence"]["evidence_id"]
            sup.mark_deterministic_verdict(
                ident, "c1", passed=True, reason="controller test verifier", evidence_ids=[evidence_id]
            )
            sup.checkpoint_packet(
                ident,
                decision_id="decision-x",
                reason="handoff",
                current_plan="finish packaging",
                artifacts=[{"commit": "abc123"}],
            )
            persisted = ExperienceStore(self.path)
            claims = list(persisted.claims().values())
            self.assertTrue(any(c["subject"] == "dod:task-x:c1" and c["claim_type"] == "verified" for c in claims))
            capsules = persisted.capsules(task_id="task-x")
            self.assertEqual(len(capsules), 1)
            self.assertEqual(capsules[0]["verified_criteria"], ["c1"])
            self.assertEqual(capsules[0]["next_move"], "finish packaging")
            self.assertIn(evidence_id, capsules[0]["evidence_ids"])

    def test_experience_cli_reconcile_and_summary(self):
        with patch.dict(os.environ, {"HERMES_NERVE_EXPERIENCE": str(self.path)}, clear=False):
            self.verified()
            out = io.StringIO()
            with redirect_stdout(out):
                code = cli.main(["experience", "summary", "--scope", '{"repository":"keeltrace/hermes-nerve"}'])
            self.assertEqual(code, 0)
            payload = json.loads(out.getvalue())
            self.assertEqual(payload["schema"], "hermes-nerve-experience-summary/v1")

            out = io.StringIO()
            with redirect_stdout(out):
                code = cli.main(["experience", "reconcile"])
            self.assertEqual(code, 0)
            payload = json.loads(out.getvalue())
            self.assertEqual(payload["authority"], "advisory-only")
            self.assertFalse(payload["mutated_policy"])


if __name__ == "__main__":
    unittest.main()
