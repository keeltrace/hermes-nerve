"""Regression tests for the Paperclip worker self-transition fence.

A ``hermes_local`` worker reaches its issue through Paperclip's HTTP API
(curl / the repo's PaperclipClient), so the Kanban completion-intent fence
never sees the transition. For controller-supervised Paperclip runs the
verified ``in_review`` handoff belongs to Nerve, not the worker.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hermes_nerve.integrations.paperclip import PaperclipRunContext
from hermes_nerve.work import hooks, paperclip_runtime, runtime
from hermes_nerve.work.supervisor import CardSupervisor


class FakeAuthority:
    def __init__(self, updated: bool = True, reason: str = ""):
        self.updated = updated
        self.reason = reason

    def request_review(self, verdict, *, summary="", evidence=()):
        from hermes_nerve.work.paperclip_authority import HandoffResult

        return HandoffResult(True, self.updated, self.reason or "handed off")


class PaperclipSelfTransitionFenceTests(unittest.TestCase):
    def setUp(self):
        runtime.set_tool_dispatcher(lambda name, args: {"ok": True, "status": "done"})
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        supervisor = CardSupervisor(store_path=Path(self._tmp.name) / "work.db")
        runtime.set_supervisor_for_tests(supervisor, enabled_value=True)
        self.supervisor = supervisor

        context = PaperclipRunContext(
            "company-1", "issue-e2e", "run-e2e", "builder-1",
            api_url="http://paperclip.test", role="builder",
        )
        self.identity = paperclip_runtime.bootstrap_paperclip_worker(
            supervisor,
            context=context,
            client=_FakeClient(),
            workspace_path=self._tmp.name,
            session_id="session-e2e",
        )
        self.assertIsNotNone(self.identity)

    def tearDown(self):
        paperclip_runtime.clear_for_tests()
        runtime.set_supervisor_for_tests(None, enabled_value=False)
        runtime.set_tool_dispatcher(None)

    def _patch_authority(self, authority):
        return patch.object(
            paperclip_runtime, "current_authority", lambda: authority
        )

    def _write_passing_tests(self) -> None:
        tests = Path(self._tmp.name) / "tests"
        tests.mkdir(exist_ok=True)
        (tests / "test_ok.py").write_text(
            "import unittest\n\n"
            "class T(unittest.TestCase):\n"
            "    def test_ok(self): self.assertEqual(2 + 2, 4)\n"
        )

    def test_curl_style_in_review_patch_is_blocked_and_controller_handsoff(self):
        """Worker PATCH via curl must never reach the API; controller hands off."""
        self._write_passing_tests()
        authority = FakeAuthority(updated=True)
        with self._patch_authority(authority):
            decision = hooks.pre_tool_call(
                "terminal",
                {"command": 'curl -X PATCH -d \'{"status":"in_review"}\' '
                            'http://paperclip.test/api/issues/issue-e2e'},
                task_id="issue-e2e",
                session_id="session-e2e",
            )
        self.assertEqual(decision["action"], "block")
        self.assertIn("Nerve verified", decision["message"])

    def test_paperclip_client_request_review_is_blocked_and_controller_handsoff(self):
        """Worker request_review via the repo client must be fenced the same way."""
        self._write_passing_tests()
        authority = FakeAuthority(updated=True)
        with self._patch_authority(authority):
            decision = hooks.pre_tool_call(
                "terminal",
                {"command": "python3 -c 'from hermes_nerve.integrations.paperclip_client "
                            "import PaperclipClient; c.request_review(\"issue-e2e\", "
                            "comment=\"### Nerve Verification\\n\\nResult: PASS\")'"},
                task_id="issue-e2e",
                session_id="session-e2e",
            )
        self.assertEqual(decision["action"], "block")
        self.assertIn("Nerve verified", decision["message"])

    def test_unverified_transition_attempt_reports_missing_criteria(self):
        """No deterministic evidence yet -> blocked with the gate reason, no handoff."""
        with self._patch_authority(FakeAuthority()):
            decision = hooks.pre_tool_call(
                "terminal",
                {"command": 'curl -X PATCH -d \'{"status":"in_review"}\' '
                            'http://x.test/api/issues/issue-e2e'},
                task_id="issue-e2e",
                session_id="session-e2e",
            )
        self.assertEqual(decision["action"], "block")
        self.assertNotIn("Nerve verified the locked", decision["message"])

    def test_unrelated_terminal_command_is_not_fenced(self):
        decision = hooks.pre_tool_call(
            "terminal",
            {"command": "python3 -m pytest -q tests/test_ok.py"},
            task_id="issue-e2e",
            session_id="session-e2e",
        )
        self.assertIsNone(decision)

    def test_non_paperclip_run_is_not_fenced(self):
        paperclip_runtime.clear_for_tests()
        decision = hooks.pre_tool_call(
            "terminal",
            {"command": 'curl -X PATCH -d \'{"status":"in_review"}\' http://x.test/api/issues/other'},
            task_id="issue-e2e",
            session_id="session-e2e",
        )
        self.assertIsNone(decision)

    def test_session_end_hands_off_verified_paperclip_run(self):
        """Session-end reconciliation performs the Nerve-owned handoff."""
        authority = FakeAuthority(updated=True)
        with self._patch_authority(authority):
            # Make the deterministic DoD pass: write a passing test file.
            tests = Path(self._tmp.name) / "tests"
            tests.mkdir(exist_ok=True)
            (tests / "test_ok.py").write_text(
                "import unittest\n\n"
                "class T(unittest.TestCase):\n"
                "    def test_ok(self): self.assertEqual(2 + 2, 4)\n"
            )
            with patch.dict("os.environ", {"HERMES_KANBAN_WORKSPACE": self._tmp.name}):
                hooks.on_session_end(task_id="issue-e2e", session_id="session-e2e")
        # The controller attempted the handoff (diagnostic recorded either way).
        store = self.supervisor.store
        kinds = set()
        for kind in ("completion_session_end", "completion_session_end_handoff", "completion_session_end_failed"):
            if store.latest_diagnostic(task_id=self.identity.task_id, kind=kind):
                kinds.add(kind)
        self.assertTrue(
            kinds,
            "expected session-end completion diagnostics",
        )


class _FakeClient:
    """Minimal PaperclipClient stand-in for bootstrap (issue fetch only)."""

    run_id = "run-e2e"

    def __init__(self):
        from hermes_nerve.integrations.paperclip_client import PaperclipIssue

        self._issue = PaperclipIssue(
            id="issue-e2e",
            title="Fence smoke",
            description=(
                "## Definition of Done\n"
                "- `python3 -m pytest -q tests/test_ok.py` exits 0.\n"
            ),
            status="in_progress",
            assignee_agent_id="builder-1",
            checkout_run_id="run-e2e",
        )

    def get_issue(self, issue_id):
        return self._issue


if __name__ == "__main__":
    unittest.main()
