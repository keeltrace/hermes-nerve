"""Sensitive list-form search approvals must bind the actual requested roots."""
from hermes_nerve import gate, risk
import pytest


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setattr(risk, "_HOME", tmp_path)
    monkeypatch.setenv("TMPDIR", str(tmp_path / "no-tmp-root"))
    (tmp_path / ".ssh").mkdir()
    (tmp_path / ".aws").mkdir()


def test_list_search_assessment_captures_requested_paths(tmp_path):
    roots = [str(tmp_path / ".ssh"), str(tmp_path / ".aws")]
    args = {"pattern": "fixture", "paths": roots}
    assessment = risk.assess_tool_call("search_files", args)
    assert gate.bypass_reason("search_files", args) is None
    assert assessment.category == risk.CONSEQUENTIAL
    assert assessment.shape["paths"] == roots


def test_list_search_approval_identity_changes_with_requested_root(tmp_path):
    def identity(root):
        args = {"pattern": "fixture", "paths": [str(root)]}
        assessment = risk.assess_tool_call("search_files", args)
        assert assessment.category == risk.CONSEQUENTIAL
        return risk.effect_identity("search_files", args, assessment)
    first = identity(tmp_path / ".ssh")
    second = identity(tmp_path / ".aws")
    assert first != second, "Different sensitive roots shared an empty-path identity"
    assert first == identity(tmp_path / ".ssh")
