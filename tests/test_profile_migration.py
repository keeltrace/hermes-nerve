import json, tempfile, unittest
from pathlib import Path
from unittest.mock import patch
from hermes_nerve.config_resolver import resolve_config
from hermes_nerve.profiles import load_profile, normalize_profile_name, save_profile

class ProfileMigrationTests(unittest.TestCase):
    def test_legacy_preserves_v023_without_reading_real_home(self):
        with tempfile.TemporaryDirectory() as td:
            r=resolve_config(get_config=lambda k,d=None:d, home=Path(td), profile=None)
        self.assertEqual(r.profile,"legacy"); self.assertTrue(r.enabled("work_supervision")); self.assertTrue(r.enabled("context_governor")); self.assertFalse(r.enabled("action_gate"))

    def test_sidecar_profile_wins_and_stale_hermes_pin_is_reported(self):
        # 0.4.13 config-repair: the sidecar document is what both supported
        # writers (nerve setup, Control Panel) maintain, so it selects the
        # profile, keeps its overrides/advanced, and any differing Hermes
        # config.yaml nerve_profile is reported instead of silently winning.
        side={"version":1,"nerve_profile":"fat_cat","nerve_modules":{"assistant_loops":True,"context_governor":True},"advanced":{"gate_mode":"precommit"}}
        r=resolve_config(get_config=lambda k,d=None: "lean" if k=="nerve_profile" else d, profile=side)
        self.assertEqual(r.profile,"fat_cat"); self.assertTrue(r.enabled("assistant_loops")); self.assertTrue(r.enabled("context_governor")); self.assertEqual(r.advanced,{"gate_mode":"precommit"})
        self.assertEqual(r.ignored_settings_profile,"lean")

    def test_hermes_profile_applies_when_no_sidecar_exists(self):
        import tempfile
        with tempfile.TemporaryDirectory() as empty_home:
            r=resolve_config(get_config=lambda k,d=None: "lean" if k=="nerve_profile" else d, profile=None, home=empty_home)
            self.assertEqual(r.profile,"lean"); self.assertFalse(r.enabled("assistant_loops"))

    def test_corrupt_profile_recovers_backup_then_falls_back_legacy(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)/"nerve"; root.mkdir(); (root/"profile.json").write_text("{bad")
            (root/"profile.json.bak").write_text(json.dumps({"version":1,"nerve_profile":"Lean","nerve_modules":{},"advanced":{}}))
            with self.assertLogs("hermes_nerve.profiles",level="WARNING"):
                self.assertEqual(load_profile(Path(td))["nerve_profile"],"lean")
            (root/"profile.json.bak").write_text("also bad")
            with self.assertLogs("hermes_nerve.profiles",level="ERROR"):
                with self.assertRaisesRegex(RuntimeError,"Invalid Nerve profile"):
                    load_profile(Path(td))

    def test_save_after_recovery_does_not_destroy_good_backup(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)/"nerve"; root.mkdir()
            recovered={"version":1,"nerve_profile":"lean","nerve_modules":{},"advanced":{}}
            replacement={"version":1,"nerve_profile":"operator","nerve_modules":{},"advanced":{}}
            (root/"profile.json").write_text("{bad")
            (root/"profile.json.bak").write_text(json.dumps(recovered))
            with self.assertLogs("hermes_nerve.profiles",level="WARNING"):
                self.assertEqual(load_profile(Path(td)),recovered)
            save_profile(replacement,Path(td))
            self.assertEqual(json.loads((root/"profile.json.bak").read_text()),recovered)
            self.assertEqual(load_profile(Path(td)),replacement)

    def test_clear_profile_removes_stale_backup_authority(self):
        with tempfile.TemporaryDirectory() as td:
            home=Path(td); root=home/"nerve"
            old={"version":1,"nerve_profile":"operator","nerve_modules":{},"advanced":{}}
            newer={"version":1,"nerve_profile":"lean","nerve_modules":{},"advanced":{}}
            save_profile(old,home); save_profile(old,home)
            from hermes_nerve.profiles import clear_profile
            clear_profile(home)
            self.assertFalse((root/"profile.json").exists())
            self.assertFalse((root/"profile.json.bak").exists())
            save_profile(newer,home)
            (root/"profile.json").write_text("{bad")
            with self.assertLogs("hermes_nerve.profiles",level="ERROR"):
                with self.assertRaisesRegex(RuntimeError,"Invalid Nerve profile"):
                    load_profile(home)

    def test_profile_name_normalization(self):
        self.assertEqual(normalize_profile_name("Fat-Cat"),"fat_cat")
        self.assertEqual(normalize_profile_name("Marie Kondo"),"marie_kondo")

    def test_malformed_explicit_module_overrides_fail_closed(self):
        bad_values=(
            '{"action_gate": "false"}',
            '{"not_a_module": false}',
            '[["action_gate", false]]',
            '{bad json',
        )
        for raw in bad_values:
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError):
                    resolve_config(get_config=lambda k,d=None: "operator" if k=="nerve_profile" else (raw if k=="nerve_modules" else d), profile=None)

    def test_unsupported_backup_version_is_not_guessed_and_remains_on_disk(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)/"nerve"; root.mkdir(); (root/"profile.json").write_text("{bad")
            backup=root/"profile.json.bak"
            backup.write_text(json.dumps({"version":0,"nerve_profile":"lean","nerve_modules":{"context_governor":True},"advanced":{}}))
            before=backup.read_text()
            with self.assertLogs("hermes_nerve.profiles",level="ERROR"):
                with self.assertRaisesRegex(RuntimeError,"Invalid Nerve profile"):
                    load_profile(Path(td))
            self.assertEqual(backup.read_text(),before)
