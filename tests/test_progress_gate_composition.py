"""Progress gate composition: empty targets are a composition error, not idleness.

Regression: ``kanban-dispatch-heartbeat`` turns composed ``requested_paths: []``
because the prompt named a board target (card ids, ``kanban.db``) and a cron
output directory instead of a source file. The gate then reported
``progress_completion_blocked`` / "no task-relevant mutation was attempted" for
turns that had created a card and posted receipt comments (t_ac8ba462).
"""
import hashlib
import json
import os
import sqlite3
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from hermes_nerve import nervous
from hermes_nerve.progress_integrity import (
    compose_requested_paths,
    current_profile_name,
    requested_paths,
    verify_board_mutation,
)

try:
    from tests.test_nervous import ScriptedEngine
except ModuleNotFoundError:  # `unittest discover -s tests` imports top-level module names
    from test_nervous import ScriptedEngine


PROFILE = "tester"
HEARTBEAT_PROMPT = (
    "Heartbeat: cron job fa0ed41171f7 wrote nothing to "
    "/opt/data/profiles/software-developer/cron/output/fa0ed41171f7/ - "
    "create a follow-up card for t_7c4f3675 and comment the receipt in kanban.db"
)
# Verbatim production prompt (state.db message 796824, the kanban-dispatch-heartbeat
# turn the gate deadlocked on). Its composition is the cron output directory only;
# "edit it with `hermes cron edit ...`" is what makes the turn change-required.
INCIDENT_PROMPT = (
    "[Cronjob \"kanban-dispatch-heartbeat\" output \u2014 scheduled job, not the user. "
    "Review it, act on anything that needs action, and summarize for the chat.]\n\n"
    "\u26a0\ufe0f Cron 'kanban-dispatch-heartbeat' failed: Script execution failed: "
    "[Errno 11] Resource temporarily unavailable. See the full run with "
    "`hermes cron runs fa0ed41171f7` (output saved under "
    "/opt/data/profiles/software-developer/cron/output/fa0ed41171f7/); run it again with "
    "`hermes cron run fa0ed41171f7`, edit it with `hermes cron edit fa0ed41171f7`, "
    "or pause it with `hermes cron pause fa0ed41171f7`."
)


class GateHarness(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.root = Path(self._td.name)
        self.events = self.root / "nervous-events.jsonl"
        env = patch.dict(os.environ, {
            "HERMES_NERVE_NERVOUS_EVENTS": str(self.events),
            "HERMES_PROFILE": PROFILE,
            "HERMES_KANBAN_TASK": "",
        }, clear=False)
        env.start()
        self.addCleanup(env.stop)
        # The binding is the profile the turn acts as; the home path wins over
        # HERMES_PROFILE when the home is a named-profile home.
        self.actor = current_profile_name()
        ScriptedEngine.reset()
        self.system = nervous.NervousSystem(engine_factory=ScriptedEngine)
        self.system.configure(enabled=True, admission_enabled=False, mode="correct_next")

    def start(self, message: str, turn_id: str = "t1") -> nervous.TurnState:
        self.system.start_turn(user_message=message, session_id="s1", turn_id=turn_id)
        return self.system._turns[turn_id]

    def records(self, kind: str | None = None) -> list[dict]:
        if not self.events.exists():
            return []
        rows = [json.loads(line) for line in self.events.read_text().splitlines() if line.strip()]
        return [row for row in rows if kind is None or row.get("kind") == kind]

    def board_db(self) -> Path:
        db = self.root / "kanban.db"
        con = sqlite3.connect(db)
        con.executescript(
            "create table tasks (id text primary key, title text, assignee text, "
            "status text, created_by text, created_at real);"
            "create table task_comments (id integer primary key autoincrement, "
            "task_id text, author text, body text, created_at real);"
        )
        now = time.time()
        con.execute(
            "insert into tasks values (?,?,?,?,?,?)",
            ("t_deadbeef", "Fix the gate", "software-developer", "todo", self.actor, now),
        )
        con.execute(
            "insert into task_comments (task_id, author, body, created_at) values (?,?,?,?)",
            ("t_ac8ba462", self.actor, "receipt: landed", now),
        )
        con.commit()
        con.close()
        return db

    def _use_board(self) -> Path:
        db = self.board_db()
        env = patch.dict(os.environ, {"HERMES_KANBAN_DB": str(db)}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        return db


class CompositionFixTests(GateHarness):
    def test_heartbeat_prompt_composes_board_and_directory_targets(self):
        composition = compose_requested_paths(HEARTBEAT_PROMPT)
        self.assertIn(
            "/opt/data/profiles/software-developer/cron/output/fa0ed41171f7",
            composition.paths,
        )
        self.assertIn("kanban.db", composition.paths)
        self.assertEqual(len(composition.paths), len(composition.sources))
        rules = {source["rule"] for source in composition.sources}
        self.assertEqual(rules, {"abs_dir", "board_db"})
        for source in composition.sources:
            self.assertIn(source["path"], composition.paths)
            self.assertRegex(source["line_sha256"], r"^[0-9a-f]{64}$")

    def test_card_id_alone_composes_the_board_target(self):
        composition = compose_requested_paths("Continue t_7c4f3675 and report back")
        self.assertEqual(composition.paths, ("kanban.db",))
        self.assertEqual(composition.sources[0]["rule"], "card_id")

    def test_file_targets_still_compose_unchanged(self):
        self.assertEqual(requested_paths("Fix app/calc.py and run tests"), ("app/calc.py",))


class EmptyCompositionDiagnosticTests(GateHarness):
    def test_empty_composition_emits_one_diagnostic_and_never_the_idleness_block(self):
        state = self.start("Fix all the issues and make it right.")
        self.assertTrue(state.change_required)
        self.assertEqual(state.requested_paths, ())

        first = self.system.completion_gate(turn_id="t1", coding=False)
        second = self.system.completion_gate(turn_id="t1", coding=False)

        self.assertIsNotNone(first)
        self.assertIn("gate_composition_empty", first["message"])
        self.assertEqual(first, second, "the verdict must be deterministic")
        self.assertEqual(len(self.records("gate_composition_empty")), 1)
        self.assertEqual(self.records("progress_completion_blocked"), [])

        event = self.records("gate_composition_empty")[0]
        self.assertEqual(
            event["reason"], "requested_paths composed empty for a change-required prompt"
        )
        self.assertEqual(event["attempted_composition"], [])
        self.assertTrue(event["token_source"])

    def test_diagnostic_carries_only_hashes_of_the_prompt(self):
        secret = "sk-live-abcdef0123456789"
        message = f"Fix the deployment and rotate {secret} before you finish."
        self.start(message)
        self.system.completion_gate(turn_id="t1", coding=False)

        raw = self.events.read_text()
        self.assertNotIn(secret, raw)
        self.assertNotIn("rotate", raw)
        self.assertNotIn("deployment", raw)

        event = self.records("gate_composition_empty")[0]
        self.assertEqual(
            event["prompt_sha256"], hashlib.sha256(message.encode()).hexdigest()
        )
        self.assertTrue(event["token_source"])
        for entry in event["token_source"]:
            self.assertRegex(entry["line_sha256"], r"^[0-9a-f]{64}$")
            self.assertNotIn("text", entry)

    def test_empty_composition_blocks_even_after_a_relevant_mutation(self):
        repo = self.root / "scratch" / "candidate"
        repo.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        source = repo / "consumer.py"
        source.write_text("VALUE = 1\n")
        subprocess.run(["git", "-C", str(repo), "add", "consumer.py"], check=True)

        state = self.start("Fix everything.")
        self.system.observe_tool_call(
            tool_name="patch", args={"path": str(source)}, status="ok",
            result="", turn_id="t1", tool_call_id="c1",
        )
        self.assertEqual(state.relevant_mutation_attempts, 1)

        blocked = self.system.completion_gate(turn_id="t1", coding=True)
        self.assertIsNotNone(blocked, "empty composition must never pass silently")
        self.assertIn("gate_composition_empty", blocked["message"])
        self.assertEqual(self.records("progress_completion_blocked"), [])


class NonEmptyCompositionControlTests(GateHarness):
    def test_nonempty_composition_still_blocks_without_a_matching_mutation(self):
        state = self.start("Fix app/calc.py")
        self.assertEqual(state.requested_paths, ("app/calc.py",))

        blocked = self.system.completion_gate(turn_id="t1", coding=True)
        self.assertIsNotNone(blocked)
        self.assertIn("no task-relevant mutation", blocked["message"])

        event = self.records("progress_completion_blocked")[0]
        self.assertEqual(event["reason"], "no task-relevant mutation was attempted")
        self.assertEqual(event["requested_paths"], ["app/calc.py"])
        self.assertTrue(event["token_source"], "the block must name the composing source")
        self.assertFalse(event["composition"]["empty"])
        self.assertEqual(event["composition"]["path_count"], 1)
        self.assertEqual(self.records("gate_composition_empty"), [])


class BoardMutationCreditTests(GateHarness):
    def test_board_only_writes_are_credited_by_row_read_back(self):
        self._use_board()
        state = self.start("Fix the nerve gate in kanban.db")

        self.system.observe_tool_call(
            tool_name="kanban_create",
            args={"title": "Fix the gate"},
            status="ok",
            result=json.dumps({"ok": True, "task_id": "t_deadbeef"}),
            turn_id="t1",
            tool_call_id="c1",
        )
        self.system.observe_tool_call(
            tool_name="kanban_comment",
            args={"task_id": "t_ac8ba462", "body": "receipt: landed"},
            status="ok",
            result=json.dumps({"ok": True, "task_id": "t_ac8ba462", "comment_id": 7}),
            turn_id="t1",
            tool_call_id="c2",
        )

        self.assertEqual(state.board_mutations_credited, 2)
        self.assertEqual(state.relevant_mutation_attempts, 2)
        credited = self.records("board_mutation_credited")
        self.assertEqual([row["table"] for row in credited], ["tasks", "task_comments"])
        self.assertEqual([row["row_id"] for row in credited], ["t_deadbeef", "1"])
        for row in credited:
            self.assertEqual(row["actor"], self.actor)
            self.assertTrue(row["verified"])
            self.assertIn(row["scope"], {"orchestrator-window", "created-card", "lineage"})
        self.assertIsNone(
            self.system.completion_gate(turn_id="t1", coding=True),
            "verified board work satisfies the progress gate",
        )

    def test_unverified_or_unrelated_writes_are_not_credited(self):
        db = self._use_board()
        con = sqlite3.connect(db)
        con.execute(
            "insert into task_comments (task_id, author, body, created_at) values (?,?,?,?)",
            ("t_ac8ba462", "someone-else", "forged receipt", time.time()),
        )
        con.execute(
            "insert into task_comments (task_id, author, body, created_at) values (?,?,?,?)",
            ("t_ac8ba462", self.actor, "stale receipt", time.time() - 3600),
        )
        con.commit()
        con.close()

        state = self.start("Fix the nerve gate in kanban.db")
        self.system.observe_tool_call(
            tool_name="kanban_comment",
            args={"task_id": "t_ac8ba462", "body": "never written"},
            status="ok",
            result=json.dumps({"ok": True}),
            turn_id="t1",
            tool_call_id="c1",
        )
        self.system.observe_tool_call(
            tool_name="kanban_comment",
            args={"task_id": "t_ac8ba462", "body": "forged receipt"},
            status="ok",
            result=json.dumps({"ok": True}),
            turn_id="t1",
            tool_call_id="c2",
        )
        self.system.observe_tool_call(
            tool_name="kanban_comment",
            args={"task_id": "t_ac8ba462", "body": "stale receipt"},
            status="ok",
            result=json.dumps({"ok": True}),
            turn_id="t1",
            tool_call_id="c3",
        )
        self.system.observe_tool_call(
            tool_name="kanban_create",
            args={"title": "Not on the board"},
            status="ok",
            result=json.dumps({"ok": True, "task_id": "t_missing0"}),
            turn_id="t1",
            tool_call_id="c4",
        )

        self.assertEqual(state.board_mutations_credited, 0)
        self.assertEqual(self.records("board_mutation_credited"), [])

    def test_verifier_rejects_a_write_outside_the_turn_window(self):
        db = self._use_board()
        credit = verify_board_mutation(
            "comment",
            {"task_id": "t_ac8ba462", "body": "receipt: landed"},
            json.dumps({"ok": True}),
            profile=self.actor,
            started_wall=time.time() + 600,
            db_path=db,
        )
        self.assertIsNone(credit)


class BoardCreditSatisfiesPersistedChangeTests(GateHarness):
    """The incident shape: board work credited while changed_paths match nothing.

    The production caller only reports file edits, so an orchestrator turn that
    did its job through the board arrives with a changed_paths list that matches
    no requested target. Read-back-verified board credit must satisfy the
    persisted-change branch; zero credit must keep blocking.
    """

    def _incident_turn(self) -> nervous.TurnState:
        self._use_board()
        state = self.start(INCIDENT_PROMPT)
        self.assertTrue(state.change_required)
        self.assertEqual(
            state.requested_paths,
            ("/opt/data/profiles/software-developer/cron/output/fa0ed41171f7",),
            "composition must see the cron output directory the prompt names",
        )
        self.system.observe_tool_call(
            tool_name="kanban_create",
            args={"title": "Fix the gate"},
            status="ok",
            result=json.dumps({"ok": True, "task_id": "t_deadbeef"}),
            turn_id="t1",
            tool_call_id="c1",
        )
        self.system.observe_tool_call(
            tool_name="kanban_comment",
            args={"task_id": "t_ac8ba462", "body": "receipt: landed"},
            status="ok",
            result=json.dumps({"ok": True, "task_id": "t_ac8ba462", "comment_id": 9}),
            turn_id="t1",
            tool_call_id="c2",
        )
        return state

    def test_board_credit_prevents_the_incident_block_on_nonmatching_changed_paths(self):
        state = self._incident_turn()
        self.assertEqual(state.board_mutations_credited, 2)
        self.assertEqual(state.relevant_mutation_attempts, 2)

        edited = ["/opt/data/cache/scratch/rt-dispatch-spend.md"]
        self.assertIsNone(
            self.system.completion_gate(turn_id="t1", coding=True, changed_paths=edited),
            "credited board work must satisfy the gate even when the edit list matches no target",
        )
        self.assertEqual(self.records("progress_completion_blocked"), [])
        self.assertEqual(len(self.records("board_mutation_credited")), 2)

    def test_negative_control_zero_credit_still_blocks_the_incident_turn(self):
        self._use_board()
        state = self.start(INCIDENT_PROMPT)
        # A claimed comment that never landed is not progress: read-back finds
        # no row, so the turn carries zero credit.
        self.system.observe_tool_call(
            tool_name="kanban_comment",
            args={"task_id": "t_ac8ba462", "body": "never written"},
            status="ok",
            result=json.dumps({"ok": True}),
            turn_id="t1",
            tool_call_id="c1",
        )
        self.assertEqual(state.board_mutations_credited, 0)

        blocked = self.system.completion_gate(
            turn_id="t1",
            coding=True,
            changed_paths=["/opt/data/cache/scratch/rt-dispatch-spend.md"],
        )
        self.assertIsNotNone(blocked, "zero credit must keep the idleness guard")
        self.assertIn("no task-relevant mutation", blocked["message"])
        event = self.records("progress_completion_blocked")[0]
        self.assertEqual(event["reason"], "no task-relevant mutation was attempted")
        self.assertEqual(event["board_mutations_credited"], 0)
        # Composition itself is fixed: the block names the real target rather
        # than the empty set that caused the deadlock.
        self.assertEqual(
            event["requested_paths"],
            ["/opt/data/profiles/software-developer/cron/output/fa0ed41171f7"],
        )
        self.assertTrue(event["token_source"])


class PersistedChangeGuardControlTests(GateHarness):
    """Branch 2 stays intact when no board write is credited."""

    def test_zero_credit_still_blocks_on_nonmatching_changed_paths(self):
        state = self.start("Fix app/calc.py")
        self.assertEqual(state.requested_paths, ("app/calc.py",))
        self.system.observe_tool_call(
            tool_name="patch",
            args={"path": "app/calc.py"},
            status="ok",
            result="",
            turn_id="t1",
            tool_call_id="c1",
        )
        self.assertEqual(state.relevant_mutation_attempts, 1)
        self.assertEqual(state.board_mutations_credited, 0)

        blocked = self.system.completion_gate(
            turn_id="t1",
            coding=True,
            changed_paths=["/opt/data/cache/scratch/rt-dispatch-spend.md"],
        )
        self.assertIsNotNone(blocked)
        self.assertIn("no task-relevant changed path", blocked["message"])
        event = self.records("progress_completion_blocked")[0]
        self.assertEqual(event["reason"], "verification reports no task-relevant changed path")
        self.assertEqual(event["requested_paths"], ["app/calc.py"])
        self.assertTrue(event["token_source"], "the block must name the composing source")
        self.assertEqual(event["board_mutations_credited"], 0)


class CompositionDiagnosticProvenanceTests(GateHarness):
    """A filtered token is named by hash, never by raw prompt text."""

    def test_candidate_line_tokens_are_hashed_when_input_attachment_filters_composition(self):
        token = "img_deadbeef.png"
        message = (
            "Fix the screenshot at "
            "/opt/data/cache/documents/native-inputs/abc123/" + token
        )
        state = self.start(message)
        self.assertTrue(state.change_required)
        self.assertEqual(state.requested_paths, (), "an uploaded input is not a change target")

        blocked = self.system.completion_gate(turn_id="t1", coding=False)
        self.assertIsNotNone(blocked)
        self.assertIn("gate_composition_empty", blocked["message"])

        raw = self.events.read_text()
        self.assertNotIn(token, raw)
        self.assertNotIn("native-inputs", raw)
        event = self.records("gate_composition_empty")[0]
        candidate_lines = [
            entry for entry in event["token_source"] if entry["rule"] == "candidate_line"
        ]
        self.assertTrue(candidate_lines, "the diagnostic must name the rejected line")
        self.assertTrue(
            any(
                candidate["rule"] == "path_token"
                for entry in candidate_lines
                for candidate in entry["candidates"]
            )
        )
        for entry in candidate_lines:
            self.assertRegex(entry["line_sha256"], r"^[0-9a-f]{64}$")
            for candidate in entry["candidates"]:
                self.assertEqual(set(candidate), {"rule", "token_sha256", "token_chars"})
                self.assertRegex(candidate["token_sha256"], r"^[0-9a-f]{64}$")


if __name__ == "__main__":
    unittest.main()
