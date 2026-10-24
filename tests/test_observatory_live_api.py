"""Tests for the bearer-protected GET /api/live endpoint (fixed root, no leaks)."""
from __future__ import annotations

import http.client
import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from hermes_nerve.observatory import live_reader, server
from hermes_nerve.paths import hermes_home

GOOD_TOKEN = "test-token-0123456789abcdefghijklmnop"
SCHEMA = "live-reader-snapshot/v1"


def _no_sidecar(url: str, timeout: float):
    raise OSError("sidecar disabled in tests")


class LiveApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.store_path = self.dir / "work-supervision.sqlite3"
        self.shadow_path = self.dir / "shadow.jsonl"
        self.live_root = self.dir / "live-missing"  # does not exist => unknown feed
        # Keep tests hermetic: never reach the real loopback host sidecar.
        self._patch = mock.patch.object(live_reader, "_http_get", _no_sidecar)
        self._patch.start()
        self.srv = server.make_server(
            "127.0.0.1", 0, store_path=self.store_path, shadow_path=self.shadow_path,
            token=GOOD_TOKEN, history_dir=self.dir / "history",
        )
        self.srv.live_root = self.live_root
        self.port = self.srv.server_address[1]
        self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.thread.join(timeout=5)
        self._patch.stop()
        self.tmp.cleanup()

    def _get(self, path: str, headers: dict | None = None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request("GET", path, headers=headers or {})
            resp = conn.getresponse()
            body = resp.read()
            return resp.status, {k.lower(): v for k, v in resp.getheaders()}, body
        finally:
            conn.close()

    def _auth(self, token: str = GOOD_TOKEN) -> dict:
        return {"Authorization": "Bearer " + token}

    def test_missing_token_is_401(self):
        status, headers, _ = self._get("/api/live")
        self.assertEqual(status, 401)
        self.assertIn("bearer", headers.get("www-authenticate", "").lower())

    def test_wrong_token_is_401(self):
        status, _, body = self._get("/api/live", self._auth("wrong-token-0123456789abcdefghijk"))
        self.assertEqual(status, 401)
        self.assertNotIn(b"schema", body)

    def test_basic_scheme_is_401(self):
        status, _, _ = self._get("/api/live", {"Authorization": "Basic Zm9vOmJhcg=="})
        self.assertEqual(status, 401)

    def test_nonloopback_host_is_421_even_with_valid_token(self):
        headers = dict(self._auth())
        headers["Host"] = "evil.example.com"
        status, _, body = self._get("/api/live", headers)
        self.assertEqual(status, 421)
        self.assertNotIn(b"schema", body)

    def test_correct_token_returns_schema_and_security_headers(self):
        status, headers, body = self._get("/api/live", self._auth())
        self.assertEqual(status, 200)
        self.assertTrue(headers["content-type"].startswith("application/json"))
        self.assertEqual(headers["cache-control"], "no-store")
        self.assertEqual(headers["x-content-type-options"], "nosniff")
        self.assertIn("default-src 'none'", headers["content-security-policy"])
        payload = json.loads(body)
        self.assertEqual(payload["schema"], SCHEMA)
        self.assertTrue(payload["read_only"])
        for key in ("generated_at", "agent_feed", "agents", "events", "counters", "host", "notes"):
            self.assertIn(key, payload)

    def test_missing_directory_yields_unknown_feed_not_green(self):
        _, _, body = self._get("/api/live", self._auth())
        payload = json.loads(body)
        self.assertEqual(payload["agent_feed"]["state"], "unknown")
        self.assertNotEqual(payload["agent_feed"]["state"], "ok")
        self.assertEqual(payload["agents"]["total"], 0)
        self.assertEqual(payload["events"]["accepted"], 0)
        # Host telemetry is reported separately with its own status, not merged into the feed.
        self.assertIn("host", payload)
        self.assertNotIn("state", payload["host"])

    def test_query_parameters_and_path_overrides_are_ignored(self):
        _, _, plain = self._get("/api/live", self._auth())
        status, _, with_params = self._get(
            "/api/live?root=/etc&path=/etc/passwd&limit=999999", self._auth()
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(plain)["schema"], json.loads(with_params)["schema"])
        self.assertEqual(json.loads(with_params)["agent_feed"]["state"], "unknown")
        self.assertNotIn(b"/etc", with_params)

    def test_fixed_root_is_not_configurable_from_http(self):
        self.assertEqual(server.LIVE_ROOT, server.hermes_home() / "local" / "share" / "hermes-nerve-live")
        status, _, _ = self._get("/api/live?root=" + str(self.dir), self._auth())
        self.assertEqual(status, 200)
        # Only the server-side attribute decides which directory is read.
        self.assertEqual(self.srv.live_root, self.live_root)

    def test_no_source_paths_or_secrets_leak(self):
        # A real feed directory with a readable lease and a secret-looking key.
        feed = self.dir / "feed"
        feed.mkdir()
        (feed / "lease-1-aaaaaaaaaaaa.json").write_text(json.dumps({"schema": "nerve-live-lease/v1",
                                                                    "secret": "sk-SHOULD-NOT-LEAK"}))
        self.srv.live_root = feed
        _, _, body = self._get("/api/live", self._auth())
        text = body.decode("utf-8")
        self.assertNotIn(str(self.dir), text)
        self.assertNotIn(str(feed), text)
        self.assertNotIn("SHOULD-NOT-LEAK", text)
        self.assertNotIn("sk-", text)
        self.assertNotIn(GOOD_TOKEN, text)
        self.assertNotIn("/home/", text)

    def test_old_sqlite_store_does_not_make_live_feed_green(self):
        con = sqlite3.connect(self.store_path)
        con.execute("CREATE TABLE work_events(event_id TEXT, created_at TEXT)")
        con.execute("INSERT INTO work_events VALUES('old','2020-01-01T00:00:00+00:00')")
        con.commit()
        con.close()
        _, _, body = self._get("/api/live", self._auth())
        payload = json.loads(body)
        self.assertEqual(payload["agent_feed"]["state"], "unknown")
        self.assertEqual(payload["events"]["accepted"], 0)
        self.assertNotIn(str(self.store_path), body.decode("utf-8"))

    def test_existing_get_paths_still_respond(self):
        status, _, body = self._get("/api/health", self._auth())
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["auth"], "bearer")
        status, _, _ = self._get("/api/snapshot", self._auth())
        self.assertEqual(status, 200)
        status, _, _ = self._get("/api/nope", self._auth())
        self.assertEqual(status, 404)

    def test_post_to_live_is_405(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request("POST", "/api/live", body=b"{}", headers=self._auth())
            self.assertEqual(conn.getresponse().status, 405)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
