"""Hermes process fleet: stable identity, privacy, evidence and lifecycle."""
import json
import sqlite3
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from hermes_nerve.observatory import fleet_monitor as f, process_probe


class ProbeTests(unittest.TestCase):
    def test_only_genuine_hermes_cli_processes(self):
        c = process_probe._candidate
        self.assertEqual(c(["/usr/bin/python", "-m", "hermes_cli.main", "--profile", "freebrain", "gateway", "run"]),
                         ("freebrain", "gateway"))
        self.assertEqual(c(["/home/tester/.hermes/venv/bin/python", "/home/tester/.hermes/hermes-agent/hermes", "chat"]),
                         ("default", "chat"))
        self.assertEqual(c(["/bin/hermes", "--profile=worker-one", "proxy", "start"]),
                         ("worker-one", "proxy"))
        self.assertIsNone(c(["python3", "-m", "hermes_nerve.observatory", "serve"]))
        self.assertIsNone(c(["python3", "-m", "hermes_factory.cli", "event", "serve"]))
        self.assertIsNone(c(["bash", "-c", "echo hermes chat"]))
        self.assertIsNone(c(["python", "/home/tester/.hermes/tools/mcp_death_supervisor.py"]))

    def test_proc_stat_and_row_never_expose_argv(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "uptime").write_text("500.00 100.00")
            pid = root / "123"
            pid.mkdir()
            (pid / "cmdline").write_bytes(b"/usr/bin/python3\0-m\0hermes_cli.main\0--profile\0test\0chat\0--query\0PRIVATE_SECRET\0")
            # stat fields 3..22: state,ppid, placeholders...,utime,stime,starttime.
            fields = ["S", "1"] + ["0"] * 9 + ["100", "50"] + ["0"] * 6 + ["25000"] + ["0"] * 3
            (pid / "stat").write_text("123 (hermes) " + " ".join(fields))
            (pid / "status").write_text("VmRSS:\t20480 kB\nThreads:\t4\n")
            data = process_probe.probe(root, now=100000, host="j2")
            self.assertEqual(data["state"], "ok")
            self.assertEqual(len(data["instances"]), 1)
            record = json.dumps(data)
            self.assertNotIn("PRIVATE_SECRET", record)
            self.assertNotIn("--query", record)
            self.assertEqual(data["instances"][0]["rss_mib"], 20.0)

    def test_remote_data_allowlist_blocks_injection(self):
        self.assertIsNone(f._clean_row({"pid": 1, "profile": "bad/../../path"}, "win4060"))


class CensusTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.now = 2000000000
        self.host_data = [self.row()]
        self.remote_data = []
        self.remote_up = True
        self.events = []
        self.agents = []
        self.local_up = True
        def local():
            return self.report("j2", self.host_data) if self.local_up else {"state": "unknown", "instances": []}
        def remote():
            return self.report("win4060", self.remote_data) if self.remote_up else {"state": "unknown", "instances": []}
        def feed():
            return {"agents": list(self.agents), "events": list(self.events), "feed": {"state": "available"}}
        self.m = f.FleetMonitor(Path(self.tmp.name) / "fleet.sqlite3",
                                local_probe=local, remote_probe=remote, feed_probe=feed)
        self.m.path.parent.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self.tmp.cleanup()

    def row(self, pid=234, tick=3456, cpu=100, mode="gateway"):
        return {"pid": pid, "ppid": 1, "start_ticks": tick, "threads": 4, "host": "j2",
                "profile": "freebrain", "mode": mode, "os_state": "S",
                "cpu_seconds": cpu, "rss_mib": 250.5, "cwd_label": "my-project",
                "started_at": f.iso(self.now - 100), "source": "procfs"}

    def report(self, host, items):
        return {"schema": process_probe.SCHEMA, "state": "ok", "host": host,
                "instances": items, "sampled_at": f.iso(self.now), "truncated": False}

    def test_six_right_dimensions_and_private(self):
        report = self.m.poll(at=self.now)
        self.assertEqual(report["schema"], f.SCHEMA)
        self.assertEqual(report["counts"]["running"], 1)
        row = report["instances"][0]
        self.assertEqual(row["key"], "j2:234:3456")
        self.assertEqual(row["profile"], "freebrain")
        self.assertEqual(row["purpose"], "Serve Hermes gateway requests")
        self.assertIsNone(row["task_intent"])
        self.assertEqual(row["observer"], "not_instrumented")
        self.assertIsNone(row["calls"])
        self.assertIsNone(row["cost_usd"])
        self.assertIsNone(row["fallbacks"])
        self.assertIsNone(row["tokens"]["input"])
        self.assertNotIn("argv", json.dumps(report))
        self.assertNotIn(str(Path.home()), json.dumps(report))

    def test_cpu_sample_is_rate_not_lifetime_average(self):
        self.m.poll(at=self.now)
        self.host_data[0]["cpu_seconds"] = 103.
        second = self.m.poll(at=self.now + 15)
        self.assertAlmostEqual(second["instances"][0]["cpu_percent"], 20.0, delta=2)

    def test_exact_once_events_token_lower_bound_and_speed(self):
        ts = f.iso(self.now - 1)
        self.events = [
            {"origin_ref": "234:aaaaaaaaaaaa#1", "ts": ts,
             "kind": "api_request", "status": "ok", "model": "claude-haiku-5-5",
             "provider": "anthropic", "dur_ms": 2000,
             "usage": {"input_tokens": 100, "output_tokens": 50}},
            {"origin_ref": "234:aaaaaaaaaaaa#2", "ts": ts,
             "kind": "tool_call", "tool": "terminal", "status": "ok"},
            {"origin_ref": "234:aaaaaaaaaaaa#3", "ts": ts,
             "kind": "api_error", "status": "timeout"},
        ]
        self.agents = [{"origin_ref": "234:aaaaaaaaaaaa#3", "key": "1111111111111111",
                        "role": "worker", "phase": "model request error", "last_ts": ts,
                        "lease_status": "live"}]
        first = self.m.poll(at=self.now)
        row = first["instances"][0]
        self.assertEqual(row["calls"], 1)
        self.assertEqual(row["tools"], 1)
        self.assertEqual(row["errors"], 1)
        self.assertEqual(row["timeouts"], 1)
        self.assertEqual(row["tokens"]["input"], 100)
        self.assertEqual(row["tokens"]["output"], 50)
        self.assertEqual(row["tokens"]["scope"], "observed_lower_bound")
        self.assertEqual(row["speed_tps"], 25.0)
        self.assertEqual(row["observer"], "instrumented")
        self.assertEqual(row["model"], "claude-haiku-5-5")
        self.assertEqual(len(self.m.detail(row["key"])["events"]), 3)
        again = self.m.poll(at=self.now + 12)["instances"][0]
        self.assertEqual(again["calls"], 1)
        self.assertEqual(again["tokens"]["input"], 100)
        self.assertEqual(again["errors"], 1)

    def test_pid_reuse_refuses_old_observer_attribution(self):
        self.events = [{"origin_ref": "234:aaaaaaaaaaaa#1", "ts": f.iso(self.now - 400),
                        "kind": "api_request", "usage": {"input_tokens": 999}}]
        report = self.m.poll(at=self.now)
        self.assertEqual(report["instances"][0]["observer"], "not_instrumented")
        self.assertIsNone(report["instances"][0]["tokens"]["input"])

    def test_departure_retained_and_remote_outage_not_ended(self):
        self.remote_data = [self.row(pid=56, tick=5)]
        first = self.m.poll(at=self.now)
        self.assertEqual(first["counts"]["running"], 2)
        self.host_data = []
        self.remote_up = False
        second = self.m.poll(at=self.now + 15)
        by_host = {x["host"]: x for x in second["instances"]}
        self.assertEqual(by_host["j2"]["status"], "ended")
        self.assertEqual(by_host["win4060"]["status"], "remote_unreachable")
        self.assertIsNone(by_host["win4060"]["ended_at"])
        self.assertEqual(second["sources"]["win4060"]["state"], "unknown")
        self.remote_up = True
        self.remote_data = []
        third = self.m.poll(at=self.now + 30)
        self.assertEqual([x for x in third["instances"] if x["host"] == "win4060"][0]["status"], "ended")

    def test_credential_and_prompt_fields_never_stored(self):
        row = self.row()
        row["argv"] = ["--key=sk-SECRET"]
        row["prompt"] = "PRIVATE PROMPT"
        self.host_data = [row]
        self.m.poll(at=self.now)
        data = Path(self.tmp.name, "fleet.sqlite3").read_bytes()
        self.assertNotIn(b"PRIVATE PROMPT", data)
        self.assertNotIn(b"sk-SECRET", data)

    def test_invalid_instance_key_not_a_filesystem_path(self):
        self.m.poll(at=self.now)
        self.assertIsNone(self.m.detail("../../../etc/passwd"))
        self.assertIsNone(self.m.detail("j2:bad:123"))
        self.assertIsNotNone(self.m.detail("j2:234:3456"))


if __name__ == "__main__":
    unittest.main()
