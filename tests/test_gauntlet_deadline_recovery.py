"""Regression coverage for incomplete aggregate-deadline worker changes."""
import os
from pathlib import Path
import sys
import time
from hermes_nerve.work import deterministic as d


def test_non_hook_explicit_allowance_not_limited_by_hook_ceiling(tmp_path, monkeypatch):
    monkeypatch.setattr(d, "_MAX_SINGLE_COMMAND_S", 0.03)
    rc, _ = d._run([sys.executable, "-c", "import time; time.sleep(0.08)"], str(tmp_path), timeout=0.5)
    assert rc == 0


def test_nested_deadline_restores_outer_start(monkeypatch):
    original = d._hook_deadline_start.set(10.0)
    monkeypatch.setattr(d.time, "monotonic", lambda: 20.0)
    try:
        token = d._enter_hook_deadline()
        assert d._hook_deadline_start.get() == 10.0
        d._exit_hook_deadline(token)
        assert d._hook_deadline_start.get() == 10.0
    finally:
        d._hook_deadline_start.reset(original)


def _descendant_state(pid):
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except FileNotFoundError:
        # Successful process-group cleanup can reap the child at any instant.
        # Other errors must still fail this safety assertion, not imply death.
        return None


def test_descendant_state_exit_during_observation_is_dead():
    from unittest.mock import patch
    # Reproduce the exact exists()/read() race, not a timing-dependent rerun.
    with patch.object(Path, "exists", return_value=True), patch.object(
            Path, "read_text", side_effect=FileNotFoundError):
        assert _descendant_state(999997) is None


def test_descendant_state_keeps_live_and_permission_failures_visible():
    from unittest.mock import patch
    assert _descendant_state(os.getpid()) not in (None, "Z")
    with patch.object(Path, "exists", return_value=True), patch.object(
            Path, "read_text", side_effect=PermissionError):
        try:
            _descendant_state(999997)
        except PermissionError:
            pass
        else:
            raise AssertionError("Permission failure was mistaken for an exited child")


def test_timeout_does_not_wait_for_descendant_stdout(tmp_path):
    command = "import subprocess,sys,time; child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(1.2)']); print(child.pid,flush=True); time.sleep(1.2)"
    start = time.monotonic()
    rc, output = d._run([sys.executable, "-c", command], str(tmp_path), timeout=0.15)
    assert rc == 124
    assert time.monotonic() - start < 0.9
    child_pid = int(output.splitlines()[0])
    try:
        deadline = time.monotonic() + 2.0
        state = _descendant_state(child_pid)
        while state not in (None, "Z") and time.monotonic() < deadline:
            time.sleep(0.05)
            state = _descendant_state(child_pid)
        assert state in (None, "Z"), "Timed-out verification left an executing descendant"
    finally:
        try:
            os.kill(child_pid, 9)
        except ProcessLookupError:
            pass


def test_expired_budget_does_not_launch_subprocess(tmp_path, monkeypatch):
    monkeypatch.setattr(d, "_HOOK_VERIFICATION_BUDGET_S", 0.0)
    def forbidden_launch(*args, **kwargs):
        raise AssertionError("Expired budget launched a subprocess")
    monkeypatch.setattr(d.subprocess, "Popen", forbidden_launch)
    token = d._enter_hook_deadline()
    try:
        rc, output = d._run([sys.executable, "-c", "print('must not run')"], str(tmp_path))
        assert rc == 124
        assert "exhausted" in output
    finally:
        d._exit_hook_deadline(token)


def test_failed_command_never_becomes_success(tmp_path):
    rc, output = d._run([sys.executable, "-c", "print('failed'); raise SystemExit(3)"], str(tmp_path), timeout=0.5)
    assert rc == 3
    assert "failed" in output


def test_sequential_probes_share_hook_budget(tmp_path, monkeypatch):
    monkeypatch.setattr(d, "_HOOK_VERIFICATION_BUDGET_S", 0.6)
    start = time.monotonic()
    token = d._enter_hook_deadline()
    try:
        first, _ = d._run([sys.executable, "-c", "import time; time.sleep(0.2)"], str(tmp_path), timeout=1)
        second, _ = d._run([sys.executable, "-c", "import time; time.sleep(0.6)"], str(tmp_path), timeout=1)
        assert first == 0
        assert second == 124
        assert time.monotonic() - start < 1.2
    finally:
        # Compatible with the pre-repair no-argument cleanup for RED execution.
        if token is None:
            d._exit_hook_deadline()
        else:
            d._exit_hook_deadline(token)
