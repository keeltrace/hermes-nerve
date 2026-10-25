"""Sidecar authority + conflict surfacing (0.4.12 config-repair follow-up).

The Nerve sidecar document is the store both supported writers maintain
(``nerve setup`` and the Control Panel). When it names a profile it must win
over a differing Hermes ``config.yaml`` ``nerve_profile``, the ignored setting
must be reported, and every saved module override must reach the effective
state — a toggle the operator switched on may never silently resolve off.
"""
from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from hermes_nerve.cli import _show
from hermes_nerve.config_resolver import resolve_config
from hermes_nerve.profiles import load_profile, save_profile


def _settings(profile="", modules=None):
    return {"nerve_profile": profile, "nerve_modules": json.dumps(modules) if modules else ""}


class SidecarAuthorityTests(unittest.TestCase):
    def test_sidecar_wins_over_differing_settings_profile(self):
        doc = {"version": 1, "nerve_profile": "custom",
               "nerve_modules": {"remote_workers": True, "work_supervision": False}, "advanced": {}}
        resolved = resolve_config(get_config=lambda k, d=None: _settings("operator").get(k, d), profile=doc)
        self.assertEqual(resolved.profile, "custom")
        self.assertTrue(resolved.enabled("remote_workers"))
        self.assertFalse(resolved.enabled("work_supervision"))
        self.assertEqual(resolved.ignored_settings_profile, "operator")
        self.assertEqual(resolved.reasons["remote_workers"], "explicit override")

    def test_matching_settings_profile_reports_no_conflict(self):
        doc = {"version": 1, "nerve_profile": "operator", "nerve_modules": {}, "advanced": {}}
        resolved = resolve_config(get_config=lambda k, d=None: _settings("operator").get(k, d), profile=doc)
        self.assertEqual(resolved.profile, "operator")
        self.assertEqual(resolved.ignored_settings_profile, "")

    def test_settings_profile_applies_when_no_sidecar(self):
        import tempfile
        with tempfile.TemporaryDirectory() as empty_home:
            resolved = resolve_config(
                get_config=lambda k, d=None: _settings("lean").get(k, d),
                profile=None, home=empty_home)
            self.assertEqual(resolved.profile, "lean")
            self.assertEqual(resolved.ignored_settings_profile, "")

    def test_external_settings_modules_still_apply_over_sidecar(self):
        doc = {"version": 1, "nerve_profile": "custom", "nerve_modules": {"receipts": True}, "advanced": {}}
        resolved = resolve_config(
            get_config=lambda k, d=None: _settings("custom", {"shared_context": True}).get(k, d), profile=doc)
        self.assertTrue(resolved.enabled("receipts"))
        self.assertTrue(resolved.enabled("shared_context"))
        self.assertEqual(resolved.reasons["shared_context"], "explicit override")

    def test_saved_override_never_silently_resolves_off(self):
        """Regression: the operator's 'Remote workers' toggle showed on while the
        runtime resolved it off because config.yaml carried a stale profile."""
        doc = {"version": 1, "nerve_profile": "custom",
               "nerve_modules": {"remote_workers": True}, "advanced": {}}
        resolved = resolve_config(get_config=lambda k, d=None: _settings("operator").get(k, d), profile=doc)
        self.assertTrue(resolved.enabled("remote_workers"))


class SidecarAuthorityFileTests(unittest.TestCase):
    def test_resolver_reads_sidecar_from_home(self):
        with tempfile.TemporaryDirectory() as td:
            save_profile({"version": 1, "nerve_profile": "fat_cat", "nerve_modules": {}, "advanced": {}}, Path(td))
            resolved = resolve_config(get_config=lambda k, d=None: _settings("operator").get(k, d), home=Path(td))
            self.assertEqual(resolved.profile, "fat_cat")
            self.assertEqual(resolved.ignored_settings_profile, "operator")
            self.assertEqual(load_profile(Path(td))["nerve_profile"], "fat_cat")


class ConflictSurfacingTests(unittest.TestCase):
    def test_cli_show_prints_conflict_note(self):
        doc = {"version": 1, "nerve_profile": "custom", "nerve_modules": {"remote_workers": True}, "advanced": {}}
        resolved = resolve_config(get_config=lambda k, d=None: _settings("operator").get(k, d), profile=doc)
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            _show(resolved)
        out = buffer.getvalue()
        self.assertIn("takes precedence", out)
        self.assertIn("operator", out)

    def test_cli_resolves_against_hermes_config_yaml(self):
        """Regression: nerve setup --show resolved without Hermes config.yaml, so
        advanced settings the gateway honors were invisible to the CLI."""
        import os
        from hermes_nerve import cli as cli_mod
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "config.yaml").write_text(
                "plugins:\n  entries:\n    nerve:\n      config:\n        reflex_backend: d1\n")
            save_profile({"version": 1, "nerve_profile": "custom", "nerve_modules": {}, "advanced": {}}, Path(td))
            old_home = os.environ.get("HERMES_HOME")
            os.environ["HERMES_HOME"] = td
            try:
                resolved = cli_mod._current()
            finally:
                if old_home is None:
                    os.environ.pop("HERMES_HOME", None)
                else:
                    os.environ["HERMES_HOME"] = old_home
            self.assertEqual(resolved.get_config("reflex_backend", "jev"), "d1")

    def test_ui_switch_reflects_saved_override(self):
        """The feature switch must show the operator's saved choice, not only the
        effective runtime state, so a pruned-but-on module stays visibly on."""
        app_js = Path(__file__).resolve().parents[1] / "hermes_nerve" / "web" / "app.js"
        source = app_js.read_text(encoding="utf-8")
        self.assertIn("function savedOverride(", source)
        self.assertIn("savedOverride(id) === null ? featureEnabled(id) : savedOverride(id)", source)

    def test_panel_preview_allows_save_despite_stale_settings_pin(self):
        """Regression: the panel refused every save while config.yaml pinned a
        different profile, so the UI could never repair the conflict."""
        from hermes_nerve.control_panel import ControlPanel
        with tempfile.TemporaryDirectory() as td:
            save_profile({"version": 1, "nerve_profile": "custom",
                          "nerve_modules": {"remote_workers": True}, "advanced": {}}, Path(td))
            panel = ControlPanel(Path(td))
            request = {"profile": "custom", "overrides": {"remote_workers": True},
                       "advanced": {}, "clear_advanced": [], "reset": False}
            with mock.patch.object(ControlPanel, "host_settings",
                                   return_value=(_settings("operator"), [])):
                preview = panel.preview(dict(request))
                self.assertTrue(preview.get("conflict_note"))
                request["preview_id"] = preview["preview_id"]
                applied = panel.apply(dict(request))
            self.assertTrue(applied["ok"])
            doc = load_profile(Path(td))
            self.assertEqual(doc["nerve_profile"], "custom")
            self.assertTrue(doc["nerve_modules"]["remote_workers"])

    def test_panel_status_reports_conflict(self):
        from hermes_nerve.control_panel import ControlPanel
        with tempfile.TemporaryDirectory() as td:
            save_profile({"version": 1, "nerve_profile": "custom",
                          "nerve_modules": {"remote_workers": True}, "advanced": {}}, Path(td))
            panel = ControlPanel(Path(td))
            with mock.patch.object(ControlPanel, "host_settings",
                                   return_value=(_settings("operator"), [])):
                status = panel.status()
            self.assertIsNotNone(status["conflict"])
            self.assertEqual(status["conflict"]["ignored_settings_profile"], "operator")
            self.assertEqual(status["current"]["profile"], "custom")
            self.assertTrue(status["current"]["modules"]["remote_workers"])

    def test_panel_status_without_conflict_is_none(self):
        from hermes_nerve.control_panel import ControlPanel
        with tempfile.TemporaryDirectory() as td:
            save_profile({"version": 1, "nerve_profile": "custom", "nerve_modules": {}, "advanced": {}}, Path(td))
            panel = ControlPanel(Path(td))
            with mock.patch.object(ControlPanel, "host_settings",
                                   return_value=(_settings("custom"), [])):
                status = panel.status()
            self.assertIsNone(status["conflict"])


if __name__ == "__main__":
    unittest.main()
