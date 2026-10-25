"""CLI <-> Control Panel equivalence (config-repair Phase 4).

`nerve setup --profile/--reset` and the Control Panel's preview/apply must
produce equivalent saved documents and equivalent resolved configurations.
Same-profile reselection preserves explicit overrides; reset clears them on
both surfaces.
"""
import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from hermes_nerve import cli, control_panel
from hermes_nerve.config_resolver import resolve_config
from hermes_nerve.control_panel import ControlPanel
from hermes_nerve.profiles import load_profile


def _panel(td):
    return ControlPanel(home=Path(td), source=Path(control_panel.__file__).resolve().parents[1])


class CliPanelEquivalenceTests(unittest.TestCase):
    def _panel_apply(self, td, request):
        panel = _panel(td)
        preview = panel.preview(request)
        applied_req = dict(request)
        applied_req["preview_id"] = preview["preview_id"]
        applied = panel.apply(applied_req)
        return preview, applied

    def _cli_save(self, td, profile, overrides=None, *, reset=False):
        with patch.dict(os.environ, {"HERMES_HOME": str(td)}, clear=False), \
             patch.object(cli.shared_context, "reconcile_enabled",
                          side_effect=lambda enabled: {"changed": False, "enabled": enabled}):
            cli._save(profile, overrides, reset=reset)

    def test_profile_selection_equivalent_saved_and_effective(self):
        with tempfile.TemporaryDirectory() as ta, tempfile.TemporaryDirectory() as tb:
            self._panel_apply(ta, {"profile": "fat_cat", "overrides": {},
                                   "advanced": {}, "clear_advanced": []})
            self._cli_save(tb, "fat_cat")
            a, b = load_profile(Path(ta)), load_profile(Path(tb))
            self.assertEqual(a, b)
            ra = resolve_config(home=Path(ta), profile=a)
            rb = resolve_config(home=Path(tb), profile=b)
            self.assertEqual(ra.modules, rb.modules)

    def test_individual_override_equivalent_both_directions(self):
        with tempfile.TemporaryDirectory() as ta, tempfile.TemporaryDirectory() as tb:
            # UI: toggle one feature off.
            self._panel_apply(ta, {"profile": "operator",
                                   "overrides": {"token_trajectory": False},
                                   "advanced": {}, "clear_advanced": []})
            # CLI: same toggle.
            self._cli_save(tb, "operator", {"token_trajectory": False})
            a, b = load_profile(Path(ta)), load_profile(Path(tb))
            self.assertEqual(a["nerve_modules"], b["nerve_modules"])
            self.assertEqual(a["nerve_modules"], {"token_trajectory": False})
            self.assertEqual(
                resolve_config(home=Path(ta), profile=a).modules,
                resolve_config(home=Path(tb), profile=b).modules,
            )

    def test_panel_reselect_preserves_overrides_until_reset(self):
        with tempfile.TemporaryDirectory() as td:
            self._panel_apply(td, {"profile": "lean",
                                   "overrides": {"context_governor": True},
                                   "advanced": {}, "clear_advanced": []})
            self._panel_apply(td, {"profile": "lean", "overrides": {},
                                   "advanced": {}, "clear_advanced": []})
            doc = load_profile(Path(td))
            self.assertEqual(doc["nerve_modules"], {"context_governor": True})
            self._panel_apply(td, {"profile": "lean", "overrides": {}, "reset": True,
                                   "advanced": {}, "clear_advanced": []})
            self.assertEqual(load_profile(Path(td))["nerve_modules"], {})

    def test_reset_equivalent_both_surfaces(self):
        with tempfile.TemporaryDirectory() as ta, tempfile.TemporaryDirectory() as tb:
            for td in (ta, tb):
                self._panel_apply(td, {"profile": "marie_kondo",
                                       "overrides": {"remote_workers": True},
                                       "advanced": {}, "clear_advanced": []})
            self._panel_apply(ta, {"profile": "marie_kondo", "overrides": {},
                                   "reset": True, "advanced": {}, "clear_advanced": []})
            self._cli_save(tb, "marie_kondo", reset=True)
            a, b = load_profile(Path(ta)), load_profile(Path(tb))
            self.assertEqual(a["nerve_modules"], {})
            self.assertEqual(a, b)

    def test_cli_show_reflects_panel_applied_state(self):
        with tempfile.TemporaryDirectory() as td:
            self._panel_apply(td, {"profile": "operator",
                                   "overrides": {"receipts": False},
                                   "advanced": {}, "clear_advanced": []})
            fake = resolve_config(home=Path(td), profile=load_profile(Path(td)))
            out = io.StringIO()
            with patch.dict(os.environ, {"HERMES_HOME": str(td)}, clear=False), \
                 patch.object(cli, "_current", return_value=fake), \
                 redirect_stdout(out):
                self.assertEqual(cli.main(["setup", "--show"]), 0)
            text = out.getvalue()
            self.assertIn("Configured: operator", text)
            self.assertIn("Effective:  operator", text)
            self.assertIn("Running:    verification unavailable", text)
            self.assertIn("Pending restart: unknown", text)
            self.assertIn("[OFF]", text)
            self.assertIn("[explicit override]", text)

    def test_panel_apply_writes_backup_and_reports_restart(self):
        with tempfile.TemporaryDirectory() as td:
            panel = _panel(td)
            _preview, applied = self._panel_apply(td, {"profile": "lean", "overrides": {},
                                                       "advanced": {}, "clear_advanced": []})
            self.assertTrue(applied["restart_required"])
            self.assertTrue(panel.snapshots(), "apply must snapshot the previous state")


if __name__ == "__main__":
    unittest.main()
