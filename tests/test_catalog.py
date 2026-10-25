"""Nerve 0.4.1 P1: operation-aware catalog security regressions.

The catalog must classify effects, not names: a namespace prefix is never
sufficient to auto-allow, mutations require real approval, and unknown
operations stay UNCERTAIN instead of falling through to execution.
"""
from __future__ import annotations

import pytest

from hermes_nerve import catalog, gate, risk


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    yield


def test_read_only_operations_are_recognized():
    for tool, args in [
        ("skills_list", {}),
        ("skill_view", {"name": "digital-assistant"}),
        ("tool_search", {"queries": ["x"]}),
        ("nerve_stats", {}),
        ("nerve_work_status", {}),
        ("nerve_context_rehydrate", {}),
        ("mnemosyne_recall", {"query": "x"}),
        ("read_file", {"path": "/home/user/Documents/nerve/README.md"}),
        ("search_files", {"pattern": "x"}),
    ]:
        assert risk.assess_tool_call(tool, args).category == risk.SAFE, tool
        assert gate.bypass_reason(tool, args) == "catalog-read-only", tool


def test_no_blanket_namespace_bypass():
    """A Nerve-namespaced mutation must not ride a prefix allowance."""
    for tool, args in [
        ("nerve_remote_delegate_task", {"task": "do work"}),
        ("nerve_remote_worker_control", {"worker_id": "w1", "action": "kill"}),
        ("nerve_context_curate", {"budget": 10}),
    ]:
        assert gate.bypass_reason(tool, args) is None, tool
        assert risk.assess_tool_call(tool, args).category != risk.SAFE, tool


def test_skill_manage_is_operation_aware():
    delete = risk.assess_tool_call("skill_manage", {"action": "delete", "name": "x"})
    assert delete.category == risk.CONSEQUENTIAL
    patch = risk.assess_tool_call("skill_manage", {"action": "patch", "name": "x"})
    assert patch.category == risk.PROJECT
    view = risk.assess_tool_call("skill_manage", {"action": "view", "name": "x"})
    assert view.category == risk.SAFE
    unknown = risk.assess_tool_call("skill_manage", {"action": "frobnicate"})
    assert unknown.category == risk.UNCERTAIN
    assert gate.bypass_reason("skill_manage", {"action": "delete", "name": "x"}) is None


def test_deferred_dispatch_is_never_auto_allowed():
    assert risk.assess_tool_call("tool_call", {"name": "anything"}).category == risk.UNCERTAIN
    assert gate.bypass_reason("tool_call", {"name": "anything"}) is None


def test_mcp_operations_classified_by_verb():
    assert risk.assess_tool_call("mcp__github__list_issues", {}).category == risk.SAFE
    assert risk.assess_tool_call("mcp__github__create_issue", {}).category == risk.CONSEQUENTIAL
    assert risk.assess_tool_call("mcp__files__read", {"path": "x"}).category == risk.SAFE
    assert risk.assess_tool_call("mcp__x__frobnicate", {}).category == risk.UNCERTAIN
    assert gate.bypass_reason("mcp__github__create_issue", {}) is None


def test_sensitive_read_loses_the_read_bypass(tmp_path):
    target = tmp_path / ".ssh" / "id_rsa"
    target.parent.mkdir(parents=True)
    target.write_text("PRIVATE")
    assert gate.bypass_reason("read_file", {"path": str(target)}) is None
    assert risk.assess_tool_call("read_file", {"path": str(target)}).category == risk.CONSEQUENTIAL


def test_writes_outside_project_are_consequential(tmp_path):
    inside = tmp_path / "proj" / "file.txt"
    inside.parent.mkdir()
    outside = "/etc/nerve-test-file"
    assert risk.assess_tool_call("write_file", {"path": str(inside)}).category == risk.PROJECT
    assert risk.assess_tool_call("write_file", {"path": outside}).category == risk.CONSEQUENTIAL


def test_memory_and_cron_operations():
    assert risk.assess_tool_call("memory", {"action": "read"}).category == risk.SAFE
    assert risk.assess_tool_call("memory", {"action": "add", "content": "x"}).category == risk.PROJECT
    assert risk.assess_tool_call("cronjob", {"action": "list"}).category == risk.SAFE
    assert risk.assess_tool_call("cronjob", {"action": "create"}).category == risk.CONSEQUENTIAL


def test_catalog_audit_reports_unclassified():
    audit = catalog.inventory_audit([
        "skills_list", "skill_manage", "nerve_stats", "terminal",
        "mcp__github__create_issue", "some_unknown_tool",
    ])
    assert audit["total"] == 6
    assert "some_unknown_tool" in audit["unclassified"]
    assert 0.0 < audit["coverage"] < 1.0


def test_catalog_has_no_unbounded_exec_allowance():
    """No cataloged operation may be SAFE while also executing or writing."""
    bad = [(tool, op.name) for tool, ops in catalog.TOOLS.items()
           for op in ops if op.category == risk.SAFE and op.effect in (catalog.EXEC, catalog.WRITE)]
    assert not bad, bad


def test_unknown_tool_remains_uncertain():
    assert risk.assess_tool_call("brand_new_tool", {"x": 1}).category == risk.UNCERTAIN
    assert gate.bypass_reason("brand_new_tool", {"x": 1}) is None
