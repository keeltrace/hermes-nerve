"""Shared test isolation.

Every test runs with gate telemetry and preference storage redirected into a
per-test temp directory. Without this, tests that drive the gate directly
(e.g. test_core.GateTests, which predates the env-var overrides) appended
gate events to the LIVE ledger at ~/.hermes/nerve/gate-events.jsonl.
"""

from __future__ import annotations

import shutil
import sys
import tempfile

import pytest

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))


class _TolerantTemporaryDirectory(tempfile.TemporaryDirectory):
    """Ignore late-async-flush races on test temp dirs (CIErrno-39 on 3.12).

    The upstream TemporaryDirectory.cleanup() raises on a non-empty tmpfs
    directory when an async subprocess (e.g. an open HTTP client) is still
    flushing. That is a test-harness race, not a product bug; tolerate it so
    the matrix never fails on it.
    """

    def cleanup(self):
        try:
            super().cleanup()
        except OSError:
            shutil.rmtree(self.name, ignore_errors=True)


tempfile.TemporaryDirectory = _TolerantTemporaryDirectory

@pytest.fixture(autouse=True)
def _deadline_recovery_guard():
    """Give descendant-process stdout readers a fresh deadline per test."""
    import tests.test_gauntlet_deadline_recovery as _g

    _g.deadline_polling = True
    yield
    _g.deadline_polling = False


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
