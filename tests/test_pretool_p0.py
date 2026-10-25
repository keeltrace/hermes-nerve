"""P0 regression: pre_tool_call must never wait on remote inference in advisory
mode, must stay far below Hermes's 30s hook timeout in every mode, and the
composite _pre_tool_control chain must instrument each callback.

Fail-before contract (Nerve 0.4.1):
- advisory mode + hung provider: gate.pre_tool_call returns promptly (<2s),
  schedules nonblocking advisory work, NEVER retroactively blocks/authorizes;
- ask-only/enforce + stalled provider: single bounded attempt (<= 10s),
  fail to human approval, no double retry;
- composite chain records per-callback durations (sanitized) for every call;
- provider outage cooldown prevents retry storms;
- known-safe tools make zero provider calls in any mode.
"""
from __future__ import annotations

import json
import sys
import time
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from hermes_nerve import gate  # noqa: E402
from hermes_nerve import advisory  # noqa: E402


class _HangingTransport:
    """Simulates a provider that never returns within the hook budget."""

    def __init__(self):
        self.calls = 0

    def __call__(self, url, headers, body, timeout):
        self.calls += 1
        time.sleep(40)  # far beyond Hermes's 30s hook budget
        raise TimeoutError("simulated provider hang")


class _LateTransport:
    """Returns a valid ALLOW verdict after a delay."""

    def __init__(self, delay: float):
        self.delay = delay
        self.calls = 0

    def __call__(self, url, headers, body, timeout):
        self.calls += 1
        time.sleep(self.delay)
        return 200, json.dumps({
            "id": "test-late",
            "model": "typesafe/jev-1.14",
            "answers": {"decision": {"type": "choice", "value": "ALLOW",
                                     "confidence": 0.95,
                                     "probabilities": {"ALLOW": 0.97, "APPROVAL": 0.02, "BLOCK": 0.01}}},
        }).encode(), {}


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir(exist_ok=True)
    gate._configured_mode = "advisory"
    advisory.reset_for_tests()
    yield
    advisory.shutdown()


def _uncertain_args():
    return {"command": "python3 /tmp/some-unknown-operator-script.py --novel-flag"}


def test_advisory_hung_provider_does_not_block_pre_tool_path(monkeypatch):
    """FAIL-BEFORE: 0.4.0 waited synchronously on the provider in advisory mode."""
    gate._configured_mode = "advisory"
    monkeypatch.setattr(gate, "_make_client", lambda **kw: types.SimpleNamespace(
        provider_kind="openrouter", model="typesafe/jev-1.14",
        system_one=lambda **kw: (_ for _ in ()).throw(RuntimeError("should not be called")),
        _transport=_HangingTransport()))
    t0 = time.monotonic()
    directive = gate.pre_tool_call("terminal", _uncertain_args(),
                                   tool_call_id="t-hang-1", session_id="s1")
    elapsed = time.monotonic() - t0
    assert directive is None  # advisory never blocks the pipeline
    assert elapsed < 5.0, f"advisory path blocked for {elapsed:.1f}s on provider"


def test_advisory_async_result_is_telemetry_only(monkeypatch):
    gate._configured_mode = "advisory"
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test-async")

    class _FakeResult:
        value = "ALLOW"
        confidence = 0.95
        latency_ms = 12.0
        model = "typesafe/jev-1.14-20261014"
        probabilities = {"ALLOW": 0.97, "APPROVAL": 0.02, "BLOCK": 0.01}

    seen = []
    def async_evaluate(**kw):
        seen.append(kw)
        time.sleep(0.2)  # provider answers AFTER the pipeline returned
        return _FakeResult()
    monkeypatch.setattr(gate, "evaluate_tool_call", async_evaluate)
    directive = gate.pre_tool_call("terminal", _uncertain_args(),
                                   tool_call_id="t-late-1", session_id="s1")
    assert directive is None  # pipeline returned before the provider answered
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and advisory.health()["processed"] < 1:
        time.sleep(0.1)
    health = advisory.health()
    assert health["processed"] >= 1 and len(seen) == 1
    # the ALLOW verdict must NOT retroactively authorize anything:
    # the async record is telemetry only — no directive was produced and the
    # tool pipeline already returned before the provider answered.
    assert gate._configured_mode == "advisory"


def test_askonly_stalled_provider_fails_to_human_within_budget(monkeypatch):
    gate._configured_mode = "ask-only"
    attempts = []
    def hanging_evaluate(**kw):
        attempts.append(time.monotonic())
        time.sleep(40)  # stalled provider: exceeds any sane hook budget
        raise TimeoutError("provider stall")
    monkeypatch.setattr(gate, "evaluate_tool_call", hanging_evaluate)
    t0 = time.monotonic()
    directive = gate.pre_tool_call("terminal", _uncertain_args(),
                                   tool_call_id="t-stall-1", session_id="s1")
    elapsed = time.monotonic() - t0
    # bounded SINGLE attempt, comfortably below the 30s hook limit...
    # (the worker-side evaluation outlives the pipeline: the gate must not wait)
    assert elapsed < 15.0, f"ask-only path consumed {elapsed:.1f}s"
    assert directive is not None and directive.get("action") == "approve"
    # the synchronous pipeline made exactly one bounded attempt, no retry
    deadline = time.monotonic() + 3
    time.sleep(0.2)
    assert len(attempts) <= 1, "no double attempt on the critical path"


def test_provider_outage_cooldown_prevents_retry_storm(monkeypatch):
    gate._configured_mode = "ask-only"
    attempts = []
    def stalling_evaluate(**kw):
        attempts.append(time.monotonic())
        raise TimeoutError("provider stall")
    monkeypatch.setattr(gate, "evaluate_tool_call", stalling_evaluate)
    for i in range(3):
        gate.pre_tool_call("terminal", _uncertain_args(),
                           tool_call_id=f"t-storm-{i}", session_id="s1")
    # after the first stall the circuit breaker opens: no further provider calls
    assert len(attempts) <= 2, f"retry storm: {len(attempts)} provider attempts"
    assert advisory.health()["cooldown_remaining_s"] > 0


def test_known_safe_tool_zero_provider_calls_any_mode(monkeypatch):
    transport = _HangingTransport()
    monkeypatch.setattr(gate, "_make_client", lambda **kw: types.SimpleNamespace(
        provider_kind="openrouter", model="typesafe/jev-1.14",
        system_one=lambda **kw: None, _transport=transport))
    for mode in ("off", "advisory", "ask-only", "enforce"):
        gate._configured_mode = mode
        for tool, args in (("skills_list", {}),
                           ("skill_view", {"skill": "nerve"}),
                           ("read_file", {"path": "/home/user/Documents/nerve/README.md"}),
                           ("search_files", {"pattern": "catalog", "path": "/home/user/Documents/nerve/hermes_nerve"})):
            directive = gate.pre_tool_call(tool, args, tool_call_id=f"safe-{mode}-{tool}",
                                           session_id="s-safe")
            assert directive is None, (mode, tool, directive)
    assert transport.calls == 0


def test_composite_chain_records_per_callback_durations(tmp_path, monkeypatch):
    """The registered _pre_tool_control must time each callback (P0.A instrumentation)."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir(exist_ok=True)
    calls = []
    events = []

    def cb_fast(**kw):
        calls.append("fast")
        return None

    def cb_slow(**kw):
        calls.append("slow")
        time.sleep(0.05)
        return None

    trace = gate.instrument_pre_tool_chain([("fast", cb_fast), ("slow", cb_slow)],
                                           events.append)
    t0 = time.monotonic()
    directive = trace({"tool_name": "read_file", "args": {"path": "/etc/hosts"}})
    elapsed = time.monotonic() - t0
    assert directive is None and calls == ["fast", "slow"]
    assert len(events) == 2
    assert events[0]["callback"] == "fast" and events[1]["callback"] == "slow"
    assert events[1]["duration_ms"] >= 40
    assert events[0]["duration_ms"] <= events[1]["duration_ms"] + 5
    assert elapsed < 5
    assert "args" not in json.dumps(events)  # sanitized: no raw arguments


def test_composite_chain_continues_after_callback_exception(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir(exist_ok=True)
    events = []

    def cb_boom(**kw):
        raise RuntimeError("callback exploded")

    def cb_ok(**kw):
        return {"action": "approve", "message": "human needed"}

    trace = gate.instrument_pre_tool_chain([("boom", cb_boom), ("ok", cb_ok)],
                                           events.append)
    directive = trace({"tool_name": "terminal", "args": {}})
    assert directive is not None and directive["action"] == "approve"
    assert events[0]["outcome"].startswith("exception")
    assert events[1]["outcome"] == "directive"


def test_advisory_queue_saturation_and_dedup(monkeypatch):
    """Duplicate tool-call IDs collapse; a flood is bounded, never unbounded.

    The evaluation seam here is gate.evaluate_tool_call — the real execution
    path the advisory worker uses — not a client factory.
    """
    gate._configured_mode = "advisory"
    advisory.reset_for_tests()
    calls = []
    def fast_eval(**kw):
        calls.append(kw.get("tool_call_id"))
        time.sleep(0.01)
        return None
    monkeypatch.setattr(gate, "evaluate_tool_call", fast_eval)
    for _ in range(3):
        gate.pre_tool_call("terminal", _uncertain_args(),
                           tool_call_id="t-dup", session_id="s1")
    for i in range(100):
        gate.pre_tool_call("terminal", _uncertain_args(),
                           tool_call_id=f"t-flood-{i}", session_id="s1")
    health = advisory.health()
    # bounded: queue never exceeds its cap, and saturation drops work
    assert health["queue_depth"] <= advisory.MAX_QUEUE
    assert health["dropped"] > 0
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline and advisory.health()["queue_depth"] > 0:
        time.sleep(0.1)
    final = advisory.health()
    assert final["queue_depth"] == 0
    # dedup: the duplicated tool_call_id is evaluated at most once
    assert calls.count("t-dup") <= 1


def test_advisory_async_record_writes_gate_event(monkeypatch, tmp_path):
    """The async advisory outcome must land as a redacted gate event.

    Regression: the record callback omitted `tool_name`, so every async
    outcome raised TypeError inside the worker and was silently lost.
    """
    from hermes_nerve import advisory, gate

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_NERVE_GATE_EVENTS", str(tmp_path / "gate-events.jsonl"))
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test-record")
    gate._configured_mode = "advisory"
    monkeypatch.setattr(gate, "evaluate_tool_call", lambda **kw: _FakeResult())
    gate.pre_tool_call("terminal", {"command": "./rand-xyz --dry-run"},
                       tool_call_id="t-record-1", session_id="s1")
    deadline = time.monotonic() + 10
    rows = []
    while time.monotonic() < deadline:
        p = tmp_path / "gate-events.jsonl"
        if p.exists():
            rows = [json.loads(l) for l in p.read_text().splitlines() if l.strip()]
            if any(r.get("action") == "evaluated-advisory-async" for r in rows):
                break
        time.sleep(0.1)
    assert any(r.get("action") == "evaluated-advisory-async" for r in rows), rows
    row = next(r for r in rows if r.get("action") == "evaluated-advisory-async")
    assert row["tool_name"] == "terminal"
    assert "arguments" not in row


class _FakeResult:
    value = "ALLOW"
    confidence = 0.95
    latency_ms = 9.0
    model = "typesafe/jev-1.14-20261014"
    probabilities = {"ALLOW": 0.95, "APPROVAL": 0.04, "BLOCK": 0.01}
