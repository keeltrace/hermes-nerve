from __future__ import annotations

import unittest

from hermes_context_bus.projection import render_context_markdown
from hermes_context_bus.protocol import AGENT_IDS, CANONICAL_BOARD, require_agent_id
from hermes_context_bus.session import ContextSession


class FakeGateway:
    def __init__(self):
        self.calls = []

    def read_board(self, name, *, limit=100):
        self.calls.append(("read_board", name, limit))
        return {"board": name, "entries": []}

    def read_inbox(self, recipient, *, unread_only=True, limit=100):
        self.calls.append(("read_inbox", recipient, unread_only, limit))
        return {"recipient": recipient, "messages": []}

    def post(self, **kwargs):
        self.calls.append(("post", kwargs))
        return kwargs

    def send(self, **kwargs):
        self.calls.append(("send", kwargs))
        return kwargs

    def ack(self, **kwargs):
        self.calls.append(("ack", kwargs))
        return kwargs


class ProtocolTests(unittest.TestCase):
    def test_agent_ids_are_unique_and_exact(self):
        self.assertEqual(len(AGENT_IDS), 4)
        self.assertEqual(len(set(AGENT_IDS)), 4)
        self.assertEqual(
            set(AGENT_IDS),
            {
                "hermes-whatsapp",
                "hermes-discord",
                "hermes-cli",
                "hermes-desktop",
            },
        )

    def test_unknown_agent_rejected(self):
        with self.assertRaises(ValueError):
            require_agent_id("hermes-random")


class SessionTests(unittest.TestCase):
    def test_startup_reads_board_then_own_inbox(self):
        gateway = FakeGateway()
        session = ContextSession(gateway, "hermes-cli")
        context = session.startup(limit=25)
        self.assertEqual(context.agent_id, "hermes-cli")
        self.assertEqual(
            gateway.calls,
            [
                ("read_board", CANONICAL_BOARD, 25),
                ("read_inbox", "hermes-cli", True, 25),
            ],
        )

    def test_handoff_preserves_identity_and_board(self):
        gateway = FakeGateway()
        session = ContextSession(gateway, "hermes-desktop")
        out = session.handoff("Finished probe", "Observed current runtime.")
        self.assertEqual(out["board"], CANONICAL_BOARD)
        self.assertEqual(out["author"], "hermes-desktop")
        self.assertIn("handoff", out["tags"])

    def test_direct_send_rejects_non_hermes_recipient(self):
        gateway = FakeGateway()
        session = ContextSession(gateway, "hermes-whatsapp")
        with self.assertRaises(ValueError):
            session.send("random-worker", "hello")


class ProjectionTests(unittest.TestCase):
    def test_projection_is_deterministic_for_same_inputs(self):
        entries = [
            {
                "entry_id": "e2",
                "created_at": 2,
                "title": "Second",
                "author": "hermes-cli",
                "body": "two",
            },
            {
                "entry_id": "e1",
                "created_at": 1,
                "title": "First",
                "author": "hermes-whatsapp",
                "body": "one",
            },
        ]
        inboxes = {
            "hermes-cli": [
                {
                    "message_id": "m1",
                    "created_at": 3,
                    "from_id": "hermes-discord",
                    "body": "check this",
                    "thread_id": "t1",
                }
            ]
        }
        a = render_context_markdown(entries, inboxes, generated_at="fixed")
        b = render_context_markdown(entries, inboxes, generated_at="fixed")
        self.assertEqual(a, b)
        self.assertLess(a.index("### First"), a.index("### Second"))
        self.assertIn("Derived view only", a)
        self.assertIn("thread t1", a)


if __name__ == "__main__":
    unittest.main()