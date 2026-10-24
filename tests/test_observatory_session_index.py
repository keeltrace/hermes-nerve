"""Hermes-native session metadata: safe read-only title, model/cost and pagination."""
import sqlite3
import tempfile
import unittest
from pathlib import Path

from hermes_nerve.observatory import session_index as idx


class NativeSessionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        d = self.home / "profiles" / "myworker"
        d.mkdir(parents=True)
        self.db = d / "state.db"
        c = sqlite3.connect(self.db)
        columns = (
            "id TEXT,source TEXT,model TEXT,billing_provider TEXT,started_at REAL,ended_at REAL,"
            "message_count INTEGER,tool_call_count INTEGER,api_call_count INTEGER,"
            "input_tokens INTEGER,output_tokens INTEGER,cache_read_tokens INTEGER,"
            "cache_write_tokens INTEGER,reasoning_tokens INTEGER,estimated_cost_usd REAL,"
            "actual_cost_usd REAL,cost_status TEXT,cost_source TEXT,title TEXT,title_source TEXT,"
            "last_activity_at REAL,last_activity_description TEXT,last_activity_provenance TEXT,"
            "profile_name TEXT,compression_fallback_streak INTEGER"
        )
        c.execute("CREATE TABLE sessions("+columns+")")
        c.execute("""CREATE TABLE session_model_usage(
            session_id TEXT,model TEXT,billing_provider TEXT,api_call_count INTEGER,
            input_tokens INTEGER,output_tokens INTEGER,estimated_cost_usd REAL,
            actual_cost_usd REAL,cost_status TEXT,last_seen REAL)""")
        for i in range(35):
            title = "Fix PR task" if i != 34 else "Bearer SECRET-TOKEN-SHOULD-NOT-LEAK"
            c.execute("""INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (str(i), "cli", "model-beta", "nous", 1893400000+i, None,
                 4, 2, 3, 400, 60, 12, 0, 8, 0.033, None,
                 "estimated", "catalog", title, "user", 1893450000+i,
                 "Unit tests checked, no raw prompt", "agent.compression", "myworker", 1))
            c.execute("INSERT INTO session_model_usage VALUES(?,?,?,?,?,?,?,?,?,?)",
                      (str(i), "model-beta", "nous", 3, 400, 60, 0.033, None,
                       "estimated", 1893450000+i))
        c.commit()
        c.close()

    def tearDown(self):
        self.tmp.cleanup()

    def test_complete_native_metadata_but_no_raw_messages(self):
        response = idx.load_profile("myworker", home=self.home, limit=5)
        self.assertEqual(response["state"], "ok")
        self.assertEqual(response["total_sessions"], 35)
        self.assertEqual(response["shown"], 5)
        first = response["sessions"][0]
        self.assertEqual(first["title"], "[REDACTED SENSITIVE METADATA]")
        self.assertEqual(first["source"], "cli")
        self.assertEqual(first["tokens"]["input"], 400)
        self.assertEqual(first["cost"]["estimated_usd"], .033)
        self.assertFalse(first["cost"]["is_provider_invoice"] if "is_provider_invoice" in first["cost"] else False)
        self.assertEqual(first["models"][0]["provider"], "nous")
        self.assertEqual(first["compression_fallback_streak"], 1)
        self.assertNotIn("messages/content", str(response))
        self.assertNotIn("SECRET-TOKEN-SHOULD-NOT-LEAK", str(response))

    def test_lookback_pagination(self):
        a = idx.load_profile("myworker", home=self.home, limit=20, offset=0)
        b = idx.load_profile("myworker", home=self.home, limit=20, offset=20)
        self.assertEqual(a["shown"], 20)
        self.assertEqual(b["shown"], 15)
        self.assertTrue(set(x["id"] for x in a["sessions"]).isdisjoint(
            x["id"] for x in b["sessions"]))

    def test_invalid_profile_never_traverses_files(self):
        self.assertEqual(idx.load_profile("../../etc", home=self.home)["state"], "unknown")
        self.assertEqual(idx.load_profile("missing", home=self.home)["state"], "unknown")

    def test_no_secrets_or_unvalidated_text(self):
        self.assertEqual(idx._text("api_key=STOREDSECRET", 140), "[REDACTED SENSITIVE METADATA]")
        self.assertEqual(idx._text("hello\nworld", 20), "hello world")
        self.assertIsNone(idx._token("https://evil.com?secret=YES"))
        self.assertIsNone(idx._money(-2))

    def test_store_not_modified_by_reads(self):
        before = self.db.stat().st_mtime_ns
        idx.load_profile("myworker", home=self.home)
        self.assertEqual(self.db.stat().st_mtime_ns, before)


if __name__ == "__main__":
    unittest.main()
