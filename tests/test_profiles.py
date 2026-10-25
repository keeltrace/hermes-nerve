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
    def test_new_features_selectable_independently(self):
        new_features = ("verified_experience", "selective_context_memory", "selective_context_tools", "selective_context_skills")
        for feature in new_features:
            doc={"version":1,"nerve_profile":"operator","nerve_modules":{feature:False},"advanced":{}}
            resolved=resolve_config(profile=doc)
            self.assertFalse(resolved.enabled(feature))
            for other in new_features:
                if other != feature:
                    self.assertTrue(resolved.enabled(other), (feature, other))

    def test_disable_selective_context_prunes_all_surfaces(self):
        doc={"version":1,"nerve_profile":"operator","nerve_modules":{"selective_context":False},"advanced":{}}
        resolved=resolve_config(profile=doc)
        for module in ("selective_context", "selective_context_memory", "selective_context_tools", "selective_context_skills"):
            self.assertFalse(resolved.enabled(module))
    
    def test_legacy_disables_new_modules(self):
        doc={"version":1,"nerve_profile":"legacy","nerve_modules":{},"advanced":{}}
        resolved=resolve_config(profile=doc)
        for module in ("verified_experience", "selective_context", "selective_context_memory", "selective_context_tools", "selective_context_skills"):
            self.assertFalse(resolved.enabled(module))
