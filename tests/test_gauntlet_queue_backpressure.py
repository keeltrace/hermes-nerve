"""Bounded local supervision queue: no provider or production writes."""
import contextvars
import queue

from hermes_nerve.nervous import NervousSystem


def test_background_queue_is_bounded():
    system = NervousSystem()
    assert 0 < system._tasks.maxsize <= 256


def test_saturated_enqueue_drops_without_blocking_or_losing_context():
    system = NervousSystem()
    system._tasks = queue.Queue(maxsize=1)
    marker = contextvars.ContextVar("queue_test_marker", default="missing")
    token = marker.set("caller-scope")
    try:
        assert system._enqueue_task("assess", {"turn_id": "first"}) is True
    finally:
        marker.reset(token)
    assert system._enqueue_task("admit", {"turn_id": "second"}) is False
    kind, payload, ctx = system._tasks.get_nowait()
    assert kind == "assess"
    assert payload == {"turn_id": "first"}
    assert ctx.get(marker) == "caller-scope"
    assert system._metrics["queue_dropped"] == 1
    assert system._metrics["queue_dropped_admit"] == 1


def test_saturated_producers_keep_local_supervision_and_clear_inflight(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_NERVE_NERVOUS_EVENTS", str(tmp_path / "events.jsonl"))
    system = NervousSystem()
    monkeypatch.setattr(system, "_ensure_worker", lambda: None)
    system.configure(enabled=True, admission_enabled=True, policy_memory_enabled=False)
    system._tasks = queue.Queue(maxsize=1)
    system._tasks.put_nowait(("admit", {}, contextvars.copy_context()))
    tid = system.start_turn(user_message="verify local implementation", session_id="overload")
    state = system._turns[tid]
    assert state.admission == "ON"
    assert system._metrics["admission_queue_fallback"] == 1
    result = system.emit_event({"turn_id": tid, "type": "DECISION", "goal": "critical decision",
                                "choices": ["A", "B"], "hermes_decision": "A", "materiality": 1.0})
    assert result["forwarded"] is False
    assert result["reason"] == "queue-saturated"
    assert state.assessment_inflight is False
    assert state.events_forwarded == 0
    assert system._metrics["events_forwarded"] == 0
    assert system._metrics["queue_dropped_assess"] == 1
    system.finish_turn(turn_id=tid, session_id="overload", assistant_response="not qualified")
    assert not system._turns
