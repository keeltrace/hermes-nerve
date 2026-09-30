import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hermes_nerve import nervous
from hermes_nerve.progress_integrity import (
    change_required,
    mutation_is_relevant,
    requested_paths,
    test_succeeded,
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

    def test_common_change_verbs_activate_progress_guard(self):
        for verb in ("Add", "Create", "Remove", "Delete", "Rename", "Change", "Replace"):
            with self.subTest(verb=verb):
                self.assertTrue(change_required(f"{verb} src/parser.py"))

    def test_terminal_mutation_paths_can_be_task_relevant(self):
        targets = requested_paths("Fix src/parser.py")
        cases = [
            ("sed -i 's/old/new/' src/parser.py", True),
            ("printf 'x' > src/parser.py", True),
            ("rm src/parser.py", True),
            ("cp src/other.py src/parser.py", True),
            ("sed -i 's/old/new/' scratch.py", False),
        ]
        for command, expected in cases:
            with self.subTest(command=command):
                self.assertEqual(mutation_is_relevant("terminal", {"command": command}, targets), expected)

    def test_successful_zero_failure_summary_is_not_rejected(self):
        self.assertTrue(test_succeeded("ok", "12 passed, 0 failed, 0 errors", "", 0))
        self.assertFalse(test_succeeded("ok", "11 passed, 1 failed", "", 1))

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
            # Challenge first, then allow a bounded recovery window before hard blocking.
            for index in range(3):
                self.assertIsNone(system.before_tool_call(
                    tool_name="read_file", args={"path": f"grace-{index}.py"},
                    session_id="s1", turn_id="t1", tool_call_id=f"grace-pre-{index}",
                ))
                system.observe_tool_call(
                    tool_name="read_file", args={"path": f"grace-{index}.py"},
                    status="ok", result="content", error_message="",
                    tool_call_id=f"grace-{index}", session_id="s1", turn_id="t1",
                )
            blocked = system.before_tool_call(
                tool_name="read_file", args={"path": "another.py"},
                session_id="s1", turn_id="t1", tool_call_id="read-after-grace",
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


if __name__ == "__main__":
    unittest.main()