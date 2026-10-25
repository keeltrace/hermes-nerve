from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT


def load_plugin():
    name = "hcb_test_plugin"
    for key in list(sys.modules):
        if key == name or key.startswith(name + "."):
            del sys.modules[key]
    spec = importlib.util.spec_from_file_location(
        name,
        PLUGIN_DIR / "__init__.py",
        submodule_search_locations=[str(PLUGIN_DIR)],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class FakeContext:
    def __init__(self):
        self.hooks = {}
        self.tools = {}
        self.settings = {}
        self.mcp_calls = []
        self.posts = []
        self.acks = []

    def get_config(self, key, default=None):
        return self.settings.get(key, default)

    def register_hook(self, name, callback):
        self.hooks[name] = callback

    def register_tool(self, *, name, schema, handler, **kwargs):
        self.tools[name] = (schema, handler, kwargs)

    def call_mcp(self, server, tool, arguments=None, timeout=30):
        args = dict(arguments or {})
        self.mcp_calls.append((server, tool, args, timeout))
        if tool == "bb.board":
            data = {
                "board": "hermes-shared-context",
                "entries": [
                    {
                        "entry_id": "e1",
                        "title": "Shared decision",
                        "author": "hermes-cli",
                        "body": "Use the bus",
                    }
                ],
            }
        elif tool == "bb.inbox":
            data = {
                "recipient": args["recipient"],
                "messages": [
                    {
                        "message_id": "m1",
                        "from_id": "hermes-whatsapp",
                        "thread_id": "thread-1",
                        "body": "Please check this.",
                    }
                ],
            }
        elif tool == "bb.ack":
            self.acks.append(args)
            data = {"message_id": args["message_id"], "acked_at": 1}
        elif tool == "bb.post":
            self.posts.append(args)
            data = {"entry_id": f"e{len(self.posts) + 1}", **args}
        elif tool == "bb.send":
            data = {"message_id": "m2", "thread_id": args.get("thread_id") or "thread-2"}
        elif tool == "bb.thread":
            data = {"thread_id": args["thread_id"], "messages": []}
        else:
            raise AssertionError(tool)
        return {"ok": True, "result": {"ok": True, "data": data}}


class HermesPluginTests(unittest.TestCase):
    def setUp(self):
        self.plugin = load_plugin()
        self.ctx = FakeContext()
        self.plugin.register(self.ctx)

    def test_registers_hooks_and_six_tools(self):
        self.assertEqual(set(self.ctx.hooks), {"pre_llm_call", "post_llm_call"})
        self.assertEqual(
            set(self.ctx.tools),
            {
                "shared_context_post",
                "shared_context_send",
                "shared_context_inbox",
                "shared_context_thread",
                "shared_context_ack",
                "shared_context_health",
            },
        )

    def test_health_tool_reports_public_contract(self):
        _, handler, _ = self.ctx.tools["shared_context_health"]
        out = json.loads(handler({}, session_id="health"))
        self.assertTrue(out["success"])
        result = out["result"]
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["plugin"], "hermes-context-bus")
        self.assertEqual(result["version"], "0.2.0")
        self.assertEqual(result["authority"], "coordination-data-only")

    def test_pre_llm_reads_board_and_surface_inbox(self):
        out = self.ctx.hooks["pre_llm_call"](
            session_id="s1",
            turn_id="t1",
            platform="discord",
            user_message="hello",
        )
        self.assertIn("hermes-discord", out["context"])
        self.assertIn("Shared decision", out["context"])
        self.assertIn("Please check this.", out["context"])
        self.assertEqual(self.ctx.mcp_calls[0][1], "bb.board")
        self.assertEqual(self.ctx.mcp_calls[1][1], "bb.inbox")
        self.assertEqual(self.ctx.mcp_calls[1][2]["recipient"], "hermes-discord")

    def test_post_llm_auto_handoff_and_auto_ack(self):
        self.ctx.hooks["pre_llm_call"](
            session_id="s2", turn_id="t2", platform="whatsapp"
        )
        response = "Completed a meaningful task. " + ("x" * 300)
        self.ctx.hooks["post_llm_call"](
            session_id="s2",
            turn_id="t2",
            platform="whatsapp",
            user_message="Do the thing",
            assistant_response=response,
            model="test-model",
        )
        self.assertEqual(self.ctx.acks[-1]["recipient"], "hermes-whatsapp")
        self.assertEqual(self.ctx.acks[-1]["message_id"], "m1")
        self.assertEqual(self.ctx.posts[-1]["author"], "hermes-whatsapp")
        self.assertIn("Do the thing", self.ctx.posts[-1]["body"])
        self.assertLessEqual(len(self.ctx.posts[-1]["body"]), 2400)

    def test_short_response_does_not_auto_post(self):
        self.ctx.hooks["post_llm_call"](
            session_id="s3",
            turn_id="t3",
            platform="discord",
            user_message="ping",
            assistant_response="pong",
        )
        self.assertEqual(self.ctx.posts, [])

    def test_explicit_send_uses_session_surface_identity(self):
        self.ctx.hooks["pre_llm_call"](
            session_id="s4", turn_id="t4", platform="discord"
        )
        _, handler, _ = self.ctx.tools["shared_context_send"]
        raw = handler(
            {"to": "hermes-cli", "body": "Need a check"},
            session_id="s4",
        )
        parsed = json.loads(raw)
        self.assertTrue(parsed["success"])
        send_calls = [c for c in self.ctx.mcp_calls if c[1] == "bb.send"]
        self.assertEqual(send_calls[-1][2]["from_id"], "hermes-discord")
        self.assertEqual(send_calls[-1][2]["to"], "hermes-cli")

    def test_policy_denial_falls_back_to_shared_sqlite_across_surfaces(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            plugin = load_plugin()
            ctx = FakeContext()
            ctx.settings.update({
                "backend": "auto",
                "sqlite_path": str(Path(tmp) / "context.db"),
                "projection_path": str(Path(tmp) / "shared-context.md"),
                "auto_handoff_min_chars": 10,
            })

            def denied(server, tool, arguments=None, timeout=30):
                return {
                    "ok": True,
                    "result": {
                        "ok": False,
                        "error": {"code": "POLICY_DENIED", "message": "default deny"},
                    },
                }

            ctx.call_mcp = denied
            plugin.register(ctx)

            before = ctx.hooks["pre_llm_call"](
                session_id="wa-1", turn_id="wa-t1", platform="whatsapp"
            )
            self.assertIn("hermes-whatsapp", before["context"])

            ctx.hooks["post_llm_call"](
                session_id="wa-1",
                turn_id="wa-t1",
                platform="whatsapp",
                user_message="Record the decision",
                assistant_response="Decision recorded in the shared local context bus.",
                model="test",
            )

            seen = ctx.hooks["pre_llm_call"](
                session_id="dc-1", turn_id="dc-t1", platform="discord"
            )
            self.assertIn("Turn handoff from hermes-whatsapp", seen["context"])
            self.assertIn("hermes-discord", seen["context"])
            self.assertTrue((Path(tmp) / "shared-context.md").is_file())

    def test_all_four_surfaces_read_same_fallback_board(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            plugin = load_plugin()
            ctx = FakeContext()
            ctx.settings.update({
                "backend": "sqlite",
                "sqlite_path": str(Path(tmp) / "context.db"),
                "projection_path": str(Path(tmp) / "shared-context.md"),
                "auto_handoff_min_chars": 1,
            })
            plugin.register(ctx)

            plugin._CTX = ctx
            from importlib import import_module
            bridge = import_module(plugin.__package__ + ".bridge")
            bridge.Blackboard(ctx).post(
                "hermes-cli",
                "One shared fact",
                "All surfaces should see this.",
                ["test"],
            )

            cases = [
                ("whatsapp", "hermes-whatsapp"),
                ("discord", "hermes-discord"),
                ("cli", "hermes-cli"),
                ("desktop", "hermes-desktop"),
            ]
            for index, (platform, agent_id) in enumerate(cases):
                out = ctx.hooks["pre_llm_call"](
                    session_id=f"s-all-{index}",
                    turn_id=f"t-all-{index}",
                    platform=platform,
                )
                self.assertIn(agent_id, out["context"])
                self.assertIn("One shared fact", out["context"])

    def test_unknown_gateway_surface_fails_open_without_context(self):
        out = self.ctx.hooks["pre_llm_call"](
            session_id="s5", turn_id="t5", platform="telegram"
        )
        self.assertIsNone(out)


if __name__ == "__main__":
    unittest.main()