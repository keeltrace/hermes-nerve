"""GET-only authenticated Hermes process census API and privacy boundary."""
import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path

from hermes_nerve.observatory import fleet_monitor, process_probe, server

TOKEN = "fleet-frozen-fixture-token-abcdefghijklmnopqrstuvwxyz"


class FleetApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        p = Path(self.tmp.name)
        self.monitor = fleet_monitor.FleetMonitor(
            p / "fleet.sqlite3", local_probe=lambda: {
                "schema": process_probe.SCHEMA, "state": "ok",
                "sampled_at": "2030-01-01T00:00:00Z",
                "instances": [{
                    "pid": 1234, "ppid": 1, "start_ticks": 24000,
                    "profile": "example", "mode": "chat", "os_state": "S",
                    "cpu_seconds": 3, "rss_mib": 40, "threads": 2,
                    "cwd_label": "safe", "started_at": "2029-12-31T23:59:00Z",
                }]},
            remote_probe=lambda: {"schema": process_probe.SCHEMA, "state": "ok", "instances": []},
            feed_probe=lambda: {"agents": [], "events": [], "feed": {"state": "unknown"}},
        )
        self.monitor.poll(at=1893456000)
        self.server = server.make_server("127.0.0.1", 0, token=TOKEN, store_path=p / "empty.sqlite3",
                                         history_dir=p / "history")
        self.server.fleet_monitor = self.monitor
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.tmp.cleanup()

    def request(self, path, *, auth=True, method="GET", host=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        h = {"Authorization": "Bearer " + TOKEN} if auth else {}
        if host:
            h["Host"] = host
        try:
            c.request(method, path, headers=h)
            r = c.getresponse()
            return r.status, dict(r.getheaders()), r.read()
        finally:
            c.close()

    def test_missing_bearer_is_401(self):
        status, _, body = self.request("/api/fleet", auth=False)
        self.assertEqual(status, 401)
        self.assertNotIn(b"example", body)

    def test_nonloopback_host_421(self):
        self.assertEqual(self.request("/api/fleet", host="exfil.invalid")[0], 421)

    def test_authenticated_census_has_provenance_without_secrets(self):
        code, headers, raw = self.request("/api/fleet")
        self.assertEqual(code, 200)
        self.assertEqual(headers["Cache-Control"], "no-store")
        data = json.loads(raw)
        self.assertEqual(data["schema"], fleet_monitor.SCHEMA)
        self.assertEqual(data["counts"]["running"], 1)
        self.assertEqual(data["instances"][0]["key"], "j2:1234:24000")
        self.assertIsNone(data["instances"][0]["task_intent"])
        self.assertNotIn(b"argv", raw)
        self.assertNotIn(b"/home/", raw)
        self.assertNotIn(TOKEN.encode(), raw)

    def test_detail_uses_exact_id_and_bounded_limit(self):
        code, _, raw = self.request("/api/fleet/instance?key=j2:1234:24000&limit=200")
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(raw)["instance"]["profile"], "example")
        self.assertEqual(self.request("/api/fleet/instance?key=../etc/passwd")[0], 400)
        self.assertEqual(self.request("/api/fleet/instance?key=j2:1234:24001")[0], 404)
        self.assertEqual(self.request("/api/fleet/instance?key=j2:1234:24000&limit=no")[0], 400)
        self.assertEqual(self.request("/api/fleet/instance?key=j2:1234:24000&limit=3&limit=4")[0], 400)

    def test_no_mutation_or_unauthorized_details(self):
        self.assertEqual(self.request("/api/fleet", method="POST")[0], 405)
        self.assertEqual(self.request("/api/fleet/instance?key=j2:1234:24000", auth=False)[0], 401)

    def test_disabled_monitor_never_claims_zero_instances_are_healthy(self):
        self.server.fleet_monitor = None
        code, _, raw = self.request("/api/fleet")
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(raw)["status"], "disabled")
        self.assertEqual(self.request("/api/fleet/instance?key=j2:1234:24000")[0], 503)


if __name__ == "__main__":
    unittest.main()
