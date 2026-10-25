"""Regression for #60: a non-worker session id must not be written into
HERMES_KANBAN_TASK_ID, or later turns treat it as dispatcher-pinned truth."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hermes_nerve.work import runtime
from hermes_nerve.work import autobind
from hermes_nerve.work.autobind import ensure_kanban_binding

KEYS = ("HERMES_KANBAN_TASK_ID", "HERMES_KANBAN_TASK")


class AutobindEnvInjectionTests(unittest.TestCase):
    def _sup(self, td):
        runtime.configure(enabled=True, store_path=str(Path(td) / "work.db"), provider_decisions_enabled=False)
        return runtime.supervisor()

    def test_hook_local_session_id_is_not_written_to_env(self):
        clean = {k: v for k, v in os.environ.items() if k not in KEYS}
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, clean, clear=True), \
                patch.object(autobind, "_load_canonical_task", return_value=None):
            sup = self._sup(td)
            ensure_kanban_binding(sup, task_id="a75476196cf9", session_id="a75476196cf9")
            self.assertNotIn("HERMES_KANBAN_TASK_ID", os.environ)

    def test_dispatcher_pinned_task_is_still_propagated(self):
        clean = {k: v for k, v in os.environ.items() if k not in KEYS}
        clean["HERMES_KANBAN_TASK"] = "t_pinned"
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, clean, clear=True), \
                patch.object(autobind, "_load_canonical_task", return_value=None):
            sup = self._sup(td)
            ensure_kanban_binding(sup, task_id="session-xyz", session_id="session-xyz")
            self.assertEqual(os.environ.get("HERMES_KANBAN_TASK_ID"), "t_pinned")


if __name__ == "__main__":
    unittest.main()
