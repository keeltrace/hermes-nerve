"""Nerve 0.4.1 P0 lifecycle and callback-safety tests.

Proves the reliability contract that fixes the 30-second pre-tool hook
timeout: a hung provider can never occupy the advisory worker forever,
abandoned evaluations are bounded, repeated re-initialization does not
accumulate threads, and security-critical callback failures fail to the
native human approval boundary rather than permitting the call.
"""
from __future__ import annotations

import threading
import time

import pytest

from hermes_nerve import advisory, gate


def _threads(prefix: str) -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name.startswith(prefix)]


def _wait(cond, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.05)
    return False


@pytest.fixture(autouse=True)
def _clean_advisory():
    advisory.reset_for_tests()
    yield
    advisory.reset_for_tests()


def test_hung_provider_cannot_block_advisory_worker():
    """A provider that never returns is contained by the deadline."""
    advisory._ADVISORY_CALL_TIMEOUT = 0.4  # bounded for test speed
    try:
        release = threading.Event()
        try:
            advisory.submit(
                "terminal", {"arguments": {}},
                evaluate=lambda: release.wait(30),
                record=lambda evt: None,
                correlation={"tool_call_id": "tc-hang-1"})
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and advisory.health()["stalls"] < 1:
                time.sleep(0.05)
            health = advisory.health()
            assert health["stalls"] >= 1, "the hang was not detected as a stall"
            assert health["worker_alive"], "the worker died instead of surviving the hang"
            assert health["abandoned_live"] <= advisory.MAX_ABANDONED
        finally:
            release.set()
            _wait(lambda: advisory.health()["abandoned_live"] == 0, 10.0)
    finally:
        advisory._ADVISORY_CALL_TIMEOUT = 8.0


def test_pre_tool_pipeline_stays_responsive_during_provider_hang():
    """Consecutive pipeline calls stay fast while an advisory eval is hung."""
    advisory._ADVISORY_CALL_TIMEOUT = 0.4
    release = threading.Event()
    try:
        advisory.submit("terminal", {}, evaluate=lambda: release.wait(30),
                        record=lambda evt: None,
                        correlation={"tool_call_id": "tc-hang-2"})
        for i in range(5):
            t0 = time.monotonic()
            advisory.submit("terminal", {}, evaluate=lambda: release.wait(30),
                            record=lambda evt: None,
                            correlation={"tool_call_id": f"tc-fast-{i}"})
            assert time.monotonic() - t0 < 0.5, "submission blocked on a hung evaluation"
        assert len(_threads("nerve-advisory-eval-")) <= advisory.MAX_ABANDONED + 1
    finally:
        release.set()
        advisory._ADVISORY_CALL_TIMEOUT = 8.0


def test_bounded_abandonment_refuses_instead_of_spawning_unlimited_threads():
    advisory._ADVISORY_CALL_TIMEOUT = 0.2
    advisory.MAX_ABANDONED = 2
    release = threading.Event()
    try:
        for i in range(8):
            advisory.submit("terminal", {}, evaluate=lambda: release.wait(30),
                            record=lambda evt: None,
                            correlation={"tool_call_id": f"tc-bound-{i}"})
        time.sleep(3.0)
        spawned = advisory.health()["eval_threads_spawned"]
        live = len(_threads("nerve-advisory-eval-"))
        assert live <= advisory.MAX_ABANDONED + 1, f"thread growth: {live} live"
        assert spawned <= advisory.MAX_ABANDONED + 2, f"unlimited replacement: {spawned} spawned"
        assert advisory.health()["refused"] >= 1
    finally:
        release.set()
        _wait(lambda: advisory.health()["abandoned_live"] == 0, 10.0)
        advisory._ADVISORY_CALL_TIMEOUT = 8.0
        advisory.MAX_ABANDONED = 2


def test_repeated_reset_does_not_accumulate_workers():
    for _ in range(25):
        advisory.reset_for_tests()
    workers = [t for t in threading.enumerate() if t.name == "nerve-advisory"]
    assert len(workers) <= 1, f"worker accumulation: {len(workers)} threads"


def test_session_shutdown_is_bounded_and_non_interfering():
    advisory._ADVISORY_CALL_TIMEOUT = 5.0
    try:
        for i in range(3):
            advisory.submit("terminal", {}, evaluate=lambda: time.sleep(0.05),
                            record=lambda evt: None,
                            correlation={"tool_call_id": f"tc-shut-{i}"})
        t0 = time.monotonic()
        advisory.shutdown(timeout=2.0)
        elapsed = time.monotonic() - t0
        assert elapsed < 3.0, f"shutdown blocked for {elapsed:.1f}s"
        assert not advisory.health()["worker_alive"]
    finally:
        advisory.reset_for_tests()


def test_advisory_recovery_after_stall():
    """After the cooldown is cleared, advisory work resumes."""
    advisory._ADVISORY_CALL_TIMEOUT = 0.3
    assert _wait(lambda: advisory.health()["abandoned_live"] == 0, 15.0)
    advisory.reset_provider_state()
    try:
        advisory.submit("terminal", {}, evaluate=lambda: time.sleep(5),
                        record=lambda evt: None,
                        correlation={"tool_call_id": "tc-stall-x"})
        time.sleep(1.2)
        assert advisory.health()["stalls"] >= 1
        advisory.reset_provider_state()  # cooldown expires
        # backpressure is deliberate: wait for prior abandoned evals to finish
        assert _wait(lambda: advisory.health()["abandoned_live"] == 0, 8.0)
        seen = []
        advisory.submit("terminal", {}, evaluate=lambda: seen.append(1) or "ok",
                        record=lambda evt: None,
                        correlation={"tool_call_id": "tc-recover"})
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline and not seen:
            time.sleep(0.05)
        assert seen, "advisory processing did not recover after the stall"
        assert advisory.health()["processed"] >= 1
    finally:
        advisory._ADVISORY_CALL_TIMEOUT = 8.0


# --- callback failure semantics -------------------------------------------------

def _trace(callbacks):
    events = []
    return gate.instrument_pre_tool_chain(callbacks, events.append), events


def test_noncritical_callback_exception_continues():
    def boom(**kw):
        raise RuntimeError("remote control down")

    def ok(**kw):
        return {"action": "approve", "message": "native approval"}

    chain, events = _trace([("remote_control", boom), ("work_supervision", ok)])
    directive = chain({"tool_name": "terminal"})
    assert directive == {"action": "approve", "message": "native approval"}
    assert events[0]["outcome"].startswith("exception") and events[0]["critical"] is False


def test_critical_callback_exception_fails_to_human_approval():
    def boom(**kw):
        raise RuntimeError("gate crashed")

    chain, events = _trace([("action_gate", boom, True)])
    directive = chain({"tool_name": "terminal", "tool_call_id": "tc-crit-1"})
    assert directive is not None
    assert directive["action"] == "approve", "a crashed gate must never silently allow"
    assert "action gate unavailable" in directive["message"]
    # 0.4.2: the failure key is scoped to the call and non-reusable, so a
    # session/permanent grant for one gate failure can never authorize another.
    key = directive["rule_key"]
    assert key.startswith("nerve:hitl:gate-failure:")
    assert len(key.rsplit(":", 1)[-1]) >= 8
    assert events[0]["outcome"].startswith("exception") and events[0]["critical"] is True
    # arguments/secrets are never recorded
    assert "arguments" not in events[0]


def test_critical_failure_after_directive_preserves_earlier_directive():
    def deny(**kw):
        return {"action": "block", "message": "prohibited"}

    def boom(**kw):
        raise RuntimeError("gate crashed")

    chain, _ = _trace([("nervous", deny), ("action_gate", boom, True)])
    directive = chain({"tool_name": "terminal"})
    assert directive is not None and directive["action"] == "block"


def test_telemetry_sink_failure_never_blocks_tools():
    def boom_sink(record):
        raise OSError("disk full")

    chain = gate.instrument_pre_tool_chain([("remote_control", lambda **kw: None)], boom_sink)
    assert chain({"tool_name": "read_file"}) is None


def test_critical_callback_success_is_unaffected():
    chain, events = _trace([("action_gate", lambda **kw: None, True)])
    assert chain({"tool_name": "read_file"}) is None
    assert events[0]["outcome"] == "none"


def test_request_scoped_timeout_does_not_contaminate_other_contexts():
    from hermes_nerve import client

    baseline = client.scoped_timeout()
    with client.timeout_scope(3.0):
        assert client.scoped_timeout() == 3.0
    assert client.scoped_timeout() == baseline
    # a fresh thread has its own (clean) scope
    seen = []
    t = threading.Thread(target=lambda: seen.append(client.scoped_timeout()))
    t.start()
    t.join()
    assert seen == [None]


# --- real registration wiring ---------------------------------------------------

def _load_plugin():
    import importlib.util
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "hermes_nerve_plugin_reg", root / "__init__.py",
        submodule_search_locations=[str(root)])
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


class _Ctx:
    def __init__(self, extra=None):
        self.tools, self.hooks, self.engine = {}, [], None
        self.extra = dict(extra or {})

    def get_config(self, key, default=None):
        return self.extra.get(key, default)

    def register_tool(self, *, name, schema=None, handler=None, **kw):
        self.tools[name] = (schema, handler)

    def register_hook(self, name, callback):
        self.hooks.append((name, callback))

    def register_context_engine(self, engine):
        self.engine = engine


def test_profile_registration_wires_instrumented_pre_tool_chain(tmp_path, monkeypatch):
    """Regression: a function-local trace sink broke modular registration.

    _register_profile referenced a helper defined inside _register_legacy and
    raised NameError, so pre-tool hook registration silently aborted (no trace,
    no gate). The sink is module-level now; this asserts the whole chain wired.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    # This exercises an ordinary operator session, not a dispatcher worker.
    # A surrounding Hermes test worker may export either task marker.
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_TASK_ID", raising=False)
    mod = _load_plugin()
    ctx = _Ctx({"nerve_profile": "operator"})
    mod.register(ctx)
    names = [n for n, _ in ctx.hooks]
    assert "pre_tool_call" in names, "pre-tool hook was not registered"
    pre_tool = next(cb for name, cb in ctx.hooks if name == "pre_tool_call")
    pre_tool(tool_name="read_file", args={"path": "/tmp/x"}, tool_call_id="tc-reg-1",
             session_id="s-reg")
    trace = tmp_path / "nerve" / "hook-trace.jsonl"
    assert trace.exists(), "instrumented chain wrote no trace"
    import json
    rows = [json.loads(line) for line in trace.read_text().splitlines() if line.strip()]
    assert rows and rows[0]["callback"], rows
    assert all("arguments" not in r for r in rows)
    assert max(r["duration_ms"] for r in rows) < 30_000


def test_legacy_registration_wires_instrumented_pre_tool_chain(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_TASK_ID", raising=False)
    mod = _load_plugin()
    ctx = _Ctx()
    mod.register(ctx)
    names = [n for n, _ in ctx.hooks]
    assert "pre_tool_call" in names
    pre_tool = next(cb for name, cb in ctx.hooks if name == "pre_tool_call")
    pre_tool(tool_name="read_file", args={"path": "/tmp/x"}, tool_call_id="tc-reg-2")
    trace = tmp_path / "nerve" / "hook-trace.jsonl"
    assert trace.exists()
    callbacks = {__import__("json").loads(line)["callback"]
                 for line in trace.read_text().splitlines() if line.strip()}
    assert {"remote_control", "work_supervision"} <= callbacks
