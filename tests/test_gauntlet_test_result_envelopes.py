"""A null error field is not evidence of test failure."""
import json
import pytest
from hermes_nerve.progress_integrity import test_succeeded as succeeded


def test_successful_terminal_envelope_is_recognized():
    result = json.dumps({"output": "33 passed in 0.25s", "exit_code": 0, "error": None})
    assert succeeded("ok", result)


@pytest.mark.parametrize("output,code,error", [
    ("33 passed in 0.25s", 1, None),
    ("33 passed in 0.25s", 0, "process interrupted"),
    ("", 0, None),
    ("2 failed, 31 passed", 0, None),
    ("33 passed in 0.25s", None, None),
    ("33 passed in 0.25s", False, None),
])
def test_unsuccessful_terminal_envelope_is_not_recognized(output, code, error):
    result = json.dumps({"output": output, "exit_code": code, "error": error})
    assert not succeeded("ok", result)
