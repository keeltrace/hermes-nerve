import io, unittest
from unittest.mock import patch
from hermes_nerve import cli

class SetupCliTests(unittest.TestCase):
    def _mutations(self):
        fake_path=type("P",(),{"__str__":lambda self:"/tmp/profile.json"})()
        return patch.object(cli,"save_profile",return_value=fake_path), patch.object(cli.shared_context,"reconcile_enabled",return_value={"changed":True}), patch.object(cli.assistant,"install",return_value={}), patch.object(cli.assistant,"disable",return_value={}), patch.object(cli,"load_profile",return_value=None)

    def test_show(self):
        fake=type("R",(),{"profile":"legacy","enabled":lambda self,k:False})()
        out=io.StringIO()
        with patch.object(cli,"_current",lambda:fake), patch("sys.stdout",out): self.assertEqual(cli.main(["setup","--show"]),0)
        self.assertIn("Profile: Legacy",out.getvalue())

    def test_profile_order(self): self.assertEqual(cli._ORDER,("fat_cat","operator","lean","marie_kondo"))

    def test_noninteractive_profile_reconciles_shared_context(self):
        a,b,c,d,e=self._mutations()
        with a,b as rec,c,d,e:
            self.assertEqual(cli.main(["setup","--profile","lean"]),0); rec.assert_called_once_with(False)

    def test_explain_does_not_warn_when_shared_context_is_off(self):
        out=io.StringIO()
        for profile in ("operator", "fat_cat"):
            current=cli.resolve_config(profile=cli._doc(profile))
            with patch.object(cli,"_current",return_value=current), \
                 patch.object(cli.shared_context,"explain",return_value="HermesContextBus is not installed") as explain, \
                 patch("sys.stdout",out):
                self.assertEqual(cli.main(["setup","--explain"]),0)
            explain.assert_not_called()
        self.assertNotIn("HermesContextBus is not installed",out.getvalue())
        self.assertIn("Shared Context: OFF",out.getvalue())

    def test_explain_warns_when_shared_context_is_explicitly_enabled(self):
        current=cli.resolve_config(profile=cli._doc("operator",{"shared_context":True}))
        out=io.StringIO()
        with patch.object(cli,"_current",return_value=current), \
             patch.object(cli.shared_context,"explain",return_value="HermesContextBus is not installed") as explain, \
             patch("sys.stdout",out):
            self.assertEqual(cli.main(["setup","--explain"]),0)
        explain.assert_called_once_with()
        self.assertIn("HermesContextBus is not installed",out.getvalue())

    def test_install_shared_context_cli_remains_supported(self):
        status={"path":"/tmp/hermes-context-bus","doctor_command":"hermes plugins doctor /tmp/hermes-context-bus --ci"}
        current=type("R",(),{"enabled":lambda self,key:False})()
        out=io.StringIO()
        with patch.object(cli.shared_context,"install_from_source",return_value=status) as install, \
             patch.object(cli,"_current",return_value=current), patch("sys.stdout",out):
            self.assertEqual(cli.main(["setup","--install-shared-context","/tmp/source"]),0)
        install.assert_called_once_with("/tmp/source",replace=False)
        self.assertIn("Shared Context installed",out.getvalue())

    def test_reconcile_failure_restores_previous_profile(self):
        import os, tempfile
        from pathlib import Path
        from hermes_nerve.profiles import load_profile, save_profile
        previous=cli._doc("operator",{"context_governor":True},{"timeout_seconds":17.0})
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ,{"HERMES_HOME":td},clear=False):
            save_profile(previous,Path(td))
            with patch.object(cli.shared_context,"reconcile_enabled",side_effect=RuntimeError("boom")):
                with self.assertRaisesRegex(RuntimeError,"boom"):
                    cli._save("lean",reset=True)
            self.assertEqual(load_profile(Path(td)),previous)

    def test_reconcile_failure_restores_legacy_no_sidecar(self):
        import os, tempfile
        from pathlib import Path
        from hermes_nerve.profiles import load_profile
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ,{"HERMES_HOME":td},clear=False):
            with patch.object(cli.shared_context,"reconcile_enabled",side_effect=RuntimeError("boom")):
                with self.assertRaisesRegex(RuntimeError,"boom"):
                    cli._save("lean",reset=True)
            self.assertIsNone(load_profile(Path(td)))

    def test_late_save_failure_restores_exact_primary_and_backup(self):
        import os, tempfile
        from pathlib import Path
        from hermes_nerve.profiles import save_profile, snapshot_profile_state
        older=cli._doc("operator",{"context_governor":True},{"timeout_seconds":11.0})
        current=cli._doc("lean",{"context_governor":True},{"timeout_seconds":17.0})
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ,{"HERMES_HOME":td},clear=False):
            home=Path(td)
            save_profile(older,home)
            save_profile(current,home)
            before=snapshot_profile_state(home)
            real_save=save_profile
            def fail_after_write(doc):
                real_save(doc,home)
                raise RuntimeError("late save failure")
            with patch.object(cli,"save_profile",side_effect=fail_after_write):
                with self.assertRaisesRegex(RuntimeError,"late save failure"):
                    cli._save("fat_cat",reset=True)
            self.assertEqual(snapshot_profile_state(home),before)

    def test_legacy_is_explicit_cli_target(self):
        a,b,c,d,e=self._mutations()
        with a as save,b,c,d,e:
            self.assertEqual(cli.main(["setup","--profile","legacy"]),0)
            self.assertEqual(save.call_args.args[0]["nerve_profile"],"legacy")

    def test_show_profile_is_preview_not_mutation(self):
        with patch.object(cli,"save_profile") as save, patch("sys.stdout",io.StringIO()):
            self.assertEqual(cli.main(["setup","--profile","lean","--show"]),0); save.assert_not_called()

    def test_interactive_rejects_zero_and_negative_choices(self):
        fake=type("R",(),{"modules":{mid:False for mid in cli.MODULES}})()
        for value in ("0","-1"):
            with patch.object(cli,"_current",lambda:fake), patch("sys.stdin.isatty",return_value=True), patch("builtins.input",return_value=value):
                self.assertEqual(cli.main(["setup"]),2)

    def test_selecting_same_profile_preserves_explicit_overrides(self):
        existing={"version":1,"nerve_profile":"lean","nerve_modules":{"context_governor":True},"advanced":{}}
        fake_path=type("P",(),{"__str__":lambda self:"/tmp/profile.json"})()
        out=io.StringIO()
        with patch.object(cli,"load_profile",return_value=existing), patch.object(cli,"save_profile",return_value=fake_path) as save, \
             patch.object(cli.shared_context,"reconcile_enabled",return_value={"changed":True}), patch.object(cli.assistant,"disable",return_value={}), \
             patch("sys.stdout",out):
            self.assertEqual(cli.main(["setup","--profile","lean"]),0)
        self.assertEqual(save.call_args.args[0]["nerve_modules"],{"context_governor":True})
        self.assertIn("[ON ] Context governor",out.getvalue())

    def test_reset_profile_clears_explicit_overrides(self):
        existing={"version":1,"nerve_profile":"lean","nerve_modules":{"context_governor":True},"advanced":{}}
        fake_path=type("P",(),{"__str__":lambda self:"/tmp/profile.json"})()
        with patch.object(cli,"load_profile",return_value=existing), patch.object(cli,"save_profile",return_value=fake_path) as save, \
             patch.object(cli.shared_context,"reconcile_enabled",return_value={"changed":True}), patch.object(cli.assistant,"disable",return_value={}):
            self.assertEqual(cli.main(["setup","--reset","lean"]),0)
        self.assertEqual(save.call_args.args[0]["nerve_modules"],{})

    def test_same_profile_preserves_advanced_settings(self):
        existing={"version":1,"nerve_profile":"lean","nerve_modules":{"context_governor":True},"advanced":{"gate_mode":"precommit","timeout_seconds":17.0}}
        fake_path=type("P",(),{"__str__":lambda self:"/tmp/profile.json"})()
        with patch.object(cli,"load_profile",return_value=existing), patch.object(cli,"save_profile",return_value=fake_path) as save, \
             patch.object(cli.shared_context,"reconcile_enabled",return_value={"changed":True}):
            self.assertEqual(cli.main(["setup","--profile","lean"]),0)
        self.assertEqual(save.call_args.args[0]["advanced"],existing["advanced"])

    def test_reset_clears_advanced_settings(self):
        existing={"version":1,"nerve_profile":"lean","nerve_modules":{},"advanced":{"timeout_seconds":17.0}}
        fake_path=type("P",(),{"__str__":lambda self:"/tmp/profile.json"})()
        with patch.object(cli,"load_profile",return_value=existing), patch.object(cli,"save_profile",return_value=fake_path) as save, \
             patch.object(cli.shared_context,"reconcile_enabled",return_value={"changed":True}):
            self.assertEqual(cli.main(["setup","--reset","lean"]),0)
        self.assertEqual(save.call_args.args[0]["advanced"],{})

    def test_advanced_editor_can_set_typed_values(self):
        answers=iter(["yes","timeout_seconds","17.5","nervous_max_provider_calls_per_turn","12",""])
        with patch("builtins.input",side_effect=lambda *a: next(answers)):
            edited=cli._edit_advanced({})
        self.assertEqual(edited["timeout_seconds"],17.5)
        self.assertEqual(edited["nervous_max_provider_calls_per_turn"],12)

    def test_profile_selection_does_not_persist_assistant_override(self):
        fake_path=type("P",(),{"__str__":lambda self:"/tmp/profile.json"})()
        with patch.object(cli,"load_profile",return_value=None), patch.object(cli,"save_profile",return_value=fake_path), \
             patch.object(cli.shared_context,"reconcile_enabled",return_value={"changed":True}), \
             patch.object(cli.assistant,"disable") as disable, patch.object(cli.assistant,"install") as install:
            self.assertEqual(cli.main(["setup","--profile","lean"]),0)
        disable.assert_not_called(); install.assert_not_called()

    def test_advanced_catalog_contains_manifest_keys_and_fails_loudly_on_schema_drift(self):
        catalog=cli._advanced_catalog()
        self.assertIn("timeout_seconds",catalog)
        self.assertIn("remote_hosts",catalog)
        self.assertGreater(len(catalog),50)

    def test_advanced_editor_clear_and_dict_values(self):
        answers=iter(["yes","clear timeout_seconds","remote_hosts","{'gpu': {'host': '10.0.0.2'}}",""])
        with patch("builtins.input",side_effect=lambda *a: next(answers)):
            edited=cli._edit_advanced({"timeout_seconds":17.0})
        self.assertNotIn("timeout_seconds",edited)
        self.assertEqual(edited["remote_hosts"],{"gpu":{"host":"10.0.0.2"}})

    def test_advanced_round_trip_persists_without_corruption(self):
        import tempfile
        from pathlib import Path
        from hermes_nerve.profiles import load_profile, save_profile
        with tempfile.TemporaryDirectory() as td:
            doc=cli._doc("custom",{"context_governor":True},{"timeout_seconds":17.5,"remote_hosts":{"gpu":{"host":"10.0.0.2"}}})
            save_profile(doc,home=Path(td))
            loaded=load_profile(Path(td))
        self.assertEqual(loaded,doc)
