from __future__ import annotations
import unittest

from hermes_nerve.selective_context import (
    AdmissionAction, ContextAdmissionController, ContextCandidate,
    SelectiveContextConfig, SurfaceConfig, memory_candidate_from_experience,
    skill_candidate, task_fingerprint, tool_candidate,
)


class SelectiveContextV0321Tests(unittest.TestCase):
    def controller(self, kind="memory"):
        cfg=SelectiveContextConfig()
        setattr(cfg,kind,SurfaceConfig(mode="apply",max_candidates=20,max_admitted=2,min_score=.12))
        return ContextAdmissionController(cfg)

    def test_memory_selects_relevant_and_suppresses_irrelevant(self):
        c=self.controller("memory")
        r=c.plan(goal="review nerve release blockers",kind="memory",candidates=[
            ContextCandidate("memory","release","x","Nerve release policy","release blocker qualification",100,trust=1.0),
            ContextCandidate("memory","van","x","green van","vehicle maintenance",100,trust=.8),
            ContextCandidate("memory","duplicate","x","already here","release blocker",100,metadata={"already_present":True}),
        ])
        self.assertIn("release",r["selected_ids"]);self.assertIn("van",r["suppressed_ids"])
        self.assertIn("duplicate",r["suppressed_ids"]);self.assertGreater(r["net_tokens_saved"],0)

    def test_mandatory_never_suppressed_even_at_zero_budget(self):
        c=ContextAdmissionController(SelectiveContextConfig(memory=SurfaceConfig("apply",20,0,.99)))
        r=c.plan(goal="unrelated",kind="memory",candidates=[ContextCandidate("memory","policy",summary="mandatory policy",estimated_tokens=20,mandatory=True)])
        self.assertEqual(r["selected_ids"],["policy"]);self.assertEqual(r["decisions"][0]["action"],AdmissionAction.PIN.value)

    def test_tools_defer_instead_of_disappear(self):
        c=self.controller("tools")
        r=c.plan(goal="review github pull request",kind="tool",candidates=[
            ContextCandidate("tool","github_pr",summary="github pull request review",estimated_tokens=500,trust=1),
            ContextCandidate("tool","gmail_send",summary="send email",estimated_tokens=500,trust=1),
        ])
        self.assertIn("github_pr",r["selected_ids"]);self.assertIn("gmail_send",r["deferred_ids"])

    def test_skills_defer_instead_of_disappear(self):
        c=self.controller("skills")
        r=c.plan(goal="review python code",kind="skill",candidates=[
            ContextCandidate("skill","code-review",summary="review python code",estimated_tokens=30,trust=1),
            ContextCandidate("skill","travel",summary="plan a vacation",estimated_tokens=30,trust=1),
        ])
        self.assertIn("code-review",r["selected_ids"]);self.assertIn("travel",r["deferred_ids"])

    def test_lease_reuses_stable_working_set(self):
        c=self.controller("tools");fp=task_fingerprint("review github pr")
        items=[ContextCandidate("tool","github",summary="github pr review",estimated_tokens=10,trust=1),
               ContextCandidate("tool","mail",summary="email",estimated_tokens=10,trust=1)]
        a=c.plan(goal="review github pr",kind="tool",candidates=items,fingerprint=fp)
        b=c.plan(goal="review github pr",kind="tool",candidates=items,fingerprint=fp)
        self.assertEqual(a["selected_ids"],b["selected_ids"]);self.assertGreaterEqual(c.report()["metrics"]["lease_hits"],1)

    def test_shadow_mode_only_proposes_and_keeps_effective_context(self):
        c=ContextAdmissionController(SelectiveContextConfig(memory=SurfaceConfig("shadow",20,1,.1)))
        r=c.plan(goal="release",kind="memory",candidates=[
            ContextCandidate("memory","release",summary="release",estimated_tokens=100,trust=1),
            ContextCandidate("memory","other",summary="unrelated vehicle",estimated_tokens=100,trust=1),
        ])
        self.assertEqual(set(r["effective_selected_ids"]), {"release","other"})
        self.assertEqual(r["effective_tokens"], r["baseline_tokens"])
        self.assertEqual(r["applied_tokens_saved"], 0)
        self.assertGreater(r["gross_tokens_saved"], 0)

    def test_invalid_candidate_fails_open(self):
        c=self.controller("memory")
        r=c.plan(goal="x",kind="memory",candidates=[{"candidate_id":"x","kind":"bogus","estimated_tokens":50}])
        self.assertTrue(r["fail_open"]);self.assertEqual(r["selected_ids"],["x"]);self.assertEqual(r["net_tokens_saved"],0)

    def test_semantic_failure_falls_back_to_deterministic(self):
        cfg=SelectiveContextConfig(semantic_enabled=True,memory=SurfaceConfig("apply",20,2,.1));c=ContextAdmissionController(cfg)
        def explode(*args): raise RuntimeError("no provider")
        r=c.plan(goal="release",kind="memory",candidates=[ContextCandidate("memory","r",summary="release",estimated_tokens=10,trust=1)],semantic_decider=explode)
        self.assertIn("r",r["selected_ids"]);self.assertEqual(c.report()["metrics"]["semantic_errors"],1)

    def test_experience_adapter_preserves_verification_trust(self):
        x=memory_candidate_from_experience({"score":.8,"claim":{"claim_id":"c1","subject":"release","statement":"tests passed","claim_type":"verified","evidence_ids":["e1"]},"status":"active"})
        self.assertEqual(x.kind,"memory");self.assertEqual(x.trust,1.0);self.assertGreater(x.estimated_tokens,0)

    def test_tool_and_skill_token_estimates_are_nonzero(self):
        self.assertGreater(tool_candidate({"function":{"name":"x","description":"do x","parameters":{"type":"object"}}}).estimated_tokens,0)
        self.assertGreater(skill_candidate({"name":"review","description":"review code"}).estimated_tokens,0)

    def test_task_fingerprint_is_stable_to_word_order(self):
        self.assertEqual(task_fingerprint("review nerve PR").digest,task_fingerprint("nerve PR review").digest)
        self.assertNotEqual(task_fingerprint("review nerve PR").digest,task_fingerprint("check calendar").digest)

    def test_report_accounts_for_gross_savings(self):
        c=self.controller("memory")
        c.plan(goal="alpha",kind="memory",candidates=[ContextCandidate("memory","a",summary="alpha",estimated_tokens=100,trust=1),ContextCandidate("memory","b",summary="zebra",estimated_tokens=100,trust=1)])
        r=c.report();self.assertEqual(r["schema"],"hermes-nerve-selective-context-stats/v1")
        self.assertGreater(r["metrics"]["gross_tokens_saved"],0);self.assertEqual(r["semantic_provider_calls"],0)


# ============================================================
# RIGOROUS REGRESSION TESTS
# ============================================================

class SelectiveContextRegressionTests(unittest.TestCase):
    """Rigorous regression tests for lease validity and duplicate accounting."""

    def _apply_controller(self, kind="tools"):
        """Create a controller in APPLY mode for deterministic testing."""
        cfg = SelectiveContextConfig()
        setattr(cfg, kind, SurfaceConfig(mode="apply", max_candidates=20, max_admitted=2, min_score=.12))
        return ContextAdmissionController(cfg)

    # --------------------------------------------------------
    # A. New mandatory candidate under valid lease
    # --------------------------------------------------------
    def test_new_mandatory_candidate_joins_valid_lease(self):
        """Plan 1: A selected. Plan 2: A unchanged + B mandatory=True.
        B must be effective-selected with PIN/MANDATORY. A can remain pinned."""
        c = self._apply_controller("tools")
        fp = task_fingerprint("review github pr")

        # Plan 1: A selected - relevant to "review github pr"
        items1 = [ContextCandidate("tool", "A", "test", "github_pr", "review github pull request", 50, mandatory=False, trust=1.0)]
        r1 = c.plan(goal="review github pr", candidates=items1, kind="tool", fingerprint=fp)
        self.assertIn("A", r1["selected_ids"], "Plan #1: A should be selected")
        self.assertIn("A", r1["effective_selected_ids"], "Plan #1: A should be effective-selected")

        # Plan 2: A unchanged + B mandatory=True
        items2 = [
            ContextCandidate("tool", "A", "test", "github_pr", "review github pull request", 50, mandatory=False, trust=1.0),
            ContextCandidate("tool", "B", "test", "mandatory_tool", "mandatory tool for the task", 50, mandatory=True, trust=1.0),
        ]
        r2 = c.plan(goal="review github pr", candidates=items2, kind="tool", fingerprint=fp)

        # B MUST be effective-selected
        self.assertIn("B", r2["effective_selected_ids"],
                      "BUG: LEASE_NEW_MANDATORY - B with mandatory=True must be effective-selected")

        # B decision is PIN with reason MANDATORY
        b_decisions = [d for d in r2["decisions"] if d["candidate_id"] == "B"]
        self.assertEqual(len(b_decisions), 1, "B should have exactly one decision")
        self.assertEqual(b_decisions[0]["action"], AdmissionAction.PIN.value,
                         "B should be PINned")
        self.assertEqual(b_decisions[0]["reason_code"], "MANDATORY",
                         "B should have reason MANDATORY")

        # A can remain pinned (valid lease)
        self.assertIn("A", r2["effective_selected_ids"],
                      "A should remain effective-selected under valid lease")

        # Lease hit occurred
        self.assertGreaterEqual(c.report()["metrics"]["lease_hits"], 1,
                                "Lease should have been hit")

    # --------------------------------------------------------
    # B. Superseded leased candidate
    # --------------------------------------------------------
    def test_superseded_leased_candidate_cannot_survive(self):
        """Plan 1: A selected/leased. Plan 2: A has status=superseded.
        A MUST NOT be effective-selected. A gets STALE semantics, not SESSION_PIN.
        B can backfill the vacated capacity."""
        c = self._apply_controller("tools")
        fp = task_fingerprint("test stale candidate")

        # Plan 1: A selected and leased
        items1 = [ContextCandidate("tool", "A", "test", "candidate A", "description A", 50, trust=1.0, mandatory=False)]
        r1 = c.plan(goal="test stale candidate", candidates=items1, kind="tool", fingerprint=fp)
        self.assertIn("A", r1["selected_ids"], "Plan #1: A should be initially selected")
        self.assertIn("A", r1["effective_selected_ids"], "Plan #1: A should be effective-selected")

        # Plan 2: A superseded + B is a strong current match
        items2 = [
            ContextCandidate("tool", "A", "test", "candidate A", "description A", 50, trust=1.0, mandatory=False,
                            metadata={"status": "superseded"}),
            ContextCandidate("tool", "B", "test", "candidate B", "description B", 50, trust=1.0, mandatory=False),
        ]
        r2 = c.plan(goal="test stale candidate", candidates=items2, kind="tool", fingerprint=fp)

        # A MUST NOT be effective-selected
        self.assertNotIn("A", r2["effective_selected_ids"],
                         "BUG: LEASE_STALE_REVOCATION - A with status=superseded must NOT be effective-selected")

        # A gets STALE semantics, not SESSION_PIN
        a_decisions = [d for d in r2["decisions"] if d["candidate_id"] == "A"]
        self.assertEqual(len(a_decisions), 1, "A should have exactly one decision")
        self.assertEqual(a_decisions[0]["action"], AdmissionAction.SUPPRESS.value,
                         "A should be SUPPRESSED when superseded")
        self.assertEqual(a_decisions[0]["reason_code"], "STALE",
                         "A should have reason STALE, not SESSION_PIN")

        # B can backfill
        self.assertIn("B", r2["effective_selected_ids"],
                      "B should backfill the vacated capacity after A is invalidated")

    # --------------------------------------------------------
    # C. Expired leased candidate
    # --------------------------------------------------------
    def test_expired_leased_candidate_cannot_survive(self):
        """Plan 1: A selected/leased. Plan 2: A has expired=True.
        A MUST NOT be effective-selected via SESSION_PIN."""
        c = self._apply_controller("tools")
        fp = task_fingerprint("test expired candidate")

        # Plan 1: A selected and leased
        items1 = [ContextCandidate("tool", "A", "test", "candidate A", "description A", 50, trust=1.0, mandatory=False)]
        r1 = c.plan(goal="test expired candidate", candidates=items1, kind="tool", fingerprint=fp)
        self.assertIn("A", r1["selected_ids"], "Plan #1: A should be initially selected")

        # Plan 2: A expired
        items2 = [
            ContextCandidate("tool", "A", "test", "candidate A", "description A", 50, trust=1.0, mandatory=False,
                            metadata={"expired": True}),
            ContextCandidate("tool", "B", "test", "candidate B", "description B", 50, trust=1.0, mandatory=False),
        ]
        r2 = c.plan(goal="test expired candidate", candidates=items2, kind="tool", fingerprint=fp)

        # A MUST NOT be effective-selected
        self.assertNotIn("A", r2["effective_selected_ids"],
                         "BUG: LEASE_STALE_REVOCATION - A with expired=True must NOT be effective-selected")

        # A gets STALE semantics
        a_decisions = [d for d in r2["decisions"] if d["candidate_id"] == "A"]
        self.assertEqual(a_decisions[0]["action"], AdmissionAction.SUPPRESS.value,
                         "A should be SUPPRESSED when expired")
        self.assertEqual(a_decisions[0]["reason_code"], "STALE",
                         "A should have reason STALE")

    # --------------------------------------------------------
    # D. Missing leased candidate
    # --------------------------------------------------------
    def test_missing_leased_candidate_invalidates_lease(self):
        """Plan 1: A selected/leased. Plan 2: A disappears, B is strong match.
        Stale lease is invalidated. B is selected via normal ranking."""
        c = self._apply_controller("tools")
        fp = task_fingerprint("test missing candidate")

        # Plan 1: A selected and leased
        items1 = [ContextCandidate("tool", "A", "test", "candidate A", "description A", 50, trust=1.0, mandatory=False)]
        r1 = c.plan(goal="test missing candidate", candidates=items1, kind="tool", fingerprint=fp)
        self.assertIn("A", r1["selected_ids"], "Plan #1: A should be initially selected")

        # Plan 2: A disappears, B is a strong current match
        items2 = [
            ContextCandidate("tool", "B", "test", "candidate B", "description B", 50, trust=1.0, mandatory=False),
        ]
        r2 = c.plan(goal="test missing candidate", candidates=items2, kind="tool", fingerprint=fp)

        # B is selected via normal ranking
        self.assertIn("B", r2["selected_ids"],
                      "B should be selected via normal ranking after lease invalidation")
        self.assertIn("B", r2["effective_selected_ids"],
                      "B should be effective-selected")

        # Lease was invalidated (not reused)
        self.assertEqual(c.report()["metrics"]["lease_invalidations"], 1,
                         "Lease should be invalidated when a leased candidate disappears")

    # --------------------------------------------------------
    # E. New mandatory conflict
    # --------------------------------------------------------
    def test_new_mandatory_conflict_preserved(self):
        """Plan 1: A selected. Plan 2: A unchanged + B mandatory=True + B status=superseded.
        B remains PINned with reason MANDATORY_CONFLICT."""
        c = self._apply_controller("tools")
        fp = task_fingerprint("review github pr")

        # Plan 1: A selected - relevant to goal
        items1 = [ContextCandidate("tool", "A", "test", "github_pr", "review github pull request", 50, mandatory=False, trust=1.0)]
        r1 = c.plan(goal="review github pr", candidates=items1, kind="tool", fingerprint=fp)
        self.assertIn("A", r1["selected_ids"], "Plan #1: A should be selected")

        # Plan 2: B mandatory=True but also status=superseded
        items2 = [
            ContextCandidate("tool", "A", "test", "github_pr", "review github pull request", 50, mandatory=False, trust=1.0),
            ContextCandidate("tool", "B", "test", "mandatory_tool", "mandatory tool for the task", 50, mandatory=True, trust=1.0,
                            metadata={"status": "superseded"}),
        ]
        r2 = c.plan(goal="review github pr", candidates=items2, kind="tool", fingerprint=fp)

        # B remains PINned
        self.assertIn("B", r2["effective_selected_ids"],
                      "B with mandatory=True should remain effective-selected even with conflict")

        # B decision is PIN with reason MANDATORY_CONFLICT
        b_decisions = [d for d in r2["decisions"] if d["candidate_id"] == "B"]
        self.assertEqual(len(b_decisions), 1, "B should have exactly one decision")
        self.assertEqual(b_decisions[0]["action"], AdmissionAction.PIN.value,
                         "B should be PINned")
        self.assertEqual(b_decisions[0]["reason_code"], "MANDATORY_CONFLICT",
                         "B should have reason MANDATORY_CONFLICT when mandatory + superseded")

    # --------------------------------------------------------
    # F. Candidate overflow
    # --------------------------------------------------------
    def test_bounded_ranking_overflow_no_loss(self):
        """140 tools with max_candidates=128. Every unique input ID has a disposition."""
        c = ContextAdmissionController(SelectiveContextConfig(tools=SurfaceConfig("shadow", 128, 16, .18)))

        items = []
        for i in range(140):
            items.append(ContextCandidate("tool", f"tool_{i}", "test",
                                         f"Tool {i} description", f"Description of tool {i}", 1, trust=1.0))

        r = c.plan(goal="test bounded ranking with 140 tools", candidates=items, kind="tool")

        all_input_ids = set(c.candidate_id for c in items)
        disposition_ids = set(r["selected_ids"] + r["deferred_ids"] + r["suppressed_ids"])

        missing = all_input_ids - disposition_ids
        self.assertEqual(len(missing), 0,
                         f"BUG: CANDIDATE_OVERFLOW_LOSS - {len(missing)} IDs have no disposition: {sorted(missing)[:10]}")

    # --------------------------------------------------------
    # G. Duplicate-accounting metamorphic test
    # --------------------------------------------------------
    def test_duplicate_accounting_metamorphic_apply(self):
        """Controller 1: one candidate A (tokens=100). Controller 2: same A twice.
        Both must produce identical accounting. Duplicate must not inflate metrics."""
        # Controller 1: unique candidate
        c1 = ContextAdmissionController(SelectiveContextConfig(tools=SurfaceConfig("apply", 128, 16, .18)))
        items1 = [ContextCandidate("tool", "A", "test", "candidate A", "description A", 100, trust=1.0)]
        r1 = c1.plan(goal="test duplicate accounting", candidates=items1, kind="tool")

        # Controller 2: duplicate candidate
        c2 = ContextAdmissionController(SelectiveContextConfig(tools=SurfaceConfig("apply", 128, 16, .18)))
        items2 = [
            ContextCandidate("tool", "A", "test", "candidate A", "description A", 100, trust=1.0),
            ContextCandidate("tool", "A", "test", "candidate A", "description A", 100, trust=1.0),
        ]
        r2 = c2.plan(goal="test duplicate accounting", candidates=items2, kind="tool")

        # Assert equality for all accounting fields
        self.assertEqual(r1["baseline_tokens"], r2["baseline_tokens"],
                         f"baseline_tokens mismatch: unique={r1['baseline_tokens']}, duplicate={r2['baseline_tokens']}")
        self.assertEqual(r1["selected_tokens"], r2["selected_tokens"],
                         f"selected_tokens mismatch: unique={r1['selected_tokens']}, duplicate={r2['selected_tokens']}")
        self.assertEqual(r1["effective_tokens"], r2["effective_tokens"],
                         f"effective_tokens mismatch: unique={r1['effective_tokens']}, duplicate={r2['effective_tokens']}")
        self.assertEqual(r1["gross_tokens_saved"], r2["gross_tokens_saved"],
                         f"gross_tokens_saved mismatch: unique={r1['gross_tokens_saved']}, duplicate={r2['gross_tokens_saved']}")
        self.assertEqual(r1["applied_tokens_saved"], r2["applied_tokens_saved"],
                         f"applied_tokens_saved mismatch: unique={r1['applied_tokens_saved']}, duplicate={r2['applied_tokens_saved']}")
        self.assertEqual(r1["net_tokens_saved"], r2["net_tokens_saved"],
                         f"net_tokens_saved mismatch: unique={r1['net_tokens_saved']}, duplicate={r2['net_tokens_saved']}")

        # Assert ID sets are equal
        self.assertEqual(set(r1["selected_ids"]), set(r2["selected_ids"]),
                         f"selected_ids mismatch: unique={r1['selected_ids']}, duplicate={r2['selected_ids']}")
        self.assertEqual(set(r1["deferred_ids"]), set(r2["deferred_ids"]),
                         f"deferred_ids mismatch: unique={r1['deferred_ids']}, duplicate={r2['deferred_ids']}")
        self.assertEqual(set(r1["suppressed_ids"]), set(r2["suppressed_ids"]),
                         f"suppressed_ids mismatch: unique={r1['suppressed_ids']}, duplicate={r2['suppressed_ids']}")

        # Duplicate result contains only one disposition for candidate ID A
        a_dispositions = [d for d in r2["decisions"] if d["candidate_id"] == "A"]
        self.assertEqual(len(a_dispositions), 1,
                         f"Duplicate ID A should have exactly 1 disposition, got {len(a_dispositions)}")

    def test_duplicate_accounting_metamorphic_shadow(self):
        """SHADOW mode: duplicate input must not change baseline/effective accounting."""
        # Controller 1: unique candidate
        c1 = ContextAdmissionController(SelectiveContextConfig(tools=SurfaceConfig("shadow", 128, 16, .18)))
        items1 = [ContextCandidate("tool", "A", "test", "candidate A", "description A", 100, trust=1.0)]
        r1 = c1.plan(goal="test duplicate accounting shadow", candidates=items1, kind="tool")

        # Controller 2: duplicate candidate
        c2 = ContextAdmissionController(SelectiveContextConfig(tools=SurfaceConfig("shadow", 128, 16, .18)))
        items2 = [
            ContextCandidate("tool", "A", "test", "candidate A", "description A", 100, trust=1.0),
            ContextCandidate("tool", "A", "test", "candidate A", "description A", 100, trust=1.0),
        ]
        r2 = c2.plan(goal="test duplicate accounting shadow", candidates=items2, kind="tool")

        # In shadow mode, effective_tokens == baseline_tokens
        self.assertEqual(r1["baseline_tokens"], r2["baseline_tokens"],
                         f"SHADOW baseline_tokens mismatch: unique={r1['baseline_tokens']}, duplicate={r2['baseline_tokens']}")
        self.assertEqual(r1["effective_tokens"], r2["effective_tokens"],
                         f"SHADOW effective_tokens mismatch: unique={r1['effective_tokens']}, duplicate={r2['effective_tokens']}")
        self.assertEqual(r1["gross_tokens_saved"], r2["gross_tokens_saved"],
                         f"SHADOW gross_tokens_saved mismatch: unique={r1['gross_tokens_saved']}, duplicate={r2['gross_tokens_saved']}")


if __name__=="__main__": unittest.main()
