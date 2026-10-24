from __future__ import annotations

import unittest

from hermes_context_bus.mcp_gateway import (
    MCPConfig,
    MegaMCPBlackboardGateway,
    SharedContextError,
)


class MCPGatewayTests(unittest.TestCase):
    def setUp(self):
        self.calls = []

        def fake(server, tool, args, timeout):
            self.calls.append((server, tool, args, timeout))
            payloads = {
                "bb.board": {"board": "hermes-shared-context", "entries": [{"entry_id": "e1"}]},
                "bb.inbox": {"recipient": "hermes-cli", "messages": [{"message_id": "m1"}]},
                "bb.post": {"entry_id": "e2"},
                "bb.send": {"message_id": "m2", "thread_id": "t1"},
                "bb.ack": {"message_id": "m1", "acked_at": 1},
                "bb.thread": {"thread_id": "t1", "messages": []},
            }
            return {"ok": True, "result": {"ok": True, "data": payloads[tool]}}

        self.gateway = MegaMCPBlackboardGateway(
            fake, config=MCPConfig(server="megamcp", timeout=7)
        )

    def test_reads_use_expected_tools(self):
        board = self.gateway.read_board("hermes-shared-context", limit=9)
        inbox = self.gateway.read_inbox("hermes-cli", limit=8)
        self.assertEqual(board["entries"][0]["entry_id"], "e1")
        self.assertEqual(inbox["messages"][0]["message_id"], "m1")
        self.assertEqual(self.calls[0][1], "bb.board")
        self.assertEqual(self.calls[1][1], "bb.inbox")
        self.assertEqual(self.calls[0][3], 7)

    def test_writes_and_thread(self):
        self.assertEqual(
            self.gateway.post(
                board="hermes-shared-context",
                title="x",
                body="y",
                author="hermes-cli",
                tags=["handoff"],
            )["entry_id"],
            "e2",
        )
        self.assertEqual(
            self.gateway.send(
                to="hermes-discord",
                body="hello",
                from_id="hermes-cli",
            )["thread_id"],
            "t1",
        )
        self.assertEqual(
            self.gateway.ack(recipient="hermes-cli", message_id="m1")["message_id"],
            "m1",
        )
        self.assertEqual(self.gateway.thread("t1")["thread_id"], "t1")

    def test_policy_denial_is_not_hidden(self):
        def denied(*_):
            return {
                "ok": True,
                "result": {
                    "ok": False,
                    "error": {"code": "POLICY_DENIED", "message": "default deny"},
                },
            }

        gateway = MegaMCPBlackboardGateway(denied)
        with self.assertRaisesRegex(SharedContextError, "POLICY_DENIED"):
            gateway.read_board("hermes-shared-context")


if __name__ == "__main__":
    unittest.main()