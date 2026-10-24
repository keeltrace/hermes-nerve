"""Offline tests for canonical Hermes connection API compatibility."""
import contextlib
import sys
from types import ModuleType, SimpleNamespace

import pytest

from hermes_nerve.work.kanban_adapter import CanonicalKanbanAdapter


@pytest.mark.parametrize("operation", ["review", "complete"])
def test_split_connection_api_preserves_canonical_transitions(monkeypatch, operation):
    calls = []
    connection = object()
    @contextlib.contextmanager
    def connect_closing():
        calls.append("open")
        try:
            yield connection
        finally:
            calls.append("close")
    def transition(conn, task_id, **kwargs):
        assert conn is connection
        assert task_id == "test-task"
        assert kwargs["expected_run_id"] == 7
        if operation == "review":
            assert kwargs["force"] is False
            assert kwargs["with_reason"] is True
        calls.append("transition")
        return (True, None) if operation == "review" else True
    package = ModuleType("hermes_cli")
    package.__path__ = []
    split = ModuleType("hermes_cli.kanban_db_connect")
    split.connect_closing = connect_closing
    package.kanban_db_connect = split
    monkeypatch.setitem(sys.modules, "hermes_cli", package)
    monkeypatch.setitem(sys.modules, "hermes_cli.kanban_db_connect", split)
    adapter = CanonicalKanbanAdapter(SimpleNamespace(request_review=transition, complete_task=transition))
    if operation == "review":
        assert adapter.request_review(task_id="test-task", expected_run_id=7, summary="evidence", metadata={}) == (True, None)
    else:
        assert adapter.complete(task_id="test-task", expected_run_id=7) is True
    assert calls == ["open", "transition", "close"]


@pytest.mark.parametrize("operation", ["review", "complete"])
def test_transition_exception_closes_connection_and_never_claims_success(operation):
    events = []
    @contextlib.contextmanager
    def connect_closing():
        events.append("open")
        try:
            yield object()
        finally:
            events.append("close")
    def refuse(*args, **kwargs):
        raise RuntimeError("canonical transition rejected")
    adapter = CanonicalKanbanAdapter(SimpleNamespace(
        connect_closing=connect_closing, request_review=refuse, complete_task=refuse,
    ))
    with pytest.raises(RuntimeError, match="canonical transition rejected"):
        if operation == "review":
            adapter.request_review(task_id="test-task", expected_run_id=7, summary="evidence", metadata={})
        else:
            adapter.complete(task_id="test-task", expected_run_id=7)
    assert events == ["open", "close"]
