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
    mutation_is_relevant,
    requested_paths,
    tool_kind,
    verification_required,
)
from tests.test_nervous import ScriptedEngine

# Verbatim shape of a Paperclip agent heartbeat: the agent instructions file is
# loaded as the prompt, with the issue body appended below it. KEE-804.
HARNESS_HEARTBEAT_PROMPT = """## Execution Contract

- Start actionable work in the same heartbeat. Do not stop at a plan unless the issue explicitly asks for planning.
- Leave durable progress in task comments, documents, or work products, then update the issue to a clear final disposition before you exit.
- When your work produces a user-inspectable deliverable file, follow the Paperclip skill's "Generated Artifacts and Work Products" workflow before final disposition. Use `skills/paperclip/scripts/paperclip-upload-artifact.sh` when working in this repo, create/update an artifact work product when the file is the deliverable, and link the uploaded attachment in the final comment.

## Paperclip Wake Payload

- reason: issue_assigned
- issue: KEE-804 Nerve progress guard misfires on taskless Paperclip heartbeats

Paperclip API guidance:
- Use `curl` from the terminal for Paperclip API calls; browser/web extraction tools may not reach localhost.
- Use `$PAPERCLIP_API_URL`, `$PAPERCLIP_API_KEY`, and `$PAPERCLIP_RUN_ID`; do not hard-code local ports.

Please review docs/qa/2026-10-05-arb-stall-triage.md and comment on the board.
"""


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

    def test_harness_instruction_prompt_derives_no_change_obligation(self):
        self.assertTrue(harness_instructions_message(HARNESS_HEARTBEAT_PROMPT))
        self.assertFalse(change_required(HARNESS_HEARTBEAT_PROMPT))
        self.assertFalse(verification_required(HARNESS_HEARTBEAT_PROMPT))

        # A real task statement keeps the guard armed.
        self.assertFalse(harness_instructions_message("Fix src/parser.py and run the tests"))
        self.assertTrue(change_required("Fix src/parser.py and run the tests"))
        self.assertTrue(verification_required("Fix src/parser.py and run the tests"))

    def test_harness_paths_are_never_requested_change_targets(self):
        self.assertEqual(requested_paths(HARNESS_HEARTBEAT_PROMPT),
                         ("docs/qa/2026-10-05-arb-stall-triage.md",))
        self.assertEqual(requested_paths(HARNESS_HEARTBEAT_PROMPT + "\nmetadata.resourceRef.kind"),
                         ("docs/qa/2026-10-05-arb-stall-triage.md",))
        self.assertTrue(is_harness_path("skills/paperclip/scripts/paperclip-upload-artifact.sh"))
        self.assertTrue(is_harness_path(
            "/home/j/.paperclip/cli/installs/npm/x/skills/paperclip/scripts/paperclip-upload-artifact.sh"))
        self.assertTrue(is_harness_path("node_modules/@paperclipai/server/dist/index.js"))
        self.assertTrue(is_harness_path(
            "agents/companies/c42a2344/agents/fb0b2bf2/instructions/AGENTS.md"))
        self.assertFalse(is_harness_path("docs/qa/2026-10-05-arb-stall-triage.md"))
        self.assertFalse(is_harness_path("hermes_nerve/progress_integrity.py"))
        # A user project may legitimately own these names; only the harness
        # segment/name combination is excluded.
        self.assertFalse(is_harness_path("paperclip/client.py"))
        self.assertFalse(is_harness_path("docs/instructions.md"))

    def test_harness_heartbeat_completes_without_unsatisfiable_block(self):
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, {
            "HERMES_NERVE_NERVOUS_EVENTS": str(Path(td) / "nervous.jsonl"),
            "HERMES_NERVE_OUTCOMES": str(Path(td) / "outcomes.jsonl"),
        }, clear=False):
            system = self._system(td)
            system.start_turn(
                user_message=HARNESS_HEARTBEAT_PROMPT, session_id="s1", turn_id="t1",
            )
            state = system.status(turn_id="t1")
            self.assertFalse(state["change_required"])
            # Only the real project path survives; the harness script named by
            # the instructions file is not a change target.
            self.assertEqual(state["requested_paths"],
                             ["docs/qa/2026-10-05-arb-stall-triage.md"])

            # A long reviewer-style turn with no code mutation must not arm the
            # progress lease (KEE-804: 23 mutations, 0 relevant, 3 blocks).
            for index in range(12):
                system.observe_tool_call(
                    tool_name="read_file", args={"path": f"source-{index}.md"},
                    status="ok", result="content", error_message="",
                    tool_call_id=f"r{index}", session_id="s1", turn_id="t1",
                )
            self.assertFalse(system.status(turn_id="t1")["progress_challenge_issued"])
            self.assertIsNone(system.before_tool_call(
                tool_name="read_file", args={"path": "docs/qa/2026-10-05-arb-stall-triage.md"},
                session_id="s1", turn_id="t1", tool_call_id="read-after-stall",
            ))
            self.assertIsNone(system.completion_gate(
                turn_id="t1", session_id="s1", final_response="Verdict recorded on the board.",
                coding=True, changed_paths=["docs/qa/2026-10-05-arb-stall-triage.md"],
            ))

    def test_harness_named_path_is_filtered_even_in_a_plain_task(self):
        # Guard against over-filtering: only harness paths are dropped, and any
        # remaining project path is still treated as a requested target.
        self.assertEqual(
            requested_paths("Update skills/paperclip/scripts/paperclip-upload-artifact.sh"), ())
        self.assertEqual(requested_paths("Update docs/qa/memo.md"), ("docs/qa/memo.md",))
        self.assertTrue(mutation_is_relevant(
            "write_file", {"path": "docs/qa/memo.md", "content": "x"},
            requested_paths("Update docs/qa/memo.md")))


if __name__ == "__main__":
    unittest.main()
