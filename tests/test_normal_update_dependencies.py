"""Regression: Nerve must not poison Hermes's normal uv update workspace.

Hermes v0.22.5 PM (pm.plugin_declarations) considers a plugin an installable
workspace member when it has nonempty project dependencies OR any optional
dependency / dependency group / build-system / uv.package=true. A previous
Laya extra transitively required Torch, which failed the Python 3.14 Android
target and caused Hermes to disable Nerve across profiles.
"""
from __future__ import annotations

import ast
import copy
from pathlib import Path
import re
import unittest

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10, with CONTRIBUTING.md dev deps
    import tomli as tomllib

ROOT = Path(__file__).resolve().parents[1]


def _hermes_workspace_member(document: dict) -> bool:
    """Mirror the metadata classification from Hermes v0.22.5 PM, not its writes."""
    project = document.get("project", {})
    uv = document.get("tool", {}).get("uv", {})
    return bool(
        project.get("dependencies")
        or "build-system" in document
        or uv.get("package") is True
        or any(project.get("optional-dependencies", {}).values())
        or any(document.get("dependency-groups", {}).values())
        or uv.get("dev-dependencies")
    )


class NormalHermesUpdateDependencyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.package = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    def test_nerve_is_not_an_installable_member_of_shared_uv_workspace(self):
        self.assertEqual(self.package["project"]["dependencies"], [])
        self.assertFalse(_hermes_workspace_member(self.package))
        # Empty project extras would still advertise an unusable install path.
        self.assertNotIn("optional-dependencies", self.package["project"])
        self.assertNotIn("dependency-groups", self.package)
        self.assertNotIn("build-system", self.package)

    def test_old_laya_extra_reproduces_the_workspace_membership_failure(self):
        bad = copy.deepcopy(self.package)
        bad["project"]["optional-dependencies"] = {"laya": ["laya==0.3.29"]}
        self.assertTrue(_hermes_workspace_member(bad))
        bad["project"]["optional-dependencies"] = {"dev": ["pytest>=8,<9"]}
        self.assertTrue(_hermes_workspace_member(bad))

    def test_no_manifest_dependency_escape_hatch(self):
        manifest = (ROOT / "plugin.yaml").read_text(encoding="utf-8")
        self.assertIsNone(re.search(r"(?m)^\s*(?:pip_dependencies|python_dependencies):", manifest))

    def test_laya_remains_an_explicit_isolated_sidecar(self):
        from hermes_nerve.local_backend_setup import RECIPES

        self.assertEqual(RECIPES["laya"]["packages"], ("laya==0.3.29",))
        client = ast.parse((ROOT / "hermes_nerve/reflex/laya.py").read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(client):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        self.assertFalse(imported & {"laya", "torch", "transformers", "onnxruntime"})


if __name__ == "__main__":
    unittest.main()
