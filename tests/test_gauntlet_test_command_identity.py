"""Real bounded venv test invocations must count without accepting lookalikes."""
import pytest
from hermes_nerve.progress_integrity import tool_kind


@pytest.mark.parametrize("command", [
    "/work/.venv/bin/python -m pytest tests/test_core.py -q",
    "timeout 20 /work/.venv/bin/python -m pytest tests/test_core.py -q",
    "timeout --signal=TERM 20s /work/.venv/bin/python3 -m unittest tests.test_core",
    "/work/.venv/bin/pytest tests/test_core.py -q",
    "timeout -k 2s 20s /work/.venv/bin/python3.11 -m pytest tests/test_core.py",
])
def test_real_venv_test_invocations_are_classified(command):
    assert tool_kind("terminal", {"command": command}) == "test"


@pytest.mark.parametrize("command", [
    "echo /work/.venv/bin/python -m pytest tests/test_core.py",
    "/work/bin/fake-python -m pytest tests/test_core.py",
    "timeout invalid /work/.venv/bin/python -m pytest tests/test_core.py",
    "/work/.venv/bin/python -c 'print(\"pytest 202 passed\")'",
    "/work/.venv/bin/python -m pytest_not_real tests/test_core.py",
])
def test_test_command_lookalikes_do_not_count(command):
    assert tool_kind("terminal", {"command": command}) != "test"
