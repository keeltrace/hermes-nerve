from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "install_hermes_plugin.py"


def load_installer():
    spec = importlib.util.spec_from_file_location("hcb_installer", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class InstallerTests(unittest.TestCase):
    def test_copy_plugin_installs_flat_user_plugin(self):
        module = load_installer()
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / ".hermes"
            dest = module.copy_plugin(ROOT, home)
            self.assertEqual(dest, home / "plugins" / "hermes-context-bus")
            self.assertTrue((dest / "plugin.yaml").is_file())
            self.assertTrue((dest / "__init__.py").is_file())
            self.assertTrue((dest / "bridge.py").is_file())


if __name__ == "__main__":
    unittest.main()