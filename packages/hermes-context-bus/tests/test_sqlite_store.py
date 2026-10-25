from __future__ import annotations

import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path


def load_store_module():
    import importlib.util

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "hcb_sqlite_store_test",
        root / "sqlite_store.py",
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class SQLiteStoreTests(unittest.TestCase):
    def setUp(self):
        self.mod = load_store_module()
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.db = root / "context.db"
        self.projection = root / "shared-context.md"
        self.store = self.mod.SQLiteBlackboard(self.db, self.projection)

    def tearDown(self):
        self.tmp.cleanup()

    def test_persists_board_and_projection_across_reopen(self):
        self.store.post(
            board="hermes-shared-context",
            title="Decision",
            body="Use the local forum",
            author="hermes-cli",
            tags=["handoff"],
        )
        reopened = self.mod.SQLiteBlackboard(self.db, self.projection)
        board = reopened.board("hermes-shared-context")
        self.assertEqual(board["count"], 1)
        self.assertEqual(board["entries"][0]["body"], "Use the local forum")
        text = self.projection.read_text()
        self.assertIn("Decision", text)
        self.assertIn("hermes-cli", text)

    def test_direct_message_thread_and_ack(self):
        sent = self.store.send(
            to="hermes-discord",
            body="Please inspect the logs",
            from_id="hermes-whatsapp",
        )
        inbox = self.store.inbox("hermes-discord", unread_only=True)
        self.assertEqual(inbox["unread"], 1)
        self.assertEqual(inbox["messages"][0]["message_id"], sent["message_id"])
        thread = self.store.thread(sent["thread_id"], "hermes-discord")
        self.assertEqual(thread["count"], 1)
        ack = self.store.ack("hermes-discord", sent["message_id"])
        self.assertFalse(ack["already_acked"])
        self.assertEqual(
            self.store.inbox("hermes-discord", unread_only=True)["unread"],
            0,
        )

    def test_two_surfaces_continue_one_thread(self):
        first = self.store.send(
            to="hermes-discord",
            body="Question from WhatsApp",
            from_id="hermes-whatsapp",
        )
        self.store.send(
            to="hermes-whatsapp",
            body="Reply from Discord",
            from_id="hermes-discord",
            thread_id=first["thread_id"],
        )
        thread = self.store.thread(first["thread_id"])
        self.assertEqual(thread["count"], 2)
        self.assertEqual(
            [m["from_id"] for m in thread["messages"]],
            ["hermes-whatsapp", "hermes-discord"],
        )

    def test_board_entries_are_immutable(self):
        entry = self.store.post(
            board="hermes-shared-context",
            title="Immutable",
            body="original",
            author="hermes-cli",
        )
        with sqlite3.connect(self.db) as conn:
            with self.assertRaises(sqlite3.DatabaseError):
                conn.execute(
                    "UPDATE context_entries SET body='changed' WHERE entry_id=?",
                    (entry["entry_id"],),
                )

    def test_concurrent_posts_do_not_corrupt_store(self):
        errors = []

        def worker(index):
            try:
                self.store.post(
                    board="hermes-shared-context",
                    title=f"post-{index}",
                    body=f"body-{index}",
                    author="hermes-cli",
                )
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(24)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(errors, [])
        board = self.store.board("hermes-shared-context", limit=100)
        self.assertEqual(board["count"], 24)
        self.assertEqual(len({e["entry_id"] for e in board["entries"]}), 24)


if __name__ == "__main__":
    unittest.main()