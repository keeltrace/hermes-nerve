"""KEE-804 portable regression: replay a Paperclip harness wake and a genuine code task.

Adapted from earlier PR #59, retaining the behavioral test and attribution
without any private machine paths, agent IDs, credentials, or environment files.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hermes_nerve import nervous
from hermes_nerve.progress_integrity import (
    change_required,
    harness_instructions_message,
    is_harness_path,
    requested_paths,
    verification_required,
)
from tests.test_nervous import ScriptedEngine

HARNESS_PROMPT = """# Agent Instructions
Paperclip wake payload
PAPERCLIP_TASK_ID=example-nonsecret-id
Execution contract: review the existing evidence and update issue status.
Harness helper: skills/paperclip/scripts/paperclip-upload-artifact.sh
This instruction document is provided by the agent harness, not an owner coding request.
"""
DELIVERABLE = "docs/qa/harness-stall-triage.md"


class HeartbeatReplayTests(unittest.TestCase):
    def _system(self) -> nervous.NervousSystem:
        ScriptedEngine.reset()
        system = nervous.NervousSystem(engine_factory=ScriptedEngine)
        system.configure(enabled=True, admission_enabled=False, mode="correct_next")
        return system

    def test_harness_heartbeat_does_not_block_a_real_deliverable(self):
        self.assertTrue(harness_instructions_message(HARNESS_PROMPT))
        self.assertFalse(change_required(HARNESS_PROMPT))
        self.assertFalse(verification_required(HARNESS_PROMPT))
        self.assertNotIn("skills/paperclip/scripts/paperclip-upload-artifact.sh",
                         requested_paths(HARNESS_PROMPT))
        self.assertTrue(is_harness_path("skills/paperclip/scripts/paperclip-upload-artifact.sh"))

        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, {
            "HERMES_NERVE_NERVOUS_EVENTS": str(Path(td) / "nervous.jsonl"),
            "HERMES_NERVE_OUTCOMES": str(Path(td) / "outcomes.jsonl"),
        }):
            system = self._system()
            system.start_turn(user_message=HARNESS_PROMPT, session_id="s1", turn_id="t1")
            for index in range(12):
                system.observe_tool_call(
                    tool_name="read_file", args={"path": f"notes-{index}.md"},
                    status="ok", result="content", error_message="",
                    tool_call_id=f"r{index}", session_id="s1", turn_id="t1",
                )
            blocked_read = system.before_tool_call(
                tool_name="read_file", args={"path": DELIVERABLE},
                session_id="s1", turn_id="t1", tool_call_id="read-after-stall",
            )
            system.observe_tool_call(
                tool_name="write_file", args={"path": DELIVERABLE, "content": "verdict"},
                status="ok", result="written", error_message="",
                tool_call_id="deliverable", session_id="s1", turn_id="t1",
            )
            gate = system.completion_gate(
                turn_id="t1", session_id="s1", final_response="Verdict recorded.",
                coding=True, changed_paths=[DELIVERABLE],
            )
            state = system.status(turn_id="t1")
            events_path = Path(td) / "nervous.jsonl"
            events = [json.loads(line) for line in events_path.read_text().splitlines()] if events_path.exists() else []
            self.assertFalse(state["progress_challenge_issued"])
            self.assertIsNone(blocked_read)
            self.assertEqual(state["mutation_attempts"], 1)
            self.assertEqual(state["relevant_mutation_attempts"], 1)
            self.assertIsNone(gate)
            self.assertFalse([e for e in events if e.get("kind") == "progress_completion_blocked"])

    def test_real_coding_task_remains_guarded(self):
        with tempfile.TemporaryDirectory() as td:
            system = self._system()
            system.start_turn(
                user_message="Fix src/parser.py and run regression tests",
                session_id="s2", turn_id="t2",
            )
            state = system.status(turn_id="t2")
            self.assertTrue(state["change_required"])
            self.assertEqual(state["requested_paths"], ["src/parser.py"])
            system.observe_tool_call(
                tool_name="write_file", args={"path": "repro.py", "content": "x"},
                status="ok", result="written", error_message="",
                tool_call_id="scratch", session_id="s2", turn_id="t2",
            )
            gate = system.completion_gate(
                turn_id="t2", session_id="s2", final_response="Done",
                coding=True, changed_paths=["repro.py"],
            )
            self.assertIsNotNone(gate)
            self.assertIn("task-relevant", gate["message"])


if __name__ == "__main__":
    unittest.main()
