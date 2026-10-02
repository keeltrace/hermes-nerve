from __future__ import annotations

import unittest

from hermes_context_bus.context_view import render_agent_context
from hermes_context_bus.identity import resolve_agent_id


class IdentityTests(unittest.TestCase):
    def test_gateway_platform_mapping(self):
        self.assertEqual(resolve_agent_id("whatsapp", environ={}), "hermes-whatsapp")
        self.assertEqual(resolve_agent_id("discord", environ={}), "hermes-discord")
        self.assertEqual(resolve_agent_id("cli", environ={}), "hermes-cli")
        self.assertEqual(resolve_agent_id("desktop", environ={}), "hermes-desktop")

    def test_desktop_terminal_overrides_cli(self):
        self.assertEqual(
            resolve_agent_id("cli", environ={"HERMES_DESKTOP_TERMINAL": "1"}),
            "hermes-desktop",
        )
        self.assertEqual(
            resolve_agent_id("tui", environ={"HERMES_DESKTOP": "true"}),
            "hermes-desktop",
        )

    def test_explicit_override_is_bounded_to_known_ids(self):
        self.assertEqual(
            resolve_agent_id("discord", environ={}, override="hermes-cli"),
            "hermes-cli",
        )
        self.assertIsNone(
            resolve_agent_id("discord", environ={}, override="other-agent")
        )


class ContextViewTests(unittest.TestCase):
    def test_context_contains_board_inbox_and_authority_warning(self):
        out = render_agent_context(
            agent_id="hermes-discord",
            board={
                "entries": [
                    {"title": "Decision", "author": "hermes-cli", "body": "Use bb.*"}
                ]
            },
            inbox={
                "messages": [
                    {
                        "from_id": "hermes-whatsapp",
                        "body": "Check logs",
                        "thread_id": "t1",
                    }
                ]
            },
            max_chars=3500,
        )
        self.assertIn("Decision", out)
        self.assertIn("Check logs", out)
        self.assertIn("not authority", out.lower())

    def test_context_is_bounded(self):
        out = render_agent_context(
            agent_id="hermes-cli",
            board={
                "entries": [
                    {"title": "x", "author": "y", "body": "z" * 5000}
                ]
            },
            inbox={"messages": []},
            max_chars=600,
        )
        self.assertLessEqual(len(out), 600)


if __name__ == "__main__":
    unittest.main()