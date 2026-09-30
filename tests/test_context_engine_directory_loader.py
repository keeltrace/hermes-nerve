"""Hermes' context-engine directory loader must never obtain a Nerve engine.

When ``context.engine`` names a user plugin directory, Hermes imports that directory
through ``plugins/context_engine/__init__.py`` and extracts an engine with
``plugins/plugin_loader.py::instance_from_module``: it calls ``register(collector)``
with a collector that has no ``get_config``, and if nothing was registered it
instantiates any ``ContextEngine`` subclass found on the module. An engine obtained
that way can see no Nerve setting and lives in a second, unconfigured module tree.
Nerve's configured engine is registered as ``jev`` through the plugin system instead.
"""
from __future__ import annotations

import contextlib
import importlib.util
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


class DirectoryLoaderCollector:
    """Shape of Hermes' ``_EngineCollector``: no-op registrations and no ``get_config``."""

    def __init__(self):
        self.engine = None

    def _noop(self, *args, **kwargs):
        pass

    register_tool = register_hook = register_cli_command = register_memory_provider = register_command = _noop

    def register_context_engine(self, engine):
        self.engine = engine


def load_plugin_root(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def is_context_engine_class(value):
    # Hermes checks issubclass(value, ContextEngine). Nerve's base is Hermes' class in a
    # host and a local stand-in otherwise, so match the base by name in both trees.
    return isinstance(value, type) and any(base.__name__ == "ContextEngine" for base in value.__mro__[1:])


def directory_loader_instance(module, collector):
    """Extraction order of Hermes ``instance_from_module``."""
    if hasattr(module, "register"):
        try:
            module.register(collector)
            if collector.engine:
                return collector.engine
        except Exception:
            pass
    for name in dir(module):
        value = getattr(module, name, None)
        if is_context_engine_class(value):
            with contextlib.suppress(Exception):
                return value()
    return None


class DirectoryLoaderTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {"HERMES_KANBAN_TASK": "", "HERMES_KANBAN_TASK_ID": ""}, clear=False)
        env.start()
        self.addCleanup(env.stop)

    def test_directory_loader_obtains_no_engine(self):
        module = load_plugin_root("nerve_directory_loader_engine")
        self.assertIsNone(directory_loader_instance(module, DirectoryLoaderCollector()))

    def test_register_without_plugin_settings_points_to_jev(self):
        module = load_plugin_root("nerve_directory_loader_warning")
        collector = DirectoryLoaderCollector()
        with self.assertLogs("hermes_nerve", level="WARNING") as logs:
            module.register(collector)
        self.assertIsNone(collector.engine)
        self.assertTrue(any("context.engine" in line and "'jev'" in line for line in logs.output), logs.output)


if __name__ == "__main__":
    unittest.main()
