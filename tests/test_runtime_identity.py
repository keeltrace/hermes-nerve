from __future__ import annotations

import importlib.util
import json
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

from hermes_nerve import tools
from hermes_nerve.provenance import VERSION, _manifest_version, _module_code_fingerprints, runtime_identity
from hermes_nerve.schemas import NERVE_STATS


class RuntimeIdentityTests(unittest.TestCase):
    def test_identity_reports_loaded_module_paths_and_content_hashes(self):
        identity = runtime_identity()
        self.assertEqual(identity["version"], VERSION)
        self.assertTrue(identity["loaded_modules"])
        for item in identity["loaded_modules"]:
            self.assertTrue(Path(item["path"]).is_absolute())
            self.assertRegex(item["source_sha256"], r"^[0-9a-f]{64}$")
            self.assertRegex(item["code_sha256"], r"^[0-9a-f]{64}$")
            self.assertRegex(item["disk_code_sha256"], r"^[0-9a-f]{64}$")
            self.assertTrue(item["code_matches_disk"], item["path"])
        self.assertRegex(identity["loaded_code_tree_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(identity["status"], "MATCH")
        self.assertEqual(identity["issues"], [])

    def test_identity_metadata_matches_plugin_manifest(self):
        root = Path(__file__).resolve().parents[1]
        manifest = (root / "plugin.yaml").read_text(encoding="utf-8")
        project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertIn('version: "' + VERSION + '"', manifest)
        self.assertEqual(project["project"]["dynamic"], ["version"])
        self.assertEqual(project["tool"]["setuptools"]["dynamic"]["version"]["attr"], "hermes_nerve._version.__version__")
        package_data = project["tool"]["setuptools"]["package-data"]["hermes_nerve"]
        self.assertIn("observatory/assets/*.css", package_data)
        self.assertIn("observatory/assets/*.js", package_data)
        self.assertIn("research/*.md", package_data)

    def test_identity_finds_wheel_data_manifest(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "site-packages" / "hermes_nerve"
            root.mkdir(parents=True)
            manifest = root.parent / "share" / "hermes-nerve" / "plugin.yaml"
            manifest.parent.mkdir(parents=True)
            manifest.write_text(f'name: hermes-nerve\nversion: "{VERSION}"\n', encoding="utf-8")
            self.assertEqual(_manifest_version(root), (VERSION, str(manifest.resolve())))

    def test_nerve_stats_identity_section_reports_runtime_code(self):
        with (
            patch.object(tools.receipts, "report", return_value={}),
            patch.object(tools.gate, "report", return_value={}),
            patch.object(tools.ledger, "report", return_value={}),
            patch.object(tools.nervous, "report", return_value={}),
            patch.object(tools.selective_context, "report", return_value={}),
        ):
            result = json.loads(tools.nerve_stats({"section": "identity"}))
        self.assertEqual(result["execution"]["version"], VERSION)
        self.assertEqual(result["runtime_identity"]["version"], VERSION)
        self.assertTrue(result["runtime_identity"]["loaded_modules"])
        sections = NERVE_STATS["parameters"]["properties"]["section"]["enum"]
        self.assertIn("identity", sections)

    def test_loaded_code_hash_detects_source_modified_after_import(self):
        with tempfile.TemporaryDirectory() as td:
            source = Path(td) / "sample_plugin.py"
            source.write_text("def behavior():\n    return 'loaded'\n", encoding="utf-8")
            spec = importlib.util.spec_from_file_location("sample_plugin", source)
            self.assertIsNotNone(spec)
            module = importlib.util.module_from_spec(spec)
            self.assertIsNotNone(spec.loader)
            spec.loader.exec_module(module)
            source.write_text("def behavior():\n    return 'changed'\n", encoding="utf-8")
            live_hash, disk_hash = _module_code_fingerprints(module)
        self.assertNotEqual(live_hash, disk_hash)

    def test_runtime_identity_does_not_clear_loaded_code_drift(self):
        with patch("hermes_nerve.provenance._module_code_fingerprints", return_value=("live", "disk")):
            identity = runtime_identity()
        self.assertEqual(identity["status"], "DRIFT")
        self.assertIn("loaded-code-drift", identity["issues"])

    def test_loaded_module_constant_drift_is_detected(self):
        with tempfile.TemporaryDirectory() as td:
            source = Path(td) / "versioned_plugin.py"
            source.write_text("VERSION = 'loaded'\ndef behavior():\n    return VERSION\n", encoding="utf-8")
            spec = importlib.util.spec_from_file_location("versioned_plugin", source)
            self.assertIsNotNone(spec)
            module = importlib.util.module_from_spec(spec)
            self.assertIsNotNone(spec.loader)
            spec.loader.exec_module(module)
            self.assertEqual(*_module_code_fingerprints(module))
            source.write_text("VERSION = 'changed'\ndef behavior():\n    return VERSION\n", encoding="utf-8")
            live_hash, disk_hash = _module_code_fingerprints(module)
        self.assertNotEqual(live_hash, disk_hash)

    def test_source_tree_hash_includes_non_python_plugin_assets(self):
        with tempfile.TemporaryDirectory() as td:
            package_root = Path(td) / "hermes_nerve"
            (package_root / "web").mkdir(parents=True)
            (package_root.parent / "plugin.yaml").write_text(
                f'name: nerve\\nversion: "{VERSION}"\\n', encoding="utf-8"
            )
            (package_root.parent / "__init__.py").write_text("", encoding="utf-8")
            asset = package_root / "web" / "app.js"
            asset.write_text("const build = 'one';\\n", encoding="utf-8")
            backup = package_root / "stale.py.bak"
            backup.write_text("old source one", encoding="utf-8")
            with patch("hermes_nerve.provenance._identity_root", return_value=package_root):
                first = runtime_identity()["source_tree_sha256"]
                backup.write_text("old source two", encoding="utf-8")
                after_backup_change = runtime_identity()["source_tree_sha256"]
                asset.write_text("const build = 'two';\\n", encoding="utf-8")
                second = runtime_identity()["source_tree_sha256"]
        self.assertEqual(first, after_backup_change)
        self.assertNotEqual(first, second)

    def test_identity_flags_manifest_version_mismatch(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "plugin.yaml").write_text('name: nerve\nversion: "wrong"\n', encoding="utf-8")
            with patch("hermes_nerve.provenance._identity_root", return_value=root):
                identity = runtime_identity()
        self.assertEqual(identity["manifest_version"], "wrong")
        self.assertEqual(identity["status"], "DRIFT")


if __name__ == "__main__":
    unittest.main()
