"""#35 follow-up: the worker reaches _resolve_secret() under the QUEUED scope.

The maintainer's round-2 review asked for one regression beyond the generic
ContextVar propagation test: prove the Nerve worker resolves provider
secrets through agent.secret_scope.get_secret under the scope captured at
enqueue time — the precise failure chain reported in #35:

    Hermes turn (scope bound) -> queue -> bare worker (scope lost)
    -> engine_factory() -> JevClient.__init__ -> _resolve_secret()
    -> get_secret() -> UnscopedSecretError

A fake ``agent.secret_scope`` module is injected into sys.modules so the
unit stays independent of Hermes. Its get_secret fails closed (raises
UnscopedSecretError) when no scope is bound in the calling context, and
returns a stand-in key when the scope IS bound — mirroring documented
Hermes multiplexing behavior.
"""
from __future__ import annotations

import contextvars
import importlib
import sys
import types
import unittest
from unittest.mock import patch

from hermes_nerve import client as jev_client
from hermes_nerve.nervous import NervousSystem

FAKE_KEY = "sk-fake-provider-key"


def _install_fake_secret_scope():
    """Build and inject a fake agent.secret_scope package; return its parts."""
    agent_mod = types.ModuleType("agent")
    if not hasattr(agent_mod, "__path__"):
        agent_mod.__path__ = []  # mark as package
    scope_mod = types.ModuleType("agent.secret_scope")

    class UnscopedSecretError(RuntimeError):
        pass

    SCOPE: contextvars.ContextVar = contextvars.ContextVar("fake_profile_scope", default=None)

    def get_secret(name: str, default=None):
        if SCOPE.get() is None:
            raise UnscopedSecretError(f"no profile scope bound for secret {name!r}")
        return FAKE_KEY

    scope_mod.UnscopedSecretError = UnscopedSecretError
    scope_mod.get_secret = get_secret
    scope_mod._SCOPE = SCOPE
    return agent_mod, scope_mod


class TestResolveSecretUnderQueuedScope(unittest.TestCase):
    def setUp(self):
        self.agent_mod, self.scope_mod = _install_fake_secret_scope()
        self.SCOPE = self.scope_mod._SCOPE
        self._patcher = patch.dict(
            sys.modules, {"agent": self.agent_mod, "agent.secret_scope": self.scope_mod}
        )
        self._patcher.start()
        self.addCleanup(self._patcher.stop)
        importlib.reload(jev_client)
        self.addCleanup(lambda: importlib.reload(jev_client))

    def test_resolve_secret_fails_closed_without_scope(self):
        # Unbound context (what the bare worker used to run under): the
        # scoping error must propagate, NOT silently fall back to env.
        with self.assertRaises(self.scope_mod.UnscopedSecretError):
            jev_client._resolve_secret("OPENROUTER_API_KEY")

    def test_resolve_secret_succeeds_under_captured_context(self):
        # Simulate the enqueue-side capture: bind the scope in this thread,
        # copy the context, unbind, then resolve UNDER the copy.
        self.SCOPE.set("profile-A")
        captured = contextvars.copy_context()
        self.SCOPE.set(None)
        got = captured.run(jev_client._resolve_secret, "OPENROUTER_API_KEY")
        self.assertEqual(got, FAKE_KEY)

    def test_worker_resolves_secret_under_queued_scope(self):
        # The precise #35 chain: start_turn() registers the turn and enqueues
        # admission; the worker later builds the engine via factory, which
        # resolves the provider secret — it must run under the scope captured
        # at enqueue time, not the (unbound) worker-default context.
        resolved: dict[str, object] = {}

        def factory():
            # Mimics JevClient.__init__'s secret resolution.
            resolved["key"] = jev_client._resolve_secret("OPENROUTER_API_KEY")
            return object()

        ns = NervousSystem(engine_factory=factory)  # type: ignore[arg-type]
        ns.configure(enabled=True)  # starts the worker
        self.addCleanup(ns._stop_worker)

        self.SCOPE.set("profile-A")
        tid = ns.start_turn(user_message="hi", turn_id="t1")
        self.SCOPE.set(None)  # producer unbinds after enqueue
        ns._tasks.join()

        self.assertEqual(tid, "t1")
        self.assertEqual(
            resolved.get("key"),
            FAKE_KEY,
            "worker must reach _resolve_secret under the scope captured at enqueue",
        )


if __name__ == "__main__":
    unittest.main()
