"""Identity resolver faults cannot grant canonical terminal authority."""
import sys
from types import ModuleType

from hermes_nerve.work import hooks


def test_dispatcher_identity_error_fails_closed(monkeypatch):
    monkeypatch.delenv("HERMES_DELEGATED_CHILD_CONTEXT", raising=False)
    helper = ModuleType("agent.delegation_context")
    def broken_identity():
        raise RuntimeError("identity service unavailable")
    helper.is_dispatcher_owned_worker_context = broken_identity
    package = ModuleType("agent")
    package.__path__ = []
    package.delegation_context = helper
    monkeypatch.setitem(sys.modules, "agent", package)
    monkeypatch.setitem(sys.modules, "agent.delegation_context", helper)
    assert hooks._owns_kanban_terminal_authority() is False
