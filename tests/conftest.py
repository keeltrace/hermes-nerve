"""Shared test isolation.

Every test runs with gate telemetry and preference storage redirected into a
per-test temp directory. Without this, tests that drive the gate directly
(e.g. test_core.GateTests, which predates the env-var overrides) appended
gate events to the LIVE ledger at ~/.hermes/nerve/gate-events.jsonl.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))


@pytest.fixture(autouse=True)
def _isolate_gate_storage(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_NERVE_GATE_EVENTS", str(tmp_path / "gate-events.jsonl"))
    monkeypatch.setenv("HERMES_NERVE_PREFERENCES", str(tmp_path / "preferences.json"))
    yield


@pytest.fixture(autouse=True)
def _nerve_p0_isolation():
    """P0 (0.4.1): isolate the advisory circuit breaker across tests.

    A simulated provider stall opens the breaker; without this fixture the
    open state leaks into unrelated tests within the same pytest process.
    """
    try:
        from hermes_nerve import advisory
        advisory.reset_provider_state()
    except Exception:
        pass
    yield
    try:
        advisory.reset_provider_state()
        advisory.reset_for_tests()
    except Exception:
        pass
