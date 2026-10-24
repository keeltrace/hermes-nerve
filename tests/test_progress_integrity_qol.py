import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hermes_nerve import nervous
from hermes_nerve.progress_integrity import (
    mutation_is_relevant,
    requested_paths,
    tool_kind,
)
from tests.test_nervous import ScriptedEngine


class ProgressIntegrityQolTests(unittest.TestCase):
    def _system(self, td: str) -> nervous.NervousSystem:
        ScriptedEngine.reset()
        system = nervous.NervousSystem(engine_factory=ScriptedEngine)
        system.configure(enabled=True, admission_enabled=False, mode="correct_next")
        return system

    def test_native_patch_is_central_mutation_alias(self):
        self.assertEqual(tool_kind("patch", {"patch": "*** Begin Patch\n*** End Patch"}), "mutation")
        targets = requested_paths("Fix src/parser.py")
        self.assertTrue(
            mutation_is_relevant(
                "patch",
                {"patch": "--- a/src/parser.py\n+++ b/src/parser.py\n@@ -1 +1 @@\n-a\n+b"},
                targets,
            )
        )

    def test_urls_and_metrics_not_treated_as_requested_paths(self):
        msg = "Update monitor: buzz_platform 378.5 ms https://buzz.ptdev.vip/ and @url:https://honcho-api.ptdev.vip/health"
        targets = requested_paths(msg)
        self.assertEqual(targets, ())
        self.assertTrue(mutation_is_relevant("patch", {"path": "scripts/watchdog_htop.py"}, targets))

    def test_case_insensitive_url_and_marker_stripping(self):
        cases = [
            ("HTTPS://EXAMPLE.COM/foo.py", ()),
            ("Https://example.com/sub/bar.py", ()),
            ("@URL:https://example.com/api/test.py", ()),
            ("@url:HTTPS://EXAMPLE.COM/spec.py", ()),
            ("URL:HTTP://SERVER/index.html", ()),
            ("HTTPS://EXAMPLE.COM/foo.py then fix src/parser.py", ("src/parser.py",)),
            ("Check @URL:https://host/doc.md and update lib/utils.py and tests/test_run.py", ("lib/utils.py", "tests/test_run.py")),
            ("Mixed HttP://ExAmPlE.CoM/bar.py followed by a/b/c.py", ("a/b/c.py",)),
        ]
        for msg, expected in cases:
            with self.subTest(msg=msg):
                self.assertEqual(requested_paths(msg), expected)

    def test_scratch_mutation_does_not_clear_progress_stall(self):
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, {
            "HERMES_NERVE_NERVOUS_EVENTS": str(Path(td) / "nervous.jsonl"),
            "HERMES_NERVE_OUTCOMES": str(Path(td) / "outcomes.jsonl"),
        }, clear=False):
            system = self._system(td)
            system.start_turn(
                user_message="Fix src/parser.py and add regression tests",
                session_id="s1",
                turn_id="t1",
            )
            for index in range(12):
                system.observe_tool_call(
                    tool_name="read_file",
                    args={"path": f"source-{index}.py"},
                    status="ok",
                    result="content",
                    error_message="",
                    tool_call_id=f"r{index}",
                    session_id="s1",
                    turn_id="t1",
                )
            state = system.status(turn_id="t1")
            self.assertTrue(state["progress_challenge_issued"])
            self.assertEqual(state["relevant_mutation_attempts"], 0)

            # Scratch mutation is permitted, but must not consume the stall lease.
            self.assertIsNone(system.before_tool_call(
                tool_name="write_file",
                args={"path": "repro.py", "content": "print('repro')"},
                session_id="s1", turn_id="t1", tool_call_id="scratch-pre",
            ))
            system.observe_tool_call(
                tool_name="write_file",
                args={"path": "repro.py", "content": "print('repro')"},
                status="ok", result="written", error_message="",
                tool_call_id="scratch", session_id="s1", turn_id="t1",
            )
            state = system.status(turn_id="t1")
            self.assertEqual(state["mutation_attempts"], 1)
            self.assertEqual(state["relevant_mutation_attempts"], 0)
            self.assertIsNotNone(state["active_control"])
            blocked = system.before_tool_call(
                tool_name="read_file", args={"path": "another.py"},
                session_id="s1", turn_id="t1", tool_call_id="read-after-scratch",
            )
            self.assertEqual(blocked["action"], "block")

            # The requested file is a relevant mutation and clears the lease.
            self.assertIsNone(system.before_tool_call(
                tool_name="patch",
                args={"patch": "--- a/src/parser.py\n+++ b/src/parser.py\n@@ -1 +1 @@\n-a\n+b"},
                session_id="s1", turn_id="t1", tool_call_id="patch-pre",
            ))
            system.observe_tool_call(
                tool_name="patch",
                args={"patch": "--- a/src/parser.py\n+++ b/src/parser.py\n@@ -1 +1 @@\n-a\n+b"},
                status="ok", result="patched", error_message="",
                tool_call_id="patch", session_id="s1", turn_id="t1",
            )
            state = system.status(turn_id="t1")
            self.assertEqual(state["relevant_mutation_attempts"], 1)
            self.assertIsNone(state["active_control"])
            self.assertIsNone(system.before_tool_call(
                tool_name="read_file", args={"path": "src/parser.py"},
                session_id="s1", turn_id="t1", tool_call_id="read-after-fix",
            ))

    def test_completion_requires_relevant_change_and_requested_verification(self):
        with tempfile.TemporaryDirectory() as td:
            system = self._system(td)
            system.start_turn(
                user_message="Fix src/parser.py and run regression tests",
                session_id="s1",
                turn_id="t1",
            )
            system.observe_tool_call(
                tool_name="write_file", args={"path": "repro.py", "content": "x=1"},
                status="ok", result="written", error_message="",
                tool_call_id="scratch", session_id="s1", turn_id="t1",
            )
            blocked = system.completion_gate(
                turn_id="t1", session_id="s1", final_response="Done",
                coding=True, changed_paths=["repro.py"],
            )
            self.assertEqual(blocked["action"], "continue")
            self.assertIn("task-relevant", blocked["message"])

            system.observe_tool_call(
                tool_name="write_file", args={"path": "src/parser.py", "content": "x=2"},
                status="ok", result="written", error_message="",
                tool_call_id="fix", session_id="s1", turn_id="t1",
            )
            blocked = system.completion_gate(
                turn_id="t1", session_id="s1", final_response="Done",
                coding=True, changed_paths=["src/parser.py"],
            )
            self.assertEqual(blocked["action"], "continue")
            self.assertIn("verification", blocked["message"])

            system.observe_tool_call(
                tool_name="terminal",
                args={"command": "python3 -m unittest tests.test_parser"},
                status="ok", result="OK", error_message="",
                tool_call_id="test", session_id="s1", turn_id="t1",
            )
            self.assertEqual(system.status(turn_id="t1")["verified_test_passes"], 1)
            self.assertIsNone(system.completion_gate(
                turn_id="t1", session_id="s1", final_response="Done",
                coding=True, changed_paths=["src/parser.py"],
            ))


    def test_wrapped_verification_needs_successful_nonempty_test_execution(self):
        command = 'taskset -c 0 nice -n 15 python -B -m unittest tests.test_parser -v'
        cases = ((0, 'Ran 1 test in 0.01s\nOK', True),
                 (1, '1 passed in 0.01s', False),
                 (0, 'Ran 0 tests\nOK', False),
                 (0, '1 failed in 0.01s', False))
        for exit_code, output, verified in cases:
            with self.subTest(exit_code=exit_code, output=output), tempfile.TemporaryDirectory() as td:
                # events live outside the workspace td: a late async flush must never
                # race the workspace TemporaryDirectory cleanup (CI 3.12 Errno 39)
                with tempfile.TemporaryDirectory() as events_td, patch.dict(os.environ, {
                    'HERMES_NERVE_NERVOUS_EVENTS': str(Path(events_td) / 'nervous.jsonl'),
                    'HERMES_NERVE_OUTCOMES': str(Path(events_td) / 'outcomes.jsonl'),
                }):
                    system = self._system(td)
                    system.start_turn(user_message='Fix src/parser.py and run regression tests',
                                      session_id='wrapped', turn_id='wrapped')
                    system.observe_tool_call(
                        tool_name='write_file', args={'path': 'src/parser.py', 'content': 'x=2'},
                        status='ok', result='written', error_message='', tool_call_id='fix',
                        session_id='wrapped', turn_id='wrapped')
                    system.observe_tool_call(
                        tool_name='terminal', args={'command': command}, status='ok',
                        result=json.dumps({'output': output, 'exit_code': exit_code, 'error': None}),
                        error_message='', tool_call_id='test', session_id='wrapped', turn_id='wrapped')
                    self.assertEqual(system.status(turn_id='wrapped')['verified_test_passes'], int(verified))
                    decision = system.completion_gate(
                        turn_id='wrapped', session_id='wrapped', coding=True,
                        changed_paths=['src/parser.py'], final_response='Done')
                    if verified:
                        self.assertIsNone(decision)
                    else:
                        assert decision is not None
                        self.assertEqual(decision['action'], 'continue')
                        self.assertIn('verification', decision['message'])

    def test_environment_classifier_and_thrash_recovery(self):
        from hermes_nerve.progress_integrity import environment_action, environment_failure

        self.assertEqual(environment_action("terminal", {"command": "pip install -e ."}), "setup")
        self.assertEqual(environment_action("terminal", {"command": "python3 -m pytest -q"}), "test")
        self.assertEqual(environment_action("terminal", {"command": "git status"}), "")
        self.assertTrue(environment_failure(
            "terminal", {"command": "pytest -q"}, "error",
            "ModuleNotFoundError: No module named 'hermes_nerve'", "",
        ))
        self.assertFalse(environment_failure(
            "terminal", {"command": "pytest -q"}, "error",
            "FAILED tests/test_x.py::test_y", "AssertionError",
        ))

        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, {
            "HERMES_NERVE_NERVOUS_EVENTS": str(Path(td) / "nervous.jsonl"),
            "HERMES_NERVE_OUTCOMES": str(Path(td) / "outcomes.jsonl"),
        }, clear=False):
            system = self._system(td)
            system.start_turn(user_message="Repair the repository", session_id="s1", turn_id="t1")

            system.observe_tool_call(
                tool_name="terminal", args={"command": "pip install -e ."}, status="error",
                result="permission denied", error_message="", tool_call_id="env1",
                session_id="s1", turn_id="t1",
            )
            self.assertIsNone(system.before_tool_call(
                tool_name="terminal", args={"command": "uv pip install -e ."},
                session_id="s1", turn_id="t1", tool_call_id="env2-pre",
            ))
            system.observe_tool_call(
                tool_name="terminal", args={"command": "uv pip install -e ."}, status="error",
                result="uv: command not found", error_message="", tool_call_id="env2",
                session_id="s1", turn_id="t1",
            )
            state = system.status(turn_id="t1")
            self.assertEqual(state["active_control"]["source"], "local-environment-thrash")

            blocked = system.before_tool_call(
                tool_name="terminal", args={"command": "poetry install"},
                session_id="s1", turn_id="t1", tool_call_id="env3",
            )
            self.assertEqual(blocked["action"], "block")
            self.assertIn("native test configuration", blocked["message"])

            # A diagnostic is allowed but does not consume the lease until it succeeds.
            self.assertIsNone(system.before_tool_call(
                tool_name="read_file", args={"path": "pyproject.toml"},
                session_id="s1", turn_id="t1", tool_call_id="diag-pre",
            ))
            self.assertEqual(
                system.status(turn_id="t1")["active_control"]["source"],
                "local-environment-thrash",
            )
            system.observe_tool_call(
                tool_name="read_file", args={"path": "pyproject.toml"}, status="ok",
                result="[project]", error_message="", tool_call_id="diag",
                session_id="s1", turn_id="t1",
            )
            self.assertIsNone(system.status(turn_id="t1")["active_control"])
            self.assertEqual(system.quality_metrics()["environment_thrash_replans"], 1)
            self.assertEqual(system.quality_metrics()["environment_thrash_prevented_calls"], 1)


if __name__ == "__main__":
    unittest.main()
