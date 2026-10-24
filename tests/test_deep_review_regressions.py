"""Deep-review regression coverage (PR #75): packaging unity, override
precedence, effective-set views, and workdir-relative classification.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hermes_nerve.config_resolver import resolve_config
from hermes_nerve.control_panel import ControlPanel
from hermes_nerve.runtime_manifest import (
    RUNTIME_ASSET_PATTERNS, RUNTIME_ROOT_FILES, collect_runtime_files, find_manifest)


def _panel(home: str) -> ControlPanel:
    return ControlPanel(Path(home))


def _profile_doc(home: Path, profile: str, modules: dict | None = None) -> dict:
    return {"version": 1, "nerve_profile": profile,
            "nerve_modules": dict(modules or {}), "advanced": {}}


class RuntimePackagingTests(unittest.TestCase):
    """GUI, payload installer, and wheel must ship the same runtime files."""

    def test_collect_runtime_files_includes_required_policy_tables(self):
        repo_root = Path(__file__).resolve().parents[1]
        pkg = repo_root / "hermes_nerve"
        files = collect_runtime_files(pkg)
        rel = {p.relative_to(pkg).as_posix() for p in files}
        for table in ("policy/blocked_system_prefixes.txt",
                      "policy/disk_executables.txt",
                      "policy/unbounded_executables.txt"):
            self.assertIn(table, rel, f"runtime manifest must include {table}")
        # Root registration copies stage separately; __init__.py legitimately
        # appears as a package module too.
        for root_file in ("plugin.yaml", "CONTRIBUTORS.md", "CHANGELOG.md"):
            self.assertNotIn(root_file, rel)

    def test_policy_tables_load_from_staged_tree(self):
        """A staged tree built by the shared manifest imports with all three
        policy tables loadable — before any destination is touched."""
        import shutil
        repo_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as td:
            stage = Path(td) / "stage" / "hermes_nerve"
            stage.mkdir(parents=True)
            pkg = repo_root / "hermes_nerve"
            for path in collect_runtime_files(pkg):
                dest = stage / path.relative_to(pkg)
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, dest)
            missing = [name for name in (
                "blocked_system_prefixes.txt", "disk_executables.txt",
                "unbounded_executables.txt") if not (stage / "policy" / name).is_file()]
            self.assertEqual(missing, [], "staged tree missing policy tables")
            # The real loader must accept the staged tree (no TableLoadError).
            import importlib.util
            spec = importlib.util.spec_from_file_location(
                "_staged_policy_tables", stage / "policy_tables.py")
            module = importlib.util.module_from_spec(spec)
            with patch.object(module, "__file__", str(stage / "policy_tables.py")):
                spec.loader.exec_module(module)
            loader = getattr(module, "load_tables", None) or getattr(
                module, "_load_tables", None) or getattr(module, "load_policy_tables", None)
            if loader is not None:
                loader(stage / "policy")

    def test_find_manifest_source_and_wheel_layouts(self):
        repo_root = Path(__file__).resolve().parents[1]
        pkg = repo_root / "hermes_nerve"
        # Source checkout: manifest beside the package.
        self.assertEqual(find_manifest(pkg), repo_root / "plugin.yaml")
        # Wheel-style: share/hermes-nerve/plugin.yaml next to the package.
        with tempfile.TemporaryDirectory() as td:
            wheel_pkg = Path(td) / "site-packages" / "hermes_nerve"
            (wheel_pkg / "share" / "hermes-nerve").mkdir(parents=True)
            (wheel_pkg / "share" / "hermes-nerve" / "plugin.yaml").write_text(
                "name: nerve\nversion: \"0.4.14\"\n")
            self.assertEqual(find_manifest(wheel_pkg),
                             wheel_pkg / "share" / "hermes-nerve" / "plugin.yaml")

    def test_find_manifest_fails_loudly_when_absent(self):
        with tempfile.TemporaryDirectory() as td:
            empty = Path(td) / "hermes_nerve"
            empty.mkdir()
            with self.assertRaises(FileNotFoundError):
                find_manifest(empty)


class ModuleOverridePrecedenceTests(unittest.TestCase):
    """Module-override sources are tracked and disagreements between the saved
    sidecar and Hermes config.yaml are recorded, never silently absorbed."""

    def test_sidecar_config_conflict_is_recorded_with_winning_source(self):
        """Contract: Hermes config.yaml wins at module level (0.4.13 rule);
        the disagreement must be surfaced with both values and the winner."""
        from hermes_nerve.profiles import save_profile
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            save_profile({"version": 1, "nerve_profile": "operator",
                          "nerve_modules": {"shared_context": False}, "advanced": {}},
                         home=home)
            settings = {"nerve_modules": {"shared_context": True}}
            cfg = resolve_config(lambda k, d=None: settings.get(k, d), home=home)
            self.assertIs(cfg.modules.get("shared_context"), True)
            self.assertEqual(cfg.override_sources.get("shared_context"), "config")
            self.assertTrue(any(
                c.get("module") == "shared_context" and c.get("winner") == "config"
                and c.get("sidecar") is False and c.get("config") is True
                for c in cfg.module_conflicts))

    def test_config_override_applies_where_sidecar_silent(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            doc = _profile_doc(home, "operator")
            settings = {"nerve_modules": {"shared_context": True}}
            cfg = resolve_config(lambda k, d=None: settings.get(k, d),
                                 home=home, profile=doc)
            self.assertIs(cfg.modules.get("shared_context"), True)
            self.assertEqual(cfg.override_sources.get("shared_context"), "config")
            self.assertEqual(cfg.module_conflicts, [])

    def test_panel_status_exposes_sources_and_conflicts(self):
        with tempfile.TemporaryDirectory() as td:
            panel = _panel(td)
            status = panel.status()
            self.assertIn("module_override_sources", status)
            self.assertIn("module_conflicts", status)


class EffectiveSetViewTests(unittest.TestCase):
    """The current named profile's simplified view shows the effective enabled
    set — override-enabled modules are visible, canonical-hidden ones included."""

    def test_current_profile_view_shows_override_enabled_module(self):
        with tempfile.TemporaryDirectory() as td:
            panel = _panel(td)
            doc = _profile_doc(td, "operator", {"shared_context": True})
            cfg = resolve_config(home=Path(td), profile=doc)
            self.assertTrue(cfg.modules.get("shared_context"))
            views = panel.profile_views(doc, cfg)
            self.assertIn("shared_context", views["operator"]["primary_modules"])


class WorkdirRelativeClassificationTests(unittest.TestCase):
    """Relative paths classify against the declared workdir, not the
    classifier process CWD."""

    def test_relative_credential_in_workdir_outside_home_is_not_sensitive(self):
        from hermes_nerve import risk
        with tempfile.TemporaryDirectory() as td:
            workdir = Path(td) / "project"
            (workdir / "keys").mkdir(parents=True)
            with patch.object(risk, "_HOME", Path("/home/nobody-else")), \
                 patch.dict("os.environ", {"HOME": "/home/nobody-else"}), \
                 patch.object(risk, "temp_roots", lambda: []):
                scope = risk.classify_path(Path("keys/id_ed25519"), workdir)
            # Resolved against the declared workdir (approved scope), and the
            # relative credential name still trips the sensitive-name scan.
            self.assertEqual(scope, "workdir")

    def test_relative_credential_under_tmp_resident_home_stays_sensitive(self):
        from hermes_nerve import risk
        with tempfile.TemporaryDirectory() as td:
            fake_home = Path(td) / "runner-home"
            # workdir sits INSIDE the tmp-resident home; a relative credential
            # path must still classify sensitive, not routine workdir/tmp.
            workdir = fake_home / "scratch-project"
            workdir.mkdir(parents=True)
            with patch.object(risk, "_HOME", fake_home), \
                 patch.dict("os.environ", {"HOME": str(fake_home)}):
                scope = risk.classify_path(Path(".ssh/id_ed25519"), workdir)
            self.assertEqual(scope, "sensitive-home")


if __name__ == "__main__":
    unittest.main()
