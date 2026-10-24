"""Profile views + configuration states contract (config-repair Phase 3/5).

The Control Panel UI must render server-provided per-profile views and may
keep no hardcoded per-profile module rules. The Configured / Effective /
Running / Pending-restart states must be honest: running-state verification is
only claimed when the panel runs inside the Hermes plugin host.
"""
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from hermes_nerve import control_panel
from hermes_nerve.control_panel import ControlPanel
from hermes_nerve.modules import MODULES
from hermes_nerve.profiles import PROFILE_NAMES, PROFILES


def _panel(td):
    return ControlPanel(home=Path(td), source=Path(control_panel.__file__).resolve().parents[1])


NAMED_PROFILES = ("operator", "lean", "fat_cat", "marie_kondo")


class ProfileViewTests(unittest.TestCase):
    def test_views_cover_every_profile_with_emphasis(self):
        with tempfile.TemporaryDirectory() as td:
            panel = _panel(td)
            status = panel.status()
            self.assertEqual(set(status["views"]), set(PROFILE_NAMES))
            for name, view in status["views"].items():
                self.assertTrue(view["emphasis"], name)
                self.assertIn("primary_modules", view)
                self.assertIn("available_with_setup", view)

    def test_named_profile_views_match_canonical_defaults(self):
        with tempfile.TemporaryDirectory() as td:
            status = _panel(td).status()
            for name in NAMED_PROFILES:
                expected = sorted(k for k, on in PROFILES[name].items() if on)
                self.assertEqual(status["views"][name]["primary_modules"], expected, name)

    def test_custom_view_shows_effective_modules_when_current(self):
        with tempfile.TemporaryDirectory() as td:
            panel = _panel(td)
            from hermes_nerve.config_resolver import resolve_config
            doc = {"version": 1, "nerve_profile": "custom", "nerve_modules": {}, "advanced": {}}
            cfg = resolve_config(home=Path(td), profile=doc)
            views = panel.profile_views(doc, cfg)
            self.assertEqual(
                views["custom"]["primary_modules"],
                sorted(k for k, on in cfg.modules.items() if on),
            )
            # Not current -> no claim about its primary set.
            self.assertEqual(views["legacy"]["primary_modules"], [])

    def test_available_with_setup_excludes_primary_and_setup_dependent(self):
        with tempfile.TemporaryDirectory() as td:
            views = _panel(td).status()["views"]
            for name in NAMED_PROFILES:
                setup = views[name]["available_with_setup"]
                primary = set(views[name]["primary_modules"])
                self.assertFalse(primary & set(setup), name)
                for mid in setup:
                    self.assertIn(mid, MODULES)

    def test_ui_has_no_hardcoded_profile_module_rules(self):
        app_js = (Path(control_panel.__file__).parent / "web" / "app.js").read_text()
        self.assertNotIn("visibleSimple", app_js)
        for module_id in MODULES:
            # Module ids may appear only in data-driven strings, never as a
            # static JS membership set. A rough but effective guard: no line
            # in app.js declares a module-id list literal.
            for line in app_js.splitlines():
                stripped = line.strip()
                if stripped.startswith('const ') and 'Set([' in stripped:
                    self.assertNotIn(module_id, stripped)


class ConfigStatesTests(unittest.TestCase):
    def test_states_present_and_honest_when_standalone(self):
        with tempfile.TemporaryDirectory() as td:
            states = _panel(td).status()["states"]
            self.assertFalse(states["running"]["available"])
            self.assertIn("unavailable", states["running"]["detail"])
            self.assertIsNone(states["pending_restart"])
            self.assertFalse(states["configured"]["has_saved_sidecar"])
            # A fresh home resolves to Legacy until the operator picks a
            # profile: nothing changes without an explicit choice.
            self.assertEqual(states["effective"]["profile"], "legacy")
            self.assertEqual(set(states["effective"]["reasons"]), set(MODULES))

    def test_configured_reflects_saved_sidecar(self):
        from hermes_nerve.profiles import save_profile
        with tempfile.TemporaryDirectory() as td:
            panel = _panel(td)
            save_profile({"version": 1, "nerve_profile": "lean",
                          "nerve_modules": {"context_governor": True},
                          "advanced": {}}, Path(td))
            states = panel.status()["states"]
            self.assertTrue(states["configured"]["has_saved_sidecar"])
            self.assertEqual(states["configured"]["profile"], "lean")
            self.assertEqual(states["configured"]["overrides"], {"context_governor": True})

    def test_pending_restart_true_only_when_saved_after_host_load(self):
        from hermes_nerve.profiles import save_profile
        fake_host = types.ModuleType("hermes_cli")
        fake_identity = {"loaded": [{"module": "hermes_nerve"}],
                         "plugin_root": "/srv/nerve", "source_tree_sha256": "abc"}
        with tempfile.TemporaryDirectory() as td:
            panel = _panel(td)
            sidecar = Path(td) / "nerve" / "profile.json"
            save_profile({"version": 1, "nerve_profile": "lean",
                          "nerve_modules": {}, "advanced": {}}, Path(td))
            old = os.path.getmtime(sidecar)
            os.utime(sidecar, (old - 10_000, old - 10_000))
            with patch.dict(sys.modules, {"hermes_cli": fake_host}), \
                 patch.object(control_panel, "runtime_identity", return_value=fake_identity):
                before = panel.status()["states"]
                self.assertTrue(before["running"]["available"])
                self.assertFalse(before["pending_restart"])
                save_profile({"version": 1, "nerve_profile": "fat_cat",
                              "nerve_modules": {}, "advanced": {}}, Path(td))
                after = panel.status()["states"]
                self.assertTrue(after["pending_restart"])
            # Standalone again -> honest unknown.
            standalone = panel.status()["states"]
            self.assertFalse(standalone["running"]["available"])
            self.assertIsNone(standalone["pending_restart"])

    def test_preview_carries_reasons_and_states(self):
        with tempfile.TemporaryDirectory() as td:
            panel = _panel(td)
            preview = panel.preview({"profile": "operator", "overrides": {},
                                     "advanced": {}, "clear_advanced": []})
            self.assertEqual(set(preview["reasons"]), set(MODULES))
            self.assertIn("states", preview)
            self.assertFalse(preview["states"]["running"]["available"])

    def test_corrupt_sidecar_is_refused_loudly(self):
        """A corrupt sidecar must never degrade into guessed states: the panel
        refuses and points at the repair path (conflict explained, safe
        resolution offered)."""
        from hermes_nerve.control_panel import PanelError
        with tempfile.TemporaryDirectory() as td:
            panel = _panel(td)
            path = Path(td) / "nerve" / "profile.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{not json at all", encoding="utf-8")
            with self.assertRaises(PanelError) as ctx:
                panel.status()
            self.assertIn("invalid", str(ctx.exception).lower())
            self.assertIn("Nerve CLI", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
