import tempfile, unittest
from pathlib import Path
from hermes_nerve.modules import MODULES
from hermes_nerve.profiles import PROFILES, load_profile, save_profile
from hermes_nerve.config_resolver import resolve_config

class ProfileTests(unittest.TestCase):
    def test_profiles_complete(self):
        for name in ("fat_cat","operator","lean","marie_kondo"):
            self.assertEqual(set(PROFILES[name]),set(MODULES))
    def test_shared_context_defaults(self):
        for name,expected in (("fat_cat",False),("operator",False),("lean",False),("marie_kondo",False)):
            doc={"version":1,"nerve_profile":name,"nerve_modules":{},"advanced":{}}
            self.assertEqual(resolve_config(profile=doc).enabled("shared_context"),expected)
    def test_shared_context_can_be_explicitly_enabled(self):
        for name in ("fat_cat", "operator"):
            doc={"version":1,"nerve_profile":name,"nerve_modules":{"shared_context":True},"advanced":{}}
            self.assertTrue(resolve_config(profile=doc).enabled("shared_context"))

    def test_save_load(self):
        with tempfile.TemporaryDirectory() as td:
            doc={"version":1,"nerve_profile":"lean","nerve_modules":{},"advanced":{}}
            p=save_profile(doc,Path(td))
            self.assertTrue(p.exists())
            self.assertEqual(load_profile(Path(td))["nerve_profile"],"lean")
