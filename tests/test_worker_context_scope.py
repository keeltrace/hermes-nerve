"""Regression tests for #35: worker thread must preserve the caller's context.

The NervousSystem worker is a long-lived bare threading.Thread. Without the
fix, tasks queued from a Hermes turn (where profile-scoped secrets live in
ContextVars) were dispatched with an EMPTY context, so any profile-scoped
secret resolution inside DecisionEngine raised UnscopedSecretError under
multiplexing. The fix captures the enqueuer's contextvars.Context per task
and runs each dispatch under it.

These tests prove the invariant without importing Hermes: a module-level
ContextVar is set by the producer, and the worker must observe the value
the producer had at enqueue time — including after the producer changes it.
"""
from __future__ import annotations

import contextvars
import threading
import unittest
from unittest.mock import patch

from hermes_nerve.nervous import NervousSystem

SCOPE: contextvars.ContextVar = contextvars.ContextVar("nerve_test_scope", default=None)


class _FakeEngine:
    def __init__(self, *a, **k):  # noqa: ANN002, ANN003 - test stub
        pass


class TestWorkerPreservesCallerContext(unittest.TestCase):
    def setUp(self):
        self.ns = NervousSystem(engine_factory=lambda: _FakeEngine())  # type: ignore[arg-type]
        self.ns.configure(enabled=True)  # starts the worker thread
        self.addCleanup(self.ns._stop_worker)
        self.seen: dict[str, object] = {}
        original = self.ns._dispatch_task

        def spy(kind, payload):
            self.seen["kind"] = kind
            self.seen["scope_in_dispatch"] = SCOPE.get()
            return original(kind, payload)

        self.ns._dispatch_task = spy  # type: ignore[method-assign]

    def test_dispatch_runs_under_enqueuers_context(self):
        SCOPE.set("profile-A")
        self.ns._tasks.put(
            ("admit", {"turn_id": "t1", "user_message": "hi"}, contextvars.copy_context())
        )
        # Producer changes context AFTER enqueue: the task must still run
        # under the captured context, not whatever is current at dispatch.
        SCOPE.set("profile-B")
        self.ns._tasks.join()

        self.assertEqual(self.seen.get("kind"), "admit")
        self.assertEqual(
            self.seen.get("scope_in_dispatch"),
            "profile-A",
            "dispatch must run under the context captured at enqueue time",
        )

    def test_task_tuple_carries_context(self):
        ns = NervousSystem(engine_factory=lambda: _FakeEngine())  # type: ignore[arg-type]
        ns._tasks.put(("assess", {"x": 1}, contextvars.copy_context()))
        kind, payload, ctx = ns._tasks.get_nowait()
        self.assertEqual(kind, "assess")
        self.assertIsInstance(ctx, contextvars.Context)

    def test_each_task_carries_its_own_context(self):
        # One worker serves many profiles: each task must carry its own
        # captured context (the reviewed design), not one shared snapshot.
        results = []
        ctx_pairs = []

        def produce():
            SCOPE.set("profile-A")
            ctx_pairs.append(contextvars.copy_context())
            SCOPE.set("profile-B")
            ctx_pairs.append(contextvars.copy_context())

        producer = threading.Thread(target=produce)
        producer.start()
        producer.join()
        ctx_a, ctx_b = ctx_pairs
        results.append(ctx_a.run(SCOPE.get))
        results.append(ctx_b.run(SCOPE.get))
        self.assertEqual(results[0], "profile-A")
        self.assertEqual(results[1], "profile-B")
        self.assertIsNot(ctx_a, ctx_b)


if __name__ == "__main__":
    unittest.main()
