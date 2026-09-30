from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


class Ctx:
    def __init__(self, home: Path, extra=None):
        self.home = home
        self.extra = dict(extra or {})
        self.tools = {}
        self.hooks = []
        self.engine = None

    def get_config(self, key, default=None):
        values = {
            "work_supervision_db": str(self.home / "work.db"),
            "remote_data_dir": str(self.home / "remote"),
        }
        values.update(self.extra)
        return values.get(key, default)

    def register_tool(self, *, name, schema=None, handler=None, **kwargs):
        self.tools[name] = (schema, handler)

    def register_hook(self, name, callback):
        self.hooks.append((name, callback))

    def register_context_engine(self, engine):
        self.engine = engine


def load_plugin(tag: str):
    name = "profile_matrix_" + tag
    spec = importlib.util.spec_from_file_location(
        name, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)]
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


EXPECTED_ON = {
    "fat_cat": {
        "reflex", "nervous", "work_supervision", "token_trajectory",
        "action_gate", "context_governor", "remote_workers",
        "assistant_loops", "assistant_audit", "receipts", "local_learning",
    },
    "operator": {
        "reflex", "nervous", "action_gate", "context_governor",
        "receipts", "local_learning",
    },
    "lean": {"reflex", "nervous", "work_supervision", "token_trajectory", "receipts"},
    "marie_kondo": {"reflex", "work_supervision", "token_trajectory", "receipts"},
}

EXPECTED_TOOLS_NO_REMOTE = {
    "fat_cat": {
        "nerve_decide", "nerve_rank", "nerve_verify", "nerve_assess",
        "nerve_context_curate", "nerve_context_rehydrate", "nerve_stats",
        "nerve_nervous_event", "nerve_supervise_card", "nerve_work_event",
        "nerve_work_status",
    },
    "operator": {
        "nerve_decide", "nerve_rank", "nerve_verify", "nerve_assess",
        "nerve_context_curate", "nerve_context_rehydrate", "nerve_stats",
        "nerve_nervous_event",
    },
    "lean": {
        "nerve_decide", "nerve_stats", "nerve_nervous_event",
        "nerve_supervise_card", "nerve_work_event", "nerve_work_status",
    },
    "marie_kondo": {
        "nerve_decide", "nerve_rank", "nerve_verify", "nerve_assess",
        "nerve_stats", "nerve_supervise_card", "nerve_work_event", "nerve_work_status",
    },
}

REMOTE_TOOLS = {
    "nerve_remote_delegate_task", "nerve_remote_worker_status",
    "nerve_remote_worker_result", "nerve_remote_worker_cancel",
    "nerve_remote_worker_control",
}


class ProfileCapabilityMatrixTests(unittest.TestCase):
    def register(self, profile: str, extra=None, *, headless=False):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        env = {
            "HERMES_HOME": td.name,
            "HERMES_KANBAN_TASK": "matrix-task" if headless else "",
            "HERMES_KANBAN_TASK_ID": "matrix-task" if headless else "",
            "HERMES_NERVE_OFFLINE_VERIFY": "1",
        }
        env_patch = patch.dict(os.environ, env, clear=False)
        env_patch.start()
        self.addCleanup(env_patch.stop)
        mod = load_plugin(profile + ("_headless" if headless else ""))
        values = {"nerve_profile": profile, "remote_hosts": {}}
        values.update(extra or {})
        ctx = Ctx(Path(td.name), values)
        if headless:
            with patch.object(mod.work_hooks, "bootstrap_kanban_worker", return_value=object()):
                mod.register(ctx)
        else:
            mod.register(ctx)
        return mod, ctx

    def test_declared_module_matrix(self):
        for profile, expected in EXPECTED_ON.items():
            with self.subTest(profile=profile):
                mod, ctx = self.register(profile)
                policy = mod.resolve_config(ctx.get_config)
                actual = {name for name, enabled in policy.modules.items() if enabled}
                # Fat Cat remote workers are intentionally conditional on hosts.
                if profile == "fat_cat":
                    expected = expected - {"remote_workers"}
                self.assertEqual(actual, expected)

    def test_exact_tool_surface_without_remote_hosts(self):
        for profile, expected in EXPECTED_TOOLS_NO_REMOTE.items():
            with self.subTest(profile=profile):
                _, ctx = self.register(profile)
                self.assertEqual(set(ctx.tools), expected)

    def test_runtime_module_activation_matches_profile_contract(self):
        for profile in EXPECTED_ON:
            with self.subTest(profile=profile):
                mod, _ = self.register(profile)
                self.assertEqual(mod.nervous._default._config.enabled, profile in {"fat_cat", "operator", "lean"})
                self.assertEqual(mod.work_runtime.enabled(), profile in {"fat_cat", "lean", "marie_kondo"})
                self.assertEqual(mod.gate.gate_mode(), "advisory" if profile in {"fat_cat", "operator"} else "off")
                self.assertEqual(mod.assistant._loops_enabled, profile == "fat_cat")
                self.assertEqual(mod.assistant._audit_enabled, profile == "fat_cat")
                self.assertEqual(mod.assistant.enabled(), profile == "fat_cat")
                self.assertEqual(mod.assistant.audit_enabled(), profile == "fat_cat")
                self.assertTrue(mod.receipts.enabled())
                self.assertEqual(mod.ledger.enabled(), profile in {"fat_cat", "operator"})
                self.assertEqual(mod.nervous._default._config.local_learning, profile in {"fat_cat", "operator"})
                self.assertEqual(
                    mod.work_runtime.settings()["nerve_observer_enabled"],
                    profile in {"fat_cat", "lean", "marie_kondo"},
                )

    def test_context_engine_presence_matches_profile_contract(self):
        for profile in EXPECTED_ON:
            with self.subTest(profile=profile):
                _, ctx = self.register(profile)
                self.assertEqual(ctx.engine is not None, profile in {"fat_cat", "operator"})

    def test_fat_cat_remote_tools_appear_only_when_hosts_configured(self):
        _, empty = self.register("fat_cat", {"remote_hosts": {}})
        self.assertTrue(REMOTE_TOOLS.isdisjoint(empty.tools))
        _, configured = self.register(
            "fat_cat",
            {"remote_hosts": {"lab": {"host": "example.invalid", "user": "tester"}}},
        )
        self.assertTrue(REMOTE_TOOLS.issubset(configured.tools))

    def test_headless_work_profiles_export_zero_tool_schemas(self):
        for profile in ("fat_cat", "lean", "marie_kondo"):
            with self.subTest(profile=profile):
                mod, ctx = self.register(profile, headless=True)
                self.assertEqual(ctx.tools, {})
                self.assertEqual(mod.gate.gate_mode(), "off")
                names = {name for name, _ in ctx.hooks}
                self.assertIn("pre_verify", names)
                self.assertIn("post_api_request", names)
                self.assertIn("api_request_error", names)

    def test_operator_headless_marker_does_not_invent_work_supervision(self):
        mod, ctx = self.register("operator", headless=True)
        self.assertEqual(mod.gate.gate_mode(), "off")
        self.assertNotIn("post_api_request", {name for name, _ in ctx.hooks})
        self.assertNotIn("api_request_error", {name for name, _ in ctx.hooks})

    def test_dependency_pruning_is_fail_closed(self):
        mod, ctx = self.register(
            "lean",
            {"nerve_modules": {"work_supervision": False, "token_trajectory": True}},
        )
        policy = mod.resolve_config(ctx.get_config)
        self.assertFalse(policy.enabled("work_supervision"))
        self.assertFalse(policy.enabled("token_trajectory"))

        mod2, ctx2 = self.register(
            "operator",
            {"nerve_modules": {"assistant_loops": False, "assistant_audit": True}},
        )
        policy2 = mod2.resolve_config(ctx2.get_config)
        self.assertFalse(policy2.enabled("assistant_audit"))

        mod3, ctx3 = self.register(
            "operator",
            {"nerve_modules": {"nervous": False, "local_learning": True}},
        )
        policy3 = mod3.resolve_config(ctx3.get_config)
        self.assertFalse(policy3.enabled("nervous"))
        self.assertFalse(policy3.enabled("local_learning"))
        self.assertFalse(mod3.nervous._default._config.local_learning)

    def test_legacy_compatible_disable_flags_update_policy_and_dependents(self):
        mod, ctx = self.register("operator", {"nervous_enabled": False})
        policy = mod.resolve_config(ctx.get_config)
        self.assertFalse(policy.enabled("nervous"))
        self.assertEqual(policy.reasons["nervous"], "nervous_enabled=false")
        self.assertFalse(policy.enabled("local_learning"))
        self.assertEqual(policy.reasons["local_learning"], "requires nervous")
        self.assertFalse(mod.nervous._default._config.enabled)
        self.assertFalse(mod.nervous._default._config.local_learning)

        mod2, ctx2 = self.register("fat_cat", {"work_supervision_enabled": False})
        policy2 = mod2.resolve_config(ctx2.get_config)
        self.assertFalse(policy2.enabled("work_supervision"))
        self.assertEqual(policy2.reasons["work_supervision"], "work_supervision_enabled=false")
        self.assertFalse(policy2.enabled("token_trajectory"))
        self.assertEqual(policy2.reasons["token_trajectory"], "requires work_supervision")

    def test_fat_cat_remote_reason_matches_conditional_runtime(self):
        mod, ctx = self.register("fat_cat", {"remote_hosts": {}})
        policy = mod.resolve_config(ctx.get_config)
        self.assertFalse(policy.enabled("remote_workers"))
        self.assertEqual(policy.reasons["remote_workers"], "no remote_hosts configured")

    def test_explicit_runtime_overrides_win_over_profile_defaults(self):
        mod, _ = self.register(
            "operator",
            {"gate_mode": "off", "nervous_max_provider_calls_per_turn": 7},
        )
        self.assertEqual(mod.gate.gate_mode(), "off")
        self.assertEqual(mod.nervous._default._config.max_provider_calls_per_turn, 7)

        lean, _ = self.register(
            "lean",
            {"nervous_turn_admission": True, "nervous_max_provider_calls_per_turn": 21},
        )
        self.assertTrue(lean.nervous._default._config.admission_enabled)
        self.assertEqual(lean.nervous._default._config.max_provider_calls_per_turn, 21)

    def test_explicit_action_gate_module_override_is_not_inert(self):
        for profile in ("lean", "marie_kondo"):
            with self.subTest(profile=profile):
                mod, ctx = self.register(
                    profile,
                    {"nerve_modules": {"action_gate": True}},
                )
                policy = mod.resolve_config(ctx.get_config)
                self.assertTrue(policy.enabled("action_gate"))
                self.assertEqual(mod.gate.gate_mode(), "advisory")

        mod, ctx = self.register(
            "lean",
            {"nerve_modules": {"action_gate": True}, "gate_mode": "off"},
        )
        self.assertTrue(mod.resolve_config(ctx.get_config).enabled("action_gate"))
        self.assertEqual(mod.gate.gate_mode(), "off")

    def test_null_gate_mode_does_not_silently_disable_enabled_profile_gate(self):
        mod, ctx = self.register("fat_cat", {"gate_mode": None})
        self.assertTrue(mod.resolve_config(ctx.get_config).enabled("action_gate"))
        self.assertEqual(mod.gate.gate_mode(), "advisory")

    def test_lean_economic_defaults_are_profile_specific(self):
        lean, _ = self.register("lean")
        self.assertFalse(lean.nervous._default._config.admission_enabled)
        self.assertEqual(lean.nervous._default._config.max_provider_calls_per_turn, 12)
        self.assertEqual(lean.nervous._default._config.event_preview_chars, 800)
        self.assertEqual(lean.nervous._default._config.retain_recent_events, 32)

        operator, _ = self.register("operator")
        self.assertTrue(operator.nervous._default._config.admission_enabled)
        self.assertEqual(operator.nervous._default._config.max_provider_calls_per_turn, 96)


if __name__ == "__main__":
    unittest.main()
