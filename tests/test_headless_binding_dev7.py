from __future__ import annotations

import contextlib
import importlib.util
import os
import sqlite3
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from hermes_nerve.work import runtime
from hermes_nerve.work.autobind import ensure_kanban_binding

ROOT = Path(__file__).resolve().parents[1]


class Task:
    id = "t_dev7"
    title = "Repair service"
    body = "## Definition of Done\n1. `python3 -m pytest tests/ -q` exits 0.\n2. Git status is clean.\n"
    current_run_id = 42
    claim_lock = "claim-42"


class Con:
    def close(self):
        pass


class FakeCtx:
    def __init__(self, home: Path):
        self.home = home
        self.tools = {}
        self.hooks = []
        self.engine = None

    def get_config(self, key, default=None):
        return {
            "work_supervision_db": str(self.home / "work.db"),
            "remote_data_dir": str(self.home / "remote"),
        }.get(key, default)

    def register_tool(self, *, name, schema=None, handler=None, **kwargs):
        self.tools[name] = (schema, handler)

    def register_hook(self, name, callback):
        self.hooks.append((name, callback))

    def register_context_engine(self, engine):
        self.engine = engine


def split_hermes_modules(captured: dict):
    kb = types.ModuleType("hermes_cli.kanban_db")
    kb.get_task = lambda con, tid: Task() if tid == Task.id else None

    kbc = types.ModuleType("hermes_cli.kanban_db_connect")

    @contextlib.contextmanager
    def connect_closing(db_path=None, *, board=None):
        captured["db_path"] = str(db_path) if db_path is not None else None
        captured["board"] = board
        yield Con()

    kbc.connect_closing = connect_closing
    pkg = types.ModuleType("hermes_cli")
    pkg.kanban_db = kb
    pkg.kanban_db_connect = kbc
    return pkg, kb, kbc


class Dev7HeadlessBindingTests(unittest.TestCase):
    def setUp(self):
        runtime.set_supervisor_for_tests(None, enabled_value=False)
        # Ambient dispatcher identity must not override each test's explicit task.
        env = patch.dict(os.environ, {"HERMES_KANBAN_TASK_ID": ""}, clear=False)
        env.start()
        self.addCleanup(env.stop)

    def test_split_kanban_api_binds_using_dispatcher_pinned_db(self):
        captured = {}
        pkg, kb, kbc = split_hermes_modules(captured)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            runtime.configure(enabled=True, store_path=str(root / "work.db"), provider_decisions_enabled=False)
            sup = runtime.supervisor()
            env = {
                "HERMES_KANBAN_TASK": Task.id,
                "HERMES_KANBAN_RUN_ID": "42",
                "HERMES_KANBAN_CLAIM_LOCK": "claim-42",
                "HERMES_KANBAN_DB": str(root / "board.db"),
                "HERMES_KANBAN_BOARD": "should-not-win",
                "HERMES_KANBAN_WORKSPACE": td,
                "HERMES_PROFILE": "abtest-jev-v7",
            }
            with patch.dict(sys.modules, {
                "hermes_cli": pkg,
                "hermes_cli.kanban_db": kb,
                "hermes_cli.kanban_db_connect": kbc,
            }), patch.dict(os.environ, env, clear=False):
                ident = ensure_kanban_binding(sup, session_id="session-1")

            self.assertIsNotNone(ident)
            self.assertEqual(ident.run_id, 42)
            self.assertEqual(ident.claim_identity, "claim-42")
            self.assertEqual(captured["db_path"], str(root / "board.db"))
            self.assertIsNone(captured["board"])
            self.assertIsNotNone(sup.active_contract(Task.id))
            self.assertEqual(sup.store.current_identity(Task.id), ident)
            self.assertEqual(sup.store.run_context(ident)["session_id"], "session-1")

    def test_plugin_register_eagerly_binds_before_first_model_hook(self):
        captured = {}
        pkg, kb, kbc = split_hermes_modules(captured)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            env = {
                # Isolate profile resolution from the real ~/.hermes profile.
                "HERMES_HOME": td,
                "HERMES_KANBAN_TASK": Task.id,
                "HERMES_KANBAN_RUN_ID": "42",
                "HERMES_KANBAN_CLAIM_LOCK": "claim-42",
                "HERMES_KANBAN_DB": str(root / "board.db"),
                "HERMES_KANBAN_WORKSPACE": td,
                "HERMES_PROFILE": "abtest-jev-v7",
            }
            with patch.dict(sys.modules, {
                "hermes_cli": pkg,
                "hermes_cli.kanban_db": kb,
                "hermes_cli.kanban_db_connect": kbc,
            }), patch.dict(os.environ, env, clear=False):
                spec = importlib.util.spec_from_file_location(
                    "hermes_nerve_plugin_dev7",
                    ROOT / "__init__.py",
                    submodule_search_locations=[str(ROOT)],
                )
                mod = importlib.util.module_from_spec(spec)
                sys.modules[spec.name] = mod
                spec.loader.exec_module(mod)
                ctx = FakeCtx(root)
                mod.register(ctx)

                # Registration itself is the bootstrap boundary: no hook needs
                # to fire and the worker pays no model turn for the binding.
                with contextlib.closing(sqlite3.connect(root / "work.db")) as con:
                    self.assertEqual(con.execute("SELECT count(*) FROM active_contracts").fetchone()[0], 1)
                    self.assertEqual(con.execute("SELECT count(*) FROM run_bindings").fetchone()[0], 1)
                    self.assertEqual(con.execute("SELECT count(*) FROM run_context").fetchone()[0], 1)
                    kinds = [r[0] for r in con.execute("SELECT kind FROM supervision_diagnostics ORDER BY seq")]
                self.assertIn("autobind_startup_bound", kinds)
                self.assertTrue(os.environ.get("HERMES_NERVE_DOD_HASH"))
                self.assertEqual(ctx.tools, {})

    def test_fallback_binding_prefers_dispatcher_env_over_hook_task_id(self):
        captured = {}
        pkg, kb, kbc = split_hermes_modules(captured)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            runtime.configure(enabled=True, store_path=str(root / "work.db"), provider_decisions_enabled=False)
            sup = runtime.supervisor()
            env = {
                "HERMES_KANBAN_TASK": Task.id,
                "HERMES_KANBAN_RUN_ID": "42",
                "HERMES_KANBAN_CLAIM_LOCK": "claim-42",
                "HERMES_KANBAN_DB": str(root / "board.db"),
                "HERMES_KANBAN_WORKSPACE": td,
            }
            with patch.dict(sys.modules, {
                "hermes_cli": pkg,
                "hermes_cli.kanban_db": kb,
                "hermes_cli.kanban_db_connect": kbc,
            }), patch.dict(os.environ, env, clear=False):
                ident = ensure_kanban_binding(sup, task_id="20260921_071903_child_session", session_id="child")
            self.assertIsNotNone(ident)
            self.assertEqual(ident.task_id, Task.id)
            self.assertEqual(sup.store.current_identity(Task.id), ident)
            with sup.store.read() as con:
                missing = con.execute(
                    "SELECT count(*) FROM supervision_diagnostics WHERE kind='autobind_task_missing'"
                ).fetchone()[0]
            self.assertEqual(missing, 0)

    def test_canonical_claim_mismatch_fails_closed(self):
        captured = {}
        pkg, kb, kbc = split_hermes_modules(captured)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            runtime.configure(enabled=True, store_path=str(root / "work.db"), provider_decisions_enabled=False)
            sup = runtime.supervisor()
            env = {
                "HERMES_KANBAN_TASK": Task.id,
                "HERMES_KANBAN_RUN_ID": "42",
                "HERMES_KANBAN_CLAIM_LOCK": "stale-claim",
                "HERMES_KANBAN_DB": str(root / "board.db"),
            }
            with patch.dict(sys.modules, {
                "hermes_cli": pkg,
                "hermes_cli.kanban_db": kb,
                "hermes_cli.kanban_db_connect": kbc,
            }), patch.dict(os.environ, env, clear=False):
                ident = ensure_kanban_binding(sup)
            self.assertIsNone(ident)
            self.assertIsNone(sup.store.current_identity(Task.id))
            self.assertIsNone(sup.active_contract(Task.id))
            with sup.store.read() as con:
                kinds = [r[0] for r in con.execute("SELECT kind FROM supervision_diagnostics ORDER BY seq")]
            self.assertIn("autobind_claim_mismatch", kinds)


if __name__ == "__main__":
    unittest.main()
