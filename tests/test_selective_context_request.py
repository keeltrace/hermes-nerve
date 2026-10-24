"""Real installed Hermes dispatch -> request middleware -> provider-bound capture.

Run with Hermes' interpreter (which has its dependencies). No model/network calls.
Selectors and the Hermes middleware dispatcher are not mocked.
"""
import copy
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from hermes_nerve import consumer, experience
from hermes_nerve.context_integration import register_context_consumers


def tool(name, description):
    return {"type": "function", "function": {"name": name, "description": description,
            "parameters": {"type": "object", "properties": {}}}}


try:
    HOST_AVAILABLE = importlib.util.find_spec("hermes_cli.middleware") is not None
except ModuleNotFoundError:
    HOST_AVAILABLE = False


@unittest.skipUnless(HOST_AVAILABLE, "Run with the installed Hermes interpreter for native request integration")
class RequestIntegration(unittest.TestCase):
    def setUp(self):
        from hermes_cli.plugins import PluginManager, PluginContext, PluginManifest
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = patch.dict(os.environ, {"HERMES_HOME": self.tmp.name})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.manager = PluginManager(scope_key=self.tmp.name)
        self.ctx = PluginContext(PluginManifest(name="nerve"), self.manager)
        self.settings = {"selective_context_enabled": True,
                         "selective_context_tools_max_admitted": 1,
                         "selective_context_skills_max_admitted": 1}
        self.scope = {"home": self.tmp.name, "session": "s1", "user": "alice"}
        self.index = ("MANDATORY POLICY\n<available_skills>\n"
                      "    - hermes-agent: Hermes core operation\n"
                      "    - github-review: Review GitHub pull request code\n"
                      "    - recipes: Cook dinner from recipes\n"
                      "</available_skills>\nMANDATORY TAIL")
        from tools.tool_search import bridge_tool_schemas
        self.bridge = bridge_tool_schemas(2, listing=(
            "Deferred tool catalog (call schemas via `tool_describe`, invoke via `tool_call`):\n"
            "other tools (2):\n- github_pr_review: Review GitHub pull request code\n"
            "- dinner_recipes: Cook dinner from recipes"), listing_form="full")
        self.request = {"model": "offline-test", "messages": [
            {"role": "system", "content": self.index},
            {"role": "user", "content": "Review GitHub pull request code"}],
            "tools": [tool("terminal", "Core terminal must survive"), *self.bridge]}

    def capture(self, request):
        from hermes_cli.middleware import run_llm_execution_middleware
        from agent.turn_api_request import build_api_request
        from types import SimpleNamespace
        # Real Hermes request builder, sanitizer, request middleware and execution
        # dispatch. Only provider kwargs construction and the final transport are
        # test adapters; no selector or middleware behavior is replaced.
        agent = SimpleNamespace(
            tools=request["tools"], session_id="s1", platform="discord", model="offline-test",
            provider="custom", base_url="http://127.0.0.1:9/v1", api_mode="chat_completions",
            max_tokens=64, _use_prompt_caching=False, _empty_content_retries=0,
            _image_rejecting_models=set(), _force_ascii_payload=False,
            _reset_stream_delivery_tracking=lambda: None,
            _reapply_reasoning_echo_for_provider=lambda messages: None,
            _build_api_kwargs=lambda messages, **kw: copy.deepcopy(request),
        )
        with patch("hermes_cli.plugins._delivery_manager", return_value=self.manager):
            result = build_api_request(agent, api_messages=request.get("messages", request.get("input", [])),
                _moa_prepared_request=None, tools_for_api=request["tools"], system_message=None,
                messages=[], original_user_message="Review GitHub pull request code",
                approx_tokens=100, total_chars=400, retry_count=0, api_call_count=1,
                api_request_id="r1", api_start_time=0, effective_task_id="t1", turn_id="turn1")
            captured = run_llm_execution_middleware(result.api_kwargs, lambda kwargs: copy.deepcopy(kwargs))
        evidence = os.environ.get("NERVE_CONTEXT_EVIDENCE_DIR")
        if evidence:
            import json
            folder = Path(evidence)
            folder.mkdir(parents=True, exist_ok=True)
            self._capture_count = getattr(self, "_capture_count", 0) + 1
            (folder / f"{self._testMethodName}-{self._capture_count}.json").write_text(
                json.dumps({"stock": request, "model_bound": captured}, indent=2, ensure_ascii=False))
        return captured

    def register(self):
        register_context_consumers(self.ctx, lambda: self.settings)

    def test_on_applies_tools_and_skills_to_model_payload(self):
        self.register()
        before = copy.deepcopy(self.request)
        captured = self.capture(self.request)
        system = captured["messages"][0]["content"]
        self.assertIn("github-review: Review GitHub", system)
        self.assertNotIn("recipes: Cook dinner", system)
        self.assertIn("    - recipes\n", system)
        self.assertIn("MANDATORY POLICY", system)
        self.assertIn("MANDATORY TAIL", system)
        self.assertIn("hermes-agent: Hermes core", system)
        descriptions = "\n".join(t["function"]["description"] for t in captured["tools"])
        self.assertIn("github_pr_review: Review GitHub", descriptions)
        self.assertNotIn("dinner_recipes: Cook dinner", descriptions)
        self.assertIn("dinner_recipes", descriptions)
        self.assertEqual(captured["tools"][0], before["tools"][0])
        from tools.tool_search import BRIDGE_TOOL_NAMES
        self.assertTrue(BRIDGE_TOOL_NAMES.issubset({t["function"]["name"] for t in captured["tools"]}))
        self.assertEqual(self.request, before, "Never mutate stock request/history")

    def test_off_and_explicit_shadow_leave_exact_payload(self):
        self.register()
        for mode in ("off", "shadow"):
            with self.subTest(mode=mode):
                self.settings.update({f"selective_context_{s}_mode": mode for s in ("memory", "tools", "skills")})
                self.assertEqual(self.capture(self.request), self.request)
        self.settings["selective_context_enabled"] = False
        self.settings.update({f"selective_context_{s}_mode": "apply" for s in ("memory", "tools", "skills")})
        self.assertEqual(self.capture(self.request), self.request)

    def test_memory_is_selected_before_real_host_string_injection(self):
        self.register()
        store = experience.ExperienceStore(Path(self.tmp.name) / "experience.jsonl")
        self.settings["selective_context_memory_max_admitted"] = 1
        store.create_claim(subject="GitHub review", statement="GitHub pull request code review uses normalized header matching",
                           claim_type="verified", evidence_ids=("test-green",), confidence=.99,
                           scope={"user": "alice"}, admit=False)
        store.create_claim(subject="GitHub review archive", statement="Recipe archives contain dinner menus",
                           claim_type="verified", evidence_ids=("archive-evidence",), confidence=.99,
                           scope={"user": "alice"}, admit=False)
        store.create_claim(subject="GitHub review rule", statement="Do not publish GitHub review without approval",
                           claim_type="disproven", evidence_ids=("rule-evidence",), confidence=.99,
                           scope={"user": "alice"}, admit=False)
        experience.configure(enabled=True)
        self.addCleanup(experience.configure, enabled=False)
        from types import SimpleNamespace
        from agent.turn_context import _collect_pre_llm_call_context
        agent = SimpleNamespace(session_id="s1", model="offline-test", platform="discord", _user_id="alice")
        with patch.object(experience, "default_store", return_value=store), patch(
                "hermes_cli.plugins._delivery_manager", return_value=self.manager):
            text = _collect_pre_llm_call_context(agent, effective_task_id="t1", turn_id="turn1",
                        original_user_message="Review GitHub pull request code", messages=[], conversation_history=[])
            self.assertIn("normalized header matching", text)
            self.assertIn("Do not publish", text)
            self.assertNotIn("dinner menus", text)
            self.assertNotIn("'candidates':", text, "Host must receive selected text, not repr(envelope)")
            request = copy.deepcopy(self.request)
            request["messages"][-1]["content"] += "\n" + text
            self.assertIn("normalized header matching", str(self.capture(request)))
            for mode in ("off", "shadow"):
                self.settings["selective_context_memory_mode"] = mode
                text = _collect_pre_llm_call_context(agent, effective_task_id="t1", turn_id="turn1",
                        original_user_message="Review GitHub pull request code", messages=[], conversation_history=[])
                self.assertEqual(text, "")

    def test_actual_plugin_entrypoint_registers_and_reloads_modes(self):
        import sys
        from hermes_nerve.modules import MODULES
        root = Path(__file__).resolve().parents[1]
        spec = importlib.util.spec_from_file_location("nerve_request_candidate", root / "__init__.py",
                                                     submodule_search_locations=[str(root)])
        self.assertIsNotNone(spec)
        assert spec is not None and spec.loader is not None
        entry = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = entry
        self.addCleanup(sys.modules.pop, spec.name, None)
        spec.loader.exec_module(entry)
        active = {"selective_context", "selective_context_memory", "selective_context_tools",
                  "selective_context_skills", "verified_experience"}
        config = {"nerve_profile": "custom", "nerve_modules": {name: name in active for name in MODULES},
                  **self.settings}
        with patch.object(self.ctx, "get_config", side_effect=lambda k, d=None: config.get(k, d)):
            entry.register(self.ctx)
            self.assertIn("llm_request", self.manager._middleware)
            self.assertNotIn("recipes: Cook dinner", self.capture(self.request)["messages"][0]["content"])
            config["selective_context_enabled"] = False
            self.assertEqual(self.capture(self.request), self.request)

    def test_native_schema_selection_preserves_core_and_deferred_access(self):
        self.register()
        from tools.registry import registry
        from tools.tool_search import dispatch_tool_describe
        definitions = [tool("test_github_review", "Review GitHub pull request code"),
                       tool("test_dinner_recipes", "Cook dinner from recipes")]
        for td in definitions:
            name = td["function"]["name"]
            registry.register(name=name, toolset="mcp-selective-fixture", schema=td,
                              handler=lambda args, **kw: {"fixture": True})
            self.addCleanup(registry.deregister, name)
        request = copy.deepcopy(self.request)
        request["tools"].extend(definitions)
        captured = self.capture(request)
        names = {t["function"]["name"] for t in captured["tools"]}
        self.assertIn("terminal", names)
        self.assertIn("test_github_review", names)
        self.assertNotIn("test_dinner_recipes", names)
        import json
        described = json.loads(dispatch_tool_describe({"names": ["test_dinner_recipes"]},
                                                      current_tool_defs=request["tools"]))
        self.assertIn("test_dinner_recipes", described["tools"])

    def test_deferred_skill_still_loads_via_native_skill_view(self):
        self.register()
        root = Path(self.tmp.name) / "skills" / "recipes"
        root.mkdir(parents=True)
        (root / "SKILL.md").write_text("---\nname: recipes\ndescription: Cook dinner from recipes\n---\nFixture recipe content\n")
        captured = self.capture(self.request)
        self.assertNotIn("recipes: Cook dinner", captured["messages"][0]["content"])
        from tools.skills_tool import skill_view
        self.assertIn("Fixture recipe content", skill_view("recipes"))

    def test_compact_deferred_catalog_is_actively_focused_and_stays_discoverable(self):
        self.register()
        request = copy.deepcopy(self.request)
        for td in request["tools"]:
            fn = td["function"]
            fn["description"] = fn["description"].replace(
                "- github_pr_review: Review GitHub pull request code\n- dinner_recipes: Cook dinner from recipes",
                "github_pr_review, dinner_recipes")
        captured = self.capture(request)
        descriptions = "\n".join(t["function"]["description"] for t in captured["tools"])
        self.assertIn("Nerve relevant deferred tools: github_pr_review", descriptions)
        self.assertIn("github_pr_review, dinner_recipes", descriptions)

    def test_invalid_settings_and_mixed_modes_fail_open_per_surface(self):
        self.register()
        self.settings["selective_context_tools_mode"] = "shadow"
        result = self.capture(self.request)
        self.assertEqual(result["tools"], self.request["tools"])
        self.assertNotIn("recipes: Cook dinner", result["messages"][0]["content"])
        self.settings["selective_context_skills_mode"] = "invalid-mode"
        self.assertEqual(self.capture(self.request), self.request)

    def test_anthropic_and_responses_prompt_shapes(self):
        self.register()
        for key in ("system", "instructions"):
            request = copy.deepcopy(self.request)
            request[key] = request["messages"].pop(0)["content"]
            if key == "instructions":
                request["input"] = request.pop("messages")
            result = self.capture(request)
            self.assertNotIn("recipes: Cook dinner", result[key])
            self.assertIn("MANDATORY TAIL", result[key])

    def test_missing_discovery_or_unknown_shape_fails_open(self):
        self.register()
        request = copy.deepcopy(self.request)
        request["tools"] = [tool("terminal", "Core terminal must survive"), tool("unknown", "Unknown")]
        self.assertEqual(self.capture(request)["tools"], request["tools"])
        request["messages"] = [{"role": "user", "content": [{"type": "image", "image": "opaque"}]}]
        self.assertEqual(self.capture(request), request)


if __name__ == "__main__":
    unittest.main()
