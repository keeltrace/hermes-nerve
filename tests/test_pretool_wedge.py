"""Regression guards for the pre_tool_call chain wedge (card t_07b7a40d).

The wedge: Hermes runs the whole nerve pre_tool_call chain as ONE hook
callback under an 8.0s budget. ``work_supervision`` used to write an
``autobind_task_missing`` diagnostic (BEGIN IMMEDIATE, 30s busy timeout) on
every tool call for sessions whose task id is not a Kanban card. Under write
contention that write blew the 8s budget; Hermes abandoned the callback,
refused the tool with "pre_tool_call plugin callback timed out or is still
running", and suppressed the entire chain for 60s — minutes of blocked tools.

These tests pin: (a) the chain is bounded whatever an inner callback does,
(b) the hot path no longer write-amplifies, (c) the completion gate is
unchanged (still blocks a genuine false completion).
"""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from hermes_nerve import gate, nervous
from hermes_nerve.progress_integrity import tool_kind
from hermes_nerve.work import autobind
from hermes_nerve.work import hooks as work_hooks
from hermes_nerve.work import runtime as work_runtime
from tests.test_nervous import ScriptedEngine


def _events_env(td: str) -> dict[str, str]:
    # Events live outside the workspace TemporaryDirectory: a late async flush
    # must never race the cleanup (see test_progress_integrity_qol).
    return {
        "HERMES_NERVE_NERVOUS_EVENTS": str(Path(td) / "nervous.jsonl"),
        "HERMES_NERVE_OUTCOMES": str(Path(td) / "outcomes.jsonl"),
    }


class PreToolChainBudgetTests(unittest.TestCase):
    def setUp(self) -> None:
        # Tolerant of the pre-fix build so a red run fails on BEHAVIOUR
        # (elapsed time / directive), never on a missing test seam.
        inflight = getattr(gate, "_ADVISORY_INFLIGHT", None)
        if inflight is not None:
            inflight.clear()
        diag_once = getattr(autobind, "_DIAG_ONCE", None)
        if diag_once is not None:
            diag_once.clear()
        miss = getattr(autobind, "_MISS_CACHE", None)
        if miss is not None:
            miss.clear()

    def tearDown(self) -> None:
        inflight = getattr(gate, "_ADVISORY_INFLIGHT", None)
        if inflight is not None:
            inflight.clear()
        work_runtime.configure(enabled=False)

    def test_advisory_callback_overrun_is_skipped_inside_budget(self):
        trace: list[dict] = []

        def slow(**_kwargs):
            time.sleep(5.0)

        chain = gate.instrument_pre_tool_chain([("work_supervision", slow)], trace.append)
        with patch.dict(os.environ, {"NERVE_PRETOOL_ADVISORY_BUDGET_MS": "200"}, clear=False):
            started = time.monotonic()
            directive = chain(tool_name="terminal", args={"command": "date -u"}, session_id="s")
            elapsed = time.monotonic() - started

        self.assertIsNone(directive)
        self.assertLess(elapsed, 1.0, "chain must not wait for an overrunning advisory callback")
        self.assertEqual(trace[0]["outcome"], "timeout")
        self.assertEqual(trace[0]["budget_ms"], 200.0)

    def test_lifecycle_tool_fails_closed_when_supervision_overruns(self):
        def slow(**_kwargs):
            time.sleep(5.0)

        chain = gate.instrument_pre_tool_chain([("work_supervision", slow)], lambda rec: None)
        with patch.dict(os.environ, {"NERVE_PRETOOL_ADVISORY_BUDGET_MS": "150"}, clear=False):
            directive = chain(
                tool_name="kanban_complete", args={"task_id": "t_x"}, session_id="s",
            )
        assert directive is not None
        self.assertEqual(directive["action"], "block")
        self.assertIn("could not be verified", directive["message"])

    def test_critical_callback_overrun_fails_to_human_approval(self):
        def slow(**_kwargs):
            time.sleep(5.0)

        chain = gate.instrument_pre_tool_chain([("action_gate", slow, True)], lambda rec: None)
        with patch.dict(os.environ, {"NERVE_PRETOOL_CRITICAL_BUDGET_MS": "150"}, clear=False):
            directive = chain(tool_name="terminal", args={"command": "date -u"}, session_id="s")
        assert directive is not None
        self.assertEqual(directive["action"], "approve")
        self.assertEqual(directive["source"], "nerve-action-gate-failure")

    def test_chain_deadline_stops_starting_callbacks(self):
        """The 7s chain deadline must stop starting work, not just shorten it.

        Three 2.5s callbacks land exactly on 7.5s (> the deadline), so a fourth
        callback is the first one the chain must refuse to start. Without the
        deadline all four run to their own 2.5s budget (10s) — past Hermes'
        8.0s callback cap, which abandons the whole chain.
        """
        started: list[str] = []

        def sleeper(tag: str):
            def callback(**_kwargs):
                started.append(tag)
                time.sleep(3.0)  # longer than the 2.5s advisory budget
            return callback

        trace: list[dict] = []
        chain = gate.instrument_pre_tool_chain(
            [("a", sleeper("a")), ("b", sleeper("b")), ("c", sleeper("c")), ("d", sleeper("d"))],
            trace.append,
        )
        with patch.dict(os.environ, {}, clear=False):
            for var in (
                "NERVE_PRETOOL_CHAIN_BUDGET_MS",
                "NERVE_PRETOOL_ADVISORY_BUDGET_MS",
                "NERVE_PRETOOL_CRITICAL_BUDGET_MS",
            ):
                os.environ.pop(var, None)
            started_at = time.monotonic()
            directive = chain(tool_name="terminal", args={"command": "date -u"}, session_id="deadline")
            elapsed = time.monotonic() - started_at

        self.assertIsNone(directive)
        self.assertEqual(started, ["a", "b", "c"], "a callback past the chain deadline must not start")
        self.assertEqual(trace[3]["outcome"], "timeout:chain_budget_exhausted")
        self.assertLess(trace[3]["budget_ms"], 1.0)
        self.assertLess(elapsed, 7.6, f"chain deadline not enforced: {elapsed:.2f}s")


    def test_write_locked_supervision_store_cannot_hold_the_chain(self):
        """The live wedge: a competing writer on work-supervision.sqlite3."""
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "work-supervision.sqlite3"
            work_runtime.configure(enabled=True, store_path=str(db))
            work_runtime.supervisor()  # create schema before taking the lock
            holder = sqlite3.connect(str(db), isolation_level=None, check_same_thread=False)
            holder.execute("PRAGMA busy_timeout=8000")
            holder.execute("BEGIN IMMEDIATE")
            try:
                trace: list[dict] = []
                chain = gate.instrument_pre_tool_chain(
                    [("work_supervision", work_hooks.pre_tool_call),
                     ("nervous", lambda **_kw: None)],
                    trace.append,
                )
                env = {"NERVE_PRETOOL_ADVISORY_BUDGET_MS": "4000"}
                with patch.dict(os.environ, env, clear=False):
                    os.environ.pop("HERMES_KANBAN_TASK", None)
                    os.environ.pop("HERMES_KANBAN_TASK_ID", None)
                    started = time.monotonic()
                    directive = chain(
                        tool_name="terminal", args={"command": "date -u"},
                        task_id="t_NOT_A_REAL_CARD", session_id="s", tool_call_id="c1",
                    )
                    elapsed = time.monotonic() - started
            finally:
                holder.rollback()
                holder.close()

        self.assertIsNone(directive)
        self.assertLess(
            elapsed, 2.5,
            f"a write-locked supervision store held the hook for {elapsed:.1f}s "
            "(unbounded BEGIN IMMEDIATE on the tool hot path)",
        )
        self.assertLess(trace[0]["chain_elapsed_ms"], 2500.0)

    def test_bare_date_u_after_blocked_completion_returns_inside_budget_5x(self):
        """AC2: the exact wedged state — blocked completion + busy supervision
        store — then five bare ``date -u`` calls through the real chain."""
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "work-supervision.sqlite3"
            work_runtime.configure(enabled=True, store_path=str(db))
            work_runtime.supervisor()
            holder = sqlite3.connect(str(db), isolation_level=None, check_same_thread=False)
            holder.execute("PRAGMA busy_timeout=8000")
            holder.execute("BEGIN IMMEDIATE")
            released = threading.Event()

            def _release() -> None:
                time.sleep(6.0)
                try:
                    holder.rollback()
                    holder.close()
                finally:
                    released.set()

            threading.Thread(target=_release, daemon=True).start()
            try:
                with patch.dict(os.environ, _events_env(td), clear=False):
                    os.environ.pop("HERMES_KANBAN_TASK", None)
                    os.environ.pop("HERMES_KANBAN_TASK_ID", None)
                    system = nervous.NervousSystem(engine_factory=ScriptedEngine)
                    system.configure(enabled=True, admission_enabled=False, mode="correct_next")
                    system.start_turn(
                        user_message="Fix src/parser.py and make the regression tests pass",
                        session_id="s1", turn_id="t1",
                    )
                    system.observe_tool_call(
                        tool_name="write_file", args={"path": "src/parser.py", "content": "x=2"},
                        status="ok", result="written", error_message="", tool_call_id="m1",
                        session_id="s1", turn_id="t1",
                    )
                    blocked = system.completion_gate(
                        turn_id="t1", session_id="s1", final_response="Done",
                        coding=True, changed_paths=["src/parser.py"],
                    )
                    assert blocked is not None, "completion gate must block: no verified test run"
                    self.assertEqual(blocked["action"], "continue")
                    self.assertEqual(system.status(turn_id="t1")["verified_test_passes"], 0)

                    chain = gate.instrument_pre_tool_chain(
                        [("work_supervision", work_hooks.pre_tool_call),
                         ("nervous", system.before_tool_call)],
                        lambda rec: None,
                    )
                    with patch.dict(
                        os.environ, {"NERVE_PRETOOL_ADVISORY_BUDGET_MS": "1500"}, clear=False,
                    ):
                        for i in range(5):
                            started = time.monotonic()
                            directive = chain(
                                tool_name="terminal", args={"command": "date -u"},
                                task_id="t_NOT_A_REAL_CARD", session_id="s1", turn_id="t1",
                                tool_call_id=f"date-{i}",
                            )
                            elapsed = time.monotonic() - started
                            with self.subTest(repeat=i):
                                self.assertIsNone(directive)
                                self.assertLess(elapsed, 1.5)
            finally:
                released.wait(8.0)


class AdvisorySingleFlightScopeTests(unittest.TestCase):
    """The single-flight guard is per (callback, session), never process-wide.

    A gateway process serves many sessions and Hermes runs each hook call on
    its own worker. A global guard made session B's callback vanish (0.0ms,
    ``timeout:still_running``) whenever session A's callback was alive: B's
    ordinary tools ran without the work/hooks fences, and B's lifecycle calls
    were refused with a budget it never spent.
    """

    def setUp(self) -> None:
        gate._ADVISORY_INFLIGHT.clear()

    def tearDown(self) -> None:
        gate._ADVISORY_INFLIGHT.clear()

    def test_overlapping_sessions_do_not_cross_block_supervision(self):
        calls: list[tuple[str, str]] = []

        def slow(**kwargs):
            calls.append((kwargs.get("session_id"), kwargs.get("tool_name")))
            time.sleep(0.6)

        trace: list[dict] = []
        chain = gate.instrument_pre_tool_chain([("work_supervision", slow)], trace.append)
        out: dict[str, object] = {}

        def run(key: str, session: str, tool: str) -> None:
            args = {"command": "date -u"} if tool == "terminal" else {"task_id": "t_x"}
            out[key] = chain(tool_name=tool, args=args, session_id=session)

        first = threading.Thread(target=run, args=("a", "sessA", "terminal"))
        first.start()
        time.sleep(0.15)  # A is inside its callback when B arrives
        second = threading.Thread(target=run, args=("b", "sessB", "kanban_complete"))
        second.start()
        first.join(5.0)
        second.join(5.0)

        self.assertEqual(
            sorted(session for session, _tool in calls), ["sessA", "sessB"],
            "both sessions must run their own supervision callback",
        )
        self.assertIsNone(out["a"])
        self.assertIsNone(out["b"], "an overlapping session must never be cross-blocked")
        self.assertNotIn("timeout:still_running", [rec["outcome"] for rec in trace])

    def test_same_session_retry_is_refused_with_an_honest_reason(self):
        calls: list[str] = []

        def slow(**kwargs):
            calls.append(kwargs.get("session_id"))
            time.sleep(0.8)

        trace: list[dict] = []
        chain = gate.instrument_pre_tool_chain([("work_supervision", slow)], trace.append)
        first_out: list[object] = []

        def first_call() -> None:
            with patch.dict(os.environ, {"NERVE_PRETOOL_ADVISORY_BUDGET_MS": "120"}, clear=False):
                first_out.append(chain(
                    tool_name="terminal", args={"command": "date -u"}, session_id="sessC",
                ))

        worker = threading.Thread(target=first_call)
        worker.start()
        worker.join(3.0)
        self.assertIsNone(first_out[0])
        self.assertEqual(trace[0]["outcome"], "timeout")  # its own callback overran 120ms

        with patch.dict(os.environ, {"NERVE_PRETOOL_ADVISORY_BUDGET_MS": "2000"}, clear=False):
            blocked = chain(tool_name="kanban_complete", args={"task_id": "t_x"}, session_id="sessC")
            other = chain(tool_name="kanban_complete", args={"task_id": "t_x"}, session_id="sessD")

        assert blocked is not None
        self.assertEqual(blocked["action"], "block")
        self.assertEqual(blocked["source"], "nerve-pretool-still-running")
        self.assertIn("still finishing an earlier call", blocked["message"])
        self.assertNotIn("budget", blocked["message"], "a skipped call must not claim a budget it never spent")
        self.assertIsNone(other, "another session must not inherit sessC's in-flight run")
        self.assertEqual(calls, ["sessC", "sessD"])

    def test_finished_runs_are_pruned_from_the_guard(self):
        def quick(**_kwargs):
            return None

        chain = gate.instrument_pre_tool_chain([("work_supervision", quick)], lambda rec: None)
        for i in range(25):
            self.assertIsNone(chain(
                tool_name="terminal", args={"command": "date -u"}, session_id=f"sess-{i}",
            ))
        time.sleep(0.1)  # let the daemon runners finish
        self.assertLessEqual(
            len(gate._ADVISORY_INFLIGHT), 1,
            "a long-lived gateway must not accumulate one guard entry per session",
        )

class VerifiedTestPassCounterTests(unittest.TestCase):
    def test_recognised_test_script_moves_the_counter(self):
        """AC3: assert the counter moves, not just that the command ran."""
        self.assertEqual(tool_kind("terminal", {"command": "bash test.sh"}), "test")
        with tempfile.TemporaryDirectory() as td:
            with patch.dict(os.environ, _events_env(td), clear=False):
                system = nervous.NervousSystem(engine_factory=ScriptedEngine)
                system.configure(enabled=True, admission_enabled=False, mode="correct_next")
                system.start_turn(
                    user_message="Fix src/parser.py and make the regression tests pass",
                    session_id="s2", turn_id="t2",
                )
                system.observe_tool_call(
                    tool_name="write_file", args={"path": "src/parser.py", "content": "x=2"},
                    status="ok", result="written", error_message="", tool_call_id="m2",
                    session_id="s2", turn_id="t2",
                )
                self.assertEqual(system.status(turn_id="t2")["verified_test_passes"], 0)
                system.observe_tool_call(
                    tool_name="terminal", args={"command": "bash test.sh"}, status="ok",
                    result=json.dumps({"output": "1 passed in 0.02s", "exit_code": 0, "error": None}),
                    error_message="", tool_call_id="t2-run", session_id="s2", turn_id="t2",
                )
                self.assertEqual(system.status(turn_id="t2")["verified_test_passes"], 1)
                # Gate strictness preserved: the false completion is now clearable
                # only because a real test pass was observed.
                self.assertIsNone(system.completion_gate(
                    turn_id="t2", session_id="s2", final_response="Done",
                    coding=True, changed_paths=["src/parser.py"],
                ))


class GateStrictnessTests(unittest.TestCase):
    def test_false_completion_still_blocked_after_fix(self):
        """AC5: completion_gate() semantics unchanged."""
        with tempfile.TemporaryDirectory() as td:
            with patch.dict(os.environ, _events_env(td), clear=False):
                system = nervous.NervousSystem(engine_factory=ScriptedEngine)
                system.configure(enabled=True, admission_enabled=False, mode="correct_next")
                system.start_turn(
                    user_message="Fix src/parser.py and make the regression tests pass",
                    session_id="s3", turn_id="t3",
                )
                system.observe_tool_call(
                    tool_name="write_file", args={"path": "src/parser.py", "content": "x=2"},
                    status="ok", result="written", error_message="", tool_call_id="m3",
                    session_id="s3", turn_id="t3",
                )
                decision = system.completion_gate(
                    turn_id="t3", session_id="s3", final_response="All done, tests pass.",
                    coding=True, changed_paths=["src/parser.py"],
                )
                assert decision is not None
                self.assertEqual(decision["action"], "continue")
                self.assertIn("verification", decision["message"].lower())
                self.assertEqual(system.status(turn_id="t3")["verified_test_passes"], 0)


class HotPathDiagnosticTests(unittest.TestCase):
    def setUp(self) -> None:
        for attr in ("_DIAG_ONCE", "_MISS_CACHE"):
            cache = getattr(autobind, attr, None)
            if cache is not None:
                cache.clear()

    def tearDown(self) -> None:
        work_runtime.configure(enabled=False)

    def test_missing_task_diagnostic_is_written_once_not_per_call(self):
        """168k-row write storm: one row per (task, kind) per process."""
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "work-supervision.sqlite3"
            work_runtime.configure(enabled=True, store_path=str(db))
            supervisor = work_runtime.supervisor()
            calls = {"n": 0}

            def fake_lookup(task_id: str):
                calls["n"] += 1
                return None

            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("HERMES_KANBAN_TASK", None)
                os.environ.pop("HERMES_KANBAN_TASK_ID", None)
                with patch.object(autobind, "_load_canonical_task", fake_lookup):
                    for _ in range(50):
                        self.assertIsNone(
                            autobind.ensure_kanban_binding(
                                supervisor, task_id="local-session-id", session_id="s",
                            )
                        )
            self.assertEqual(calls["n"], 1, "negative lookups must be memoised")
            with sqlite3.connect(str(db)) as con:
                rows = con.execute(
                    "select count(*) from supervision_diagnostics where kind='autobind_task_missing'"
                ).fetchone()[0]
            self.assertEqual(rows, 1, "repeat telemetry must be deduped on the hot path")

    def test_hot_path_diagnostic_drops_when_the_store_lease_is_held(self):
        """The in-process lease: a diagnostics write must not queue behind it.

        ``add_diagnostic`` asks ``transaction()`` for a 250ms lease so it can
        never sit behind another thread's long write — the exact shape that
        wedged the hook. A lease it cannot take is DROPPED, not waited out.
        """
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "work-supervision.sqlite3"
            work_runtime.configure(enabled=True, store_path=str(db))
            store = work_runtime.supervisor().store
            held = threading.Event()
            release = threading.Event()

            def _hold_lease() -> None:
                with store._lock:
                    held.set()
                    release.wait(10.0)

            holder = threading.Thread(target=_hold_lease, daemon=True)
            holder.start()
            self.assertTrue(held.wait(2.0), "could not take the store lease for the test")
            try:
                started_at = time.monotonic()
                store.add_diagnostic(
                    task_id="t_x", kind="autobind_task_missing",
                    payload={"probe": "lease"}, created_at="2026-01-01T00:00:00Z",
                )
                elapsed = time.monotonic() - started_at
            finally:
                release.set()
                holder.join(2.0)

        self.assertLess(elapsed, 1.0, f"diagnostic write queued behind the store lease for {elapsed:.2f}s")
        self.assertEqual(store.diagnostics_dropped, 1, "a lease-refused diagnostic must be dropped")

    def test_hot_path_diagnostic_drops_under_a_competing_writer(self):
        """Cross-process bound: another connection's BEGIN IMMEDIATE.

        The card's live failure was a competing writer holding the database.
        The hot-path write must give up inside its 250ms lease and drop.
        """
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "work-supervision.sqlite3"
            work_runtime.configure(enabled=True, store_path=str(db))
            store = work_runtime.supervisor().store
            holder = sqlite3.connect(str(db), isolation_level=None, check_same_thread=False)
            holder.execute("PRAGMA busy_timeout=8000")
            holder.execute("BEGIN IMMEDIATE")
            try:
                started_at = time.monotonic()
                store.add_diagnostic(
                    task_id="t_x", kind="autobind_task_missing",
                    payload={"probe": "competing-writer"}, created_at="2026-01-01T00:00:00Z",
                )
                elapsed = time.monotonic() - started_at
            finally:
                holder.rollback()
                holder.close()

        self.assertLess(elapsed, 1.5, f"diagnostic write waited on a competing writer for {elapsed:.2f}s")
        self.assertEqual(store.diagnostics_dropped, 1, "a write-locked diagnostic must be dropped")


if __name__ == "__main__":
    unittest.main()
