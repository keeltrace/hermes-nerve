"""KEE-804 regression: replay the real Paperclip heartbeat prompt end to end.

This is the reproduction that the guard fixes. It drives a live NervousSystem
the way the agent harness does -- start_turn with the agent instructions file as
the prompt, then a reviewer-style investigation, then a real board-bound
mutation, then completion_gate -- and asserts the turn is not blocked by an
unsatisfiable change obligation.

The prompt is synthesized from the harness instruction file when available and
skipped (rather than silently passing) when it is not, so this test never
degrades into a vacuous assertion on a developer machine without Paperclip.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from hermes_nerve import nervous
from hermes_nerve.progress_integrity import (
    change_required,
    harness_instructions_message,
    is_harness_path,
    requested_paths,
    verification_required,
)
from tests.test_nervous import ScriptedEngine

HARNESS_INSTRUCTIONS = Path(
    "/home/j/.paperclip/instances/default/companies/c42a2344-23e3-4a9a-8cef-57925705e80c"
    "/agents/fb0b2bf2-eae1-4513-a4bc-0263dc0c3ed9/instructions/AGENTS.md"
)
DELIVERABLE = "docs/qa/2026-10-05-arb-stall-triage.md"


class HeartbeatReplayTests(unittest.TestCase):
    """The guard must not fire on a harness heartbeat that made an honest change."""

    def _replay(self, prompt: str, td: str) -> dict:
        os.environ["HERMES_NERVE_NERVOUS_EVENTS"] = str(Path(td) / "nervous.jsonl")
        os.environ["HERMES_NERVE_OUTCOMES"] = str(Path(td) / "outcomes.jsonl")
        system = nervous.NervousSystem(engine_factory=ScriptedEngine)
        system.configure(enabled=True, admission_enabled=False, mode="correct_next")
        system.start_turn(user_message=prompt, session_id="s1", turn_id="t1")

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
            turn_id="t1", session_id="s1", final_response="Verdict recorded on the board.",
            coding=True, changed_paths=[DELIVERABLE],
        )
        state = system.status(turn_id="t1")
        events = [json.loads(line) for line in
                  (Path(td) / "nervous.jsonl").read_text().splitlines()]
        return {
            "gate": gate,
            "blocked_read": blocked_read,
            "state": state,
            "blocked_events": [e for e in events
                               if e.get("kind") == "progress_completion_blocked"],
        }

    def test_real_paperclip_instructions_file_does_not_block_a_real_deliverable(self):
        if not HARNESS_INSTRUCTIONS.is_file():
            self.skipTest(f"Paperclip agent instructions not present at {HARNESS_INSTRUCTIONS}")
        prompt = HARNESS_INSTRUCTIONS.read_text()

        # Precondition: this really is the misfire shape from the ticket.
        self.assertTrue(harness_instructions_message(prompt))
        self.assertFalse(change_required(prompt))
        self.assertFalse(verification_required(prompt))
        self.assertNotIn(
            "skills/paperclip/scripts/paperclip-upload-artifact.sh", requested_paths(prompt))
        self.assertTrue(is_harness_path("skills/paperclip/scripts/paperclip-upload-artifact.sh"))

        with tempfile.TemporaryDirectory() as td:
            result = self._replay(prompt, td)

        self.assertFalse(
            result["state"]["progress_challenge_issued"],
            "a reviewer turn with no code mutation must not arm the progress lease",
        )
        self.assertIsNone(
            result["blocked_read"],
            "investigation reads must stay available on a harness heartbeat",
        )
        self.assertEqual(result["state"]["mutation_attempts"], 1)
        self.assertEqual(
            result["state"]["relevant_mutation_attempts"], 1,
            "a real committed board deliverable must score as relevant",
        )
        self.assertIsNone(
            result["gate"],
            f"guard blocked an honest deliverable: {result['gate']}",
        )
        self.assertEqual(result["blocked_events"], [])

    def test_real_coding_task_is_still_guarded_end_to_end(self):
        """Counter-test: the fix must not disarm the guard for genuine work."""
        with tempfile.TemporaryDirectory() as td:
            system = nervous.NervousSystem(engine_factory=ScriptedEngine)
            system.configure(enabled=True, admission_enabled=False, mode="correct_next")
            system.start_turn(
                user_message=f"Fix src/parser.py and run the regression tests",
                session_id="s1", turn_id="t1",
            )
            state = system.status(turn_id="t1")
            self.assertTrue(state["change_required"])
            self.assertEqual(state["requested_paths"], ["src/parser.py"])

            # Scratch-only mutation still fails the gate.
            system.observe_tool_call(
                tool_name="write_file", args={"path": "repro.py", "content": "x"},
                status="ok", result="written", error_message="",
                tool_call_id="scratch", session_id="s1", turn_id="t1",
            )
            gate = system.completion_gate(
                turn_id="t1", session_id="s1", final_response="Done",
                coding=True, changed_paths=["repro.py"],
            )
            self.assertIsNotNone(gate, "scratch-only work must still be refused")
            self.assertIn("task-relevant", gate["message"])


if __name__ == "__main__":
    unittest.main()