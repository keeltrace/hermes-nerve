"""Nerve settings are read from the plugin's own entry, ``plugins.entries.nerve``.

Hermes' ``PluginContext.get_config`` reads ``plugins.entries.<plugin id>.settings``,
falling back to that entry's ``config`` subtree, and ``plugin.yaml`` declares
``name: nerve``. A ``plugins.entries.hermes-nerve`` block, which earlier docs and the
Reflex profile script wrote, is never read.
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ID = yaml.safe_load((ROOT / "plugin.yaml").read_text(encoding="utf-8"))["name"]


def load_plugin_root(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def hermes_config_modules(config):
    """Stand-in for Hermes' canonical reader, ``hermes_cli.config.load_config_readonly``."""
    package = types.ModuleType("hermes_cli")
    package.__path__ = []
    config_module = types.ModuleType("hermes_cli.config")
    config_module.load_config_readonly = lambda: config
    package.config = config_module
    return {"hermes_cli": package, "hermes_cli.config": config_module}


class HermesCtx:
    """Settings lookup of Hermes ``PluginContext.get_config`` over a parsed config.yaml."""

    plugin_id = PLUGIN_ID

    def __init__(self, config):
        self.config = config
        self.tools = {}
        self.hooks = []
        self.engine = None

    def get_config(self, key, default=None):
        entry = ((self.config.get("plugins") or {}).get("entries") or {}).get(self.plugin_id)
        if not isinstance(entry, dict):
            return default
        settings = entry.get("settings") if isinstance(entry.get("settings"), dict) else {}
        if key in settings:
            return settings[key]
        fallback = entry.get("config") if isinstance(entry.get("config"), dict) else {}
        return fallback.get(key, default)

    def register_tool(self, *, name, schema=None, handler=None, **kwargs):
        self.tools[name] = (schema, handler)

    def register_hook(self, name, callback):
        self.hooks.append((name, callback))

    def register_context_engine(self, engine):
        self.engine = engine


class SettingsNamespaceTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        env = patch.dict(os.environ, {
            "HERMES_HOME": self.td.name, "HERMES_KANBAN_TASK": "", "HERMES_KANBAN_TASK_ID": "",
            "HERMES_NERVE_CONTEXT_LEDGER": str(Path(self.td.name) / "ledger.jsonl"),
        }, clear=False)
        env.start()
        self.addCleanup(env.stop)

    def settings(self, extra=None):
        values = {"work_supervision_db": str(Path(self.td.name) / "work.db"),
                  "remote_data_dir": str(Path(self.td.name) / "remote")}
        values.update(extra or {})
        return values

    def register(self, name, config):
        module = load_plugin_root(name)
        with patch.dict(sys.modules, hermes_config_modules(config)):
            module.register(HermesCtx(config))
        return module

    def test_legacy_settings_block_is_reported_as_ignored(self):
        config = {"plugins": {"entries": {
            PLUGIN_ID: {"settings": self.settings()},
            "hermes-nerve": {"settings": {"reflex_backend": "laya"}},
        }}}
        with self.assertLogs("hermes_nerve", level="WARNING") as logs:
            module = self.register("nerve_settings_namespace_warning", config)
        message = "\n".join(logs.output)
        self.assertIn("plugins.entries.hermes-nerve", message)
        self.assertIn(f"plugins.entries.{PLUGIN_ID}.settings", message)
        self.assertEqual(module.reflex.settings()["backend"], "jev")

    def test_no_warning_without_a_legacy_block(self):
        config = {"plugins": {"entries": {PLUGIN_ID: {"settings": self.settings({"reflex_backend": "laya"})}}}}
        with self.assertNoLogs("hermes_nerve", level="WARNING"):
            module = self.register("nerve_settings_namespace_quiet", config)
        self.assertEqual(module.reflex.settings()["backend"], "laya")

    def run_configure_script(self, profile_config, *extra_args, env=None):
        profile = Path(self.td.name) / "profiles" / "worker"
        profile.mkdir(parents=True, exist_ok=True)
        if profile_config is not None:
            (profile / "config.yaml").write_text(yaml.safe_dump(profile_config), encoding="utf-8")
        base = {key: value for key, value in os.environ.items()
                if not key.startswith("HERMES_REFLEX_") and key != "HERMES_NERVE_ORCHESTRATOR_REVIEWER"}
        done = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "configure_reflex_profile.py"), "worker", *extra_args],
            env={**base, **(env or {}), "HERMES_HOME": self.td.name}, capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        entries = yaml.safe_load((profile / "config.yaml").read_text(encoding="utf-8"))["plugins"]["entries"]
        return entries, done.stdout

    @staticmethod
    def effective(entries, key):
        """Value Hermes would pass to Nerve for ``key`` (settings, then the config fallback)."""
        return HermesCtx({"plugins": {"entries": entries}}).get_config(key)

    def test_configure_reflex_profile_writes_plugin_namespace(self):
        entries, _ = self.run_configure_script({"model": "x"}, "--backend", "laya", "--shadow-sync")
        self.assertEqual(entries[PLUGIN_ID]["settings"], {"reflex_backend": "laya", "reflex_shadow_async": False})
        self.assertNotIn("hermes-nerve", entries)

    def test_configure_reflex_profile_leaves_a_legacy_entry_untouched(self):
        # The script does not migrate: it reports the ignored entry and changes nothing in it.
        legacy = {"settings": {"reflex_backend": "jev", "reflex_laya_base_url": "https://stale.example/v1"}}
        entries, stdout = self.run_configure_script({"plugins": {"entries": {
            "hermes-nerve": legacy,
            PLUGIN_ID: {"config": {"reflex_laya_base_url": "https://active.example/v1"}},
        }}}, "--backend", "laya", "--laya-base-url", "https://chosen.example/v1")
        self.assertEqual(entries["hermes-nerve"], legacy)
        self.assertIn("plugins.entries.hermes-nerve", stdout)
        self.assertEqual(entries[PLUGIN_ID]["settings"], {"reflex_backend": "laya", "reflex_laya_base_url": "https://chosen.example/v1"})
        self.assertEqual(entries[PLUGIN_ID]["config"], {"reflex_laya_base_url": "https://active.example/v1"})

    def test_configure_reflex_profile_keeps_effective_values_for_omitted_options(self):
        # Settings take precedence over the config fallback, so writing a default for an
        # omitted option would silently replace the value Hermes uses, for example clearing
        # an OpenJev identity pin. Only options given on the command line may change.
        entries, _ = self.run_configure_script({"plugins": {"entries": {PLUGIN_ID: {"config": {
            "reflex_openjev_expected_identity": "required-identity",
            "reflex_openjev_base_url": "https://pinned.example/api",
            "reflex_shadow_async": False,
        }}}}}, "--backend", "openjev")
        self.assertEqual(self.effective(entries, "reflex_backend"), "openjev")
        self.assertEqual(self.effective(entries, "reflex_openjev_expected_identity"), "required-identity")
        self.assertEqual(self.effective(entries, "reflex_openjev_base_url"), "https://pinned.example/api")
        self.assertIs(self.effective(entries, "reflex_shadow_async"), False)
        # An explicit value, even an empty one, is written verbatim.
        entries, _ = self.run_configure_script(None, "--backend", "openjev", "--openjev-expected-identity", "")
        self.assertEqual(self.effective(entries, "reflex_openjev_expected_identity"), "")
        # Both shadow flags are explicit: --shadow-sync writes async=False, --no-shadow-sync writes True.
        entries, _ = self.run_configure_script(None, "--backend", "shadow", "--no-shadow-sync")
        self.assertIs(self.effective(entries, "reflex_shadow_async"), True)
        entries, _ = self.run_configure_script(None, "--backend", "shadow", "--shadow-sync")
        self.assertIs(self.effective(entries, "reflex_shadow_async"), False)

    def test_configure_reflex_profile_does_not_write_environment_values(self):
        # Nerve reads the HERMES_REFLEX_* fallbacks itself at load time, below settings and
        # config; the script copying them into settings would invert that precedence.
        entries, _ = self.run_configure_script({"model": "x"}, "--backend", "laya", env={
            "HERMES_REFLEX_LAYA_BASE_URL": "https://env.example/v1",
            "HERMES_REFLEX_OPENJEV_EXPECTED_IDENTITY": "",
            "HERMES_NERVE_ORCHESTRATOR_REVIEWER": "lead",
        })
        self.assertEqual(entries[PLUGIN_ID]["settings"], {"reflex_backend": "laya"})


if __name__ == "__main__":
    unittest.main()
