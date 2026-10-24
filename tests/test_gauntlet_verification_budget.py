"""RED/GREEN regressions for NERVE verification budget (F3).

Pins that deterministic verification subprocesses are deadline-bounded by
Hermes's 30s hook callback budget and never report PASS on unfinished work.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hermes_nerve.work.deterministic import _HOOK_VERIFICATION_BUDGET_S, _run


def test_default_verification_budget_strictly_below_hook_timeout():
    assert 0 < _HOOK_VERIFICATION_BUDGET_S < 30.0


def test_non_hook_default_retains_standalone_verification_allowance():
    import inspect
    from hermes_nerve.work.deterministic import _NON_HOOK_VERIFICATION_BUDGET_S

    default = inspect.signature(_run).parameters["timeout"].default
    assert default == _NON_HOOK_VERIFICATION_BUDGET_S == 180.0


def test_slow_subprocess_is_bounded_and_not_pass(monkeypatch):
    from hermes_nerve.work import deterministic as d

    monkeypatch.setattr(d, "_HOOK_VERIFICATION_BUDGET_S", 0.05)
    started = time.monotonic()
    token = d._enter_hook_deadline()
    try:
        rc, output = _run([sys.executable, "-c", "import time; time.sleep(0.5)"], str(ROOT))
    finally:
        d._exit_hook_deadline(token)
    elapsed = time.monotonic() - started
    assert elapsed < 0.8, f"verification subprocess was not bounded: {elapsed:.1f}s"
    assert rc != 0, "unfinished work must never be recorded as PASS"
    assert rc == 124
    assert "verification budget" in output


def test_fast_subprocess_still_passes():
    rc, output = _run(["python3", "-c", "print('ok')"], str(ROOT))
    assert rc == 0
    assert "ok" in output


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v", "--tb=short"]))