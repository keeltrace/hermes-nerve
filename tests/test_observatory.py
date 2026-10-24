from __future__ import annotations

import contextlib
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from hermes_nerve.jsonl import read_jsonl
from hermes_nerve.observatory import collector, server
from hermes_nerve.reflex.shadow import ShadowProvider  # noqa: F401  (import check: shadow path intact)
from hermes_nerve.reflex import telemetry
from hermes_nerve.work.store import SupervisionStore

ROOT = Path(__file__).resolve().parents[1]
# Test-only credential: long enough for the server minimum, never a real secret.
GOOD_TOKEN = "test-token-0123456789abcdefghijklmnop"
NOW = datetime(2026, 10, 7, 12, 0, 0, tzinfo=timezone.utc)


def _insert_event(con, seq: int, *, task: str = "task-a", run: int = 1, worker: str | None = "worker-1",
                  source: str = "worker", created: str | None = None, event_type: str = "criterion_met") -> None:
    con.execute(
        """INSERT INTO work_events(event_id,task_id,run_id,contract_hash,claim_identity,worker_id,event_type,
           criterion_id,source,payload_json,stale,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            f"evt-{task}-{run}-{seq}-{os.getpid()}-{threading.get_ident()}",
            task, run, "hash-x", "claim-x", worker, event_type, "c1", source,
            json.dumps({"secret_prompt": "SHOULD-NOT-LEAK", "note": "payload"}), 0,
            created or NOW.isoformat(),
        ),
    )


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.store_path = self.dir / "work-supervision.sqlite3"
        self.shadow_path = self.dir / "shadow.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def test_missing_sources_are_reported_not_fabricated(self):
        snap = collector.build_snapshot(store_path=self.store_path, shadow_path=self.shadow_path, now=NOW)
        self.assertEqual(snap["store"]["status"], "missing")
        self.assertEqual(snap["shadow"]["status"], "missing")
        self.assertEqual(snap["runs"], [])
        self.assertEqual(snap["agents"], [])
        self.assertEqual(snap["event_total"], 0)
        # Only the live local host is represented; no remote hosts are invented.
        self.assertEqual([h["id"] for h in snap["fleet"]["hosts"]], ["local"])
        self.assertTrue(snap["fleet"]["hosts"][0]["live"])
        self.assertEqual(snap["fleet"]["hosts"][0]["source"], "live:os")

    def test_runs_agents_and_freshness_from_real_store(self):
        store = SupervisionStore(self.store_path)
        with store.transaction() as con:
            _insert_event(con, 1, created=(NOW - timedelta(seconds=30)).isoformat())
            _insert_event(con, 2, created=(NOW - timedelta(seconds=10)).isoformat())
            _insert_event(con, 3, task="task-b", worker=None, source="kanban",
                          created=(NOW - timedelta(hours=2)).isoformat())
        snap = collector.build_snapshot(store_path=self.store_path, shadow_path=self.shadow_path, now=NOW)
        self.assertEqual(snap["store"]["status"], "ok")
        self.assertEqual(snap["event_total"], 3)
        by_task = {(r["task_id"], r["run_id"]): r for r in snap["runs"]}
        self.assertEqual(by_task[("task-a", 1)]["events"], 2)
        self.assertEqual(by_task[("task-a", 1)]["freshness"]["state"], "fresh")
        self.assertEqual(by_task[("task-b", 1)]["freshness"]["state"], "stale")
        # Missing worker id is grouped under explicit "unrecorded", not dropped.
        keys = {a["key"] for a in snap["agents"]}
        self.assertEqual(keys, {"worker-1", "unrecorded"})
        sources = {s["source"]: s for s in snap["harnesses"]}
        self.assertEqual(sources["kanban"]["freshness"]["state"], "stale")

    def test_payloads_and_prompt_text_never_leave_the_collector(self):
        store = SupervisionStore(self.store_path)
        with store.transaction() as con:
            _insert_event(con, 1)
        snap = collector.build_snapshot(store_path=self.store_path, shadow_path=self.shadow_path, now=NOW)
        blob = json.dumps(snap)
        self.assertNotIn("SHOULD-NOT-LEAK", blob)
        self.assertNotIn("claim-x", blob)
        self.assertNotIn("hash-x", blob)
        events = collector.run_events(self.store_path, "task-a", 1)
        self.assertNotIn("SHOULD-NOT-LEAK", json.dumps(events))
        self.assertNotIn("payload", json.dumps(events))

    def test_unreadable_store_is_unavailable_not_empty_success(self):
        self.store_path.write_bytes(b"this is not a sqlite database at all" * 10)
        snap = collector.build_snapshot(store_path=self.store_path, shadow_path=self.shadow_path, now=NOW)
        self.assertEqual(snap["store"]["status"], "unavailable")
        self.assertEqual(snap["runs"], [])

    def test_shadow_telemetry_is_read_with_authority_marker(self):
        telemetry.append_shadow_record(
            path=self.shadow_path,
            state={"goal": "x"},
            questions={"decision": {"type": "choice"}},
            primary=type("P", (), {"answers": {"decision": {"type": "choice", "choice": "WATCH", "probabilities": {"WATCH": 0.9}}},
                                   "provider": "Jev", "model": "jev-latest", "latency_ms": 12.0, "usage": {}, "transport": "t",
                                   "live_provider_call": True, "request_id": "r1"})(),
            shadow=None,
            shadow_error="TimeoutError: timed out after 5s",
        )
        snap = collector.build_snapshot(store_path=self.store_path, shadow_path=self.shadow_path, now=NOW)
        sh = snap["shadow"]
        self.assertEqual(sh["authority"], "primary-jev")
        self.assertEqual(sh["status"], "ok")
        self.assertEqual(sh["shadow_errors"], 1)
        self.assertEqual(sh["shadow_timeouts"], 1)
        self.assertEqual(sh["recent"][0]["shadow"]["error_class"], "TimeoutError")
        self.assertNotIn("timed out after", json.dumps(sh))

    def test_history_is_append_only_with_restrictive_permissions(self):
        hist = self.dir / "history" / "history.jsonl"
        snap = collector.build_snapshot(store_path=self.store_path, shadow_path=self.shadow_path, now=NOW)
        collector.append_history(snap, hist)
        collector.append_history(snap, hist)
        rows = read_jsonl(hist)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["schema"], collector.HISTORY_SCHEMA)
        self.assertEqual(os.stat(hist).st_mode & 0o077, 0)


def _writer(store_path: str, label: str, count: int) -> None:
    store = SupervisionStore(Path(store_path))
    for i in range(count):
        with store.transaction() as con:
            _insert_event(con, i, task=f"{label}", run=1, worker=label, created=NOW.isoformat())


class ConcurrencyAndRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.store_path = self.dir / "work-supervision.sqlite3"
        SupervisionStore(self.store_path)  # create schema once

    def tearDown(self):
        self.tmp.cleanup()

    def test_twenty_concurrent_writer_processes_lose_no_events(self):
        writers = 20
        per_writer = 10
        procs = [
            subprocess.Popen(
                [sys.executable, "-c",
                 "import sys; sys.path.insert(0, sys.argv[1]);"
                 "from tests.test_observatory import _writer;"
                 "_writer(sys.argv[2], sys.argv[3], int(sys.argv[4]))",
                 str(ROOT), str(self.store_path), f"w{i}", str(per_writer)],
                cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            for i in range(writers)
        ]
        for p in procs:
            _, err = p.communicate(timeout=120)
            self.assertEqual(p.returncode, 0, err.decode("utf-8", "replace"))
        snap = collector.build_snapshot(store_path=self.store_path, shadow_path=self.dir / "none.jsonl", now=NOW)
        self.assertEqual(snap["store"]["status"], "ok")
        self.assertEqual(snap["event_total"], writers * per_writer)
        self.assertEqual(len(snap["agents"]), writers)

    def test_forced_termination_mid_write_leaves_store_readable_and_recoverable(self):
        child = subprocess.Popen(
            [sys.executable, "-c",
             "import sys; sys.path.insert(0, sys.argv[1]);"
             "from tests.test_observatory import _writer;"
             "_writer(sys.argv[2], 'killed', 100000)",
             str(ROOT), str(self.store_path)],
            cwd=str(ROOT), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        # Wait until the child has committed some events, then SIGKILL it.
        import time
        deadline = time.monotonic() + 60
        committed = 0
        while time.monotonic() < deadline and committed < 25:
            time.sleep(0.05)
            snap = collector.build_snapshot(store_path=self.store_path, shadow_path=self.dir / "none.jsonl", now=NOW)
            committed = snap["event_total"]
        child.send_signal(signal.SIGKILL)
        child.wait(timeout=30)
        self.assertGreaterEqual(committed, 25, "child never committed events before the kill")

        # After the forced kill, the store must be intact: every committed row is readable
        # and the collector reports ok, not unavailable or a half-written record.
        snap = collector.build_snapshot(store_path=self.store_path, shadow_path=self.dir / "none.jsonl", now=NOW)
        self.assertEqual(snap["store"]["status"], "ok")
        self.assertGreaterEqual(snap["event_total"], committed)
        events = collector.run_events(self.store_path, "killed", 1, limit=10)
        self.assertEqual(events["status"], "ok")

        # Recovery: a fresh writer can append after the crash.
        store = SupervisionStore(self.store_path)
        with store.transaction() as con:
            _insert_event(con, 999999, task="recovered", worker="post-crash", created=NOW.isoformat())
        snap2 = collector.build_snapshot(store_path=self.store_path, shadow_path=self.dir / "none.jsonl", now=NOW)
        self.assertEqual(snap2["event_total"], snap["event_total"] + 1)


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.store_path = self.dir / "work-supervision.sqlite3"
        SupervisionStore(self.store_path)

    def tearDown(self):
        self.tmp.cleanup()

    def _serve(self, token: str):
        srv = server.make_server(
            "127.0.0.1", 0, store_path=self.store_path, shadow_path=self.dir / "none.jsonl", token=token,
        )
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        return srv, f"http://127.0.0.1:{srv.server_address[1]}"

    def _get(self, url: str, headers: dict | None = None, method: str = "GET"):
        req = urllib.request.Request(url, headers=headers or {}, method=method)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers), exc.read()

    def test_non_loopback_bind_without_token_is_refused(self):
        with self.assertRaises(ValueError):
            server.make_server("0.0.0.0", 0, store_path=self.store_path, token="")
        with self.assertRaises(ValueError):
            server.make_server("192.0.2.10", 0, store_path=self.store_path, token="")

    def test_api_requires_constant_time_bearer_when_token_set(self):
        _, base = self._serve(GOOD_TOKEN)
        self.assertEqual(self._get(base + "/api/snapshot")[0], 401)
        self.assertEqual(self._get(base + "/api/snapshot", {"Authorization": "Bearer wrong"})[0], 401)
        self.assertEqual(self._get(base + "/api/snapshot", {"Authorization": GOOD_TOKEN})[0], 401)
        status, headers, body = self._get(base + "/api/snapshot", {"Authorization": "Bearer " + GOOD_TOKEN})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["schema"], collector.SCHEMA)

    def test_static_shell_has_no_data_and_strict_headers(self):
        _, base = self._serve(GOOD_TOKEN)
        status, headers, body = self._get(base + "/")
        self.assertEqual(status, 200)
        self.assertIn("default-src 'none'", headers["Content-Security-Policy"])
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(headers["X-Frame-Options"], "DENY")
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertNotIn(b"http://", body.replace(b"http://www.w3.org", b""))  # no external assets

    def test_ui_source_is_text_only_with_no_external_assets(self):
        # Data reaches the DOM only through textContent; no HTML sinks or eval.
        from hermes_nerve.observatory import ui

        js = ui.APP_JS
        for sink in ("innerHTML", "insertAdjacentHTML", "document.write", "eval("):
            self.assertNotIn(sink, js, f"unsafe DOM sink {sink!r} in UI script")
        # Only the SVG XML namespace identifier is allowed; it is never fetched.
        svg_ns = "http://www.w3.org/2000/svg"
        for shell in (ui.INDEX_HTML, ui.APP_CSS, js):
            stripped = shell.replace(svg_ns, "")
            self.assertNotIn("http://", stripped)
            self.assertNotIn("https://", stripped)
        self.assertIn('<script src="/app.js">', ui.INDEX_HTML)
        self.assertIn('<link rel="stylesheet" href="/app.css">', ui.INDEX_HTML)

    def test_only_get_is_allowed(self):
        _, base = self._serve(GOOD_TOKEN)
        status, headers, _ = self._get(base + "/api/snapshot", {"Authorization": "Bearer " + GOOD_TOKEN}, method="POST")
        self.assertEqual(status, 405)
        self.assertEqual(headers["Allow"], "GET")

    def test_run_endpoint_validates_input(self):
        _, base = self._serve(GOOD_TOKEN)
        auth = {"Authorization": "Bearer " + GOOD_TOKEN}
        self.assertEqual(self._get(base + "/api/run?task_id=&run_id=1", auth)[0], 400)
        self.assertEqual(self._get(base + "/api/run?task_id=t&run_id=abc", auth)[0], 400)
        self.assertEqual(self._get(base + "/api/run?task_id=t&run_id=1", auth)[0], 200)

    def test_history_api_requires_bearer_and_returns_sanitized_pages(self):
        from hermes_nerve.observatory import history_store

        hist_dir = self.dir / "history"
        for i in range(5):
            history_store.append_row(hist_dir, {"schema": collector.HISTORY_SCHEMA, "idx": i, "event_total": i})
        srv = server.make_server(
            "127.0.0.1", 0, store_path=self.store_path, shadow_path=self.dir / "none.jsonl",
            token=GOOD_TOKEN, history_dir=hist_dir,
        )
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        auth = {"Authorization": "Bearer " + GOOD_TOKEN}

        # Auth is required: absent, wrong and bare-token credentials are refused.
        self.assertEqual(self._get(base + "/api/history")[0], 401)
        self.assertEqual(self._get(base + "/api/history", {"Authorization": "Bearer wrong"})[0], 401)
        self.assertEqual(self._get(base + "/api/history", {"Authorization": GOOD_TOKEN})[0], 401)

        # Read-only: other methods are refused.
        self.assertEqual(self._get(base + "/api/history", auth, method="POST")[0], 405)

        # Bounded pagination: first page, then follow the validated cursor to the end.
        status, _, body = self._get(base + "/api/history?limit=2", auth)
        self.assertEqual(status, 200)
        first = json.loads(body)
        self.assertEqual([r["idx"] for r in first["rows"]], [0, 1])
        self.assertIsNotNone(first["next_cursor"])
        self.assertEqual(first["storage"]["state"], "ok")
        seen = list(first["rows"])
        cursor = first["next_cursor"]
        while cursor:
            status, _, body = self._get(base + "/api/history?limit=2&cursor=" + cursor, auth)
            self.assertEqual(status, 200)
            page = json.loads(body)
            seen.extend(page["rows"])
            cursor = page["next_cursor"]
        self.assertEqual([r["idx"] for r in seen], [0, 1, 2, 3, 4])

        # Invalid limit, oversize limit and malformed cursor are refused with 400.
        self.assertEqual(self._get(base + "/api/history?limit=0", auth)[0], 400)
        self.assertEqual(self._get(base + "/api/history?limit=abc", auth)[0], 400)
        self.assertEqual(self._get(base + "/api/history?limit=99999", auth)[0], 400)
        self.assertEqual(self._get(base + "/api/history?cursor=../../etc/passwd", auth)[0], 400)

        # Sanitized output: no secrets, auth headers, or raw shadow fields in the body.
        status, _, body = self._get(base + "/api/history?limit=50", auth)
        text = body.decode("utf-8")
        self.assertNotIn(GOOD_TOKEN, text)
        self.assertNotIn("Authorization", text)
        self.assertNotIn("prompt", text.lower())

    def test_history_status_reports_disabled_and_active_recorder_safely(self):
        from unittest import mock

        from hermes_nerve.observatory import history_store, recorder

        # Default: no recorder attached, so capture is reported disabled.
        srv = server.make_server(
            "127.0.0.1", 0, store_path=self.store_path, shadow_path=self.dir / "none.jsonl",
            token=GOOD_TOKEN, history_dir=self.dir / "history-status",
        )
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        auth = {"Authorization": "Bearer " + GOOD_TOKEN}

        self.assertEqual(self._get(base + "/api/history/status")[0], 401)
        status, _, body = self._get(base + "/api/history/status", auth)
        self.assertEqual(status, 200)
        capture = json.loads(body)["capture"]
        self.assertEqual(capture["enabled"], False)
        self.assertEqual(capture["state"], "disabled")
        self.assertIsNone(capture["interval_seconds"])
        self.assertEqual(capture["rows_written"], 0)
        self.assertIsNone(capture["last_error"])
        self.assertIs(capture["running"], False)
        self.assertEqual(capture["storage"]["state"], "ok")
        self.assertEqual(capture["storage"]["segments"], 0)
        self.assertNotIn(str(self.dir), body.decode("utf-8"))

        # Disabled status also reports storage quota full from the segmented directory.
        full_dir = self.dir / "history-disabled-full"
        full_dir.mkdir()
        (full_dir / "history-000001.jsonl").write_bytes(b'{"schema":"x"}\n')
        srv.history_dir = full_dir
        with mock.patch.object(history_store, "TOTAL_BYTES", 1):
            status, _, body = self._get(base + "/api/history/status", auth)
        capture = json.loads(body)["capture"]
        self.assertEqual(capture["state"], "disabled")
        self.assertEqual(capture["storage"]["state"], "full")
        self.assertNotIn(str(self.dir), body.decode("utf-8"))
        self.assertEqual(self._get(base + "/api/snapshot", auth)[0], 200)
        srv.history_dir = self.dir / "history-status"

        # Active recorder: status carries sanitized fields only, never a path.
        rec = recorder.HistoryRecorder(
            self.dir / "history-status-active", store_path=self.store_path,
            shadow_path=self.dir / "none.jsonl", interval=60.0,
        )
        srv.history_recorder = rec
        rec.write_row()
        status, _, body = self._get(base + "/api/history/status", auth)
        text = body.decode("utf-8")
        capture = json.loads(text)["capture"]
        self.assertTrue(capture["enabled"])
        self.assertEqual(capture["rows_written"], 1)
        self.assertIsNone(capture["last_error"])
        self.assertIn(capture["storage"]["state"], ("ok", "empty"))
        self.assertNotIn(str(self.dir), text)
        self.assertNotIn("history-status-active", text)
        self.assertNotIn(GOOD_TOKEN, text)

        # Live snapshot is unaffected by the capture status route.
        status, _, body = self._get(base + "/api/snapshot", auth)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["schema"], collector.SCHEMA)

    def test_storage_full_and_error_capture_status_leave_snapshot_healthy(self):
        from unittest import mock

        from hermes_nerve.observatory import history_store, recorder

        hist_dir = self.dir / "history-status-full"
        hist_dir.mkdir()
        (hist_dir / "history-000001.jsonl").write_bytes(b'{"schema":"x"}\n')
        srv = server.make_server(
            "127.0.0.1", 0, store_path=self.store_path, shadow_path=self.dir / "none.jsonl",
            token=GOOD_TOKEN, history_dir=hist_dir,
        )
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        auth = {"Authorization": "Bearer " + GOOD_TOKEN}

        rec = recorder.HistoryRecorder(hist_dir, store_path=self.store_path,
                                       shadow_path=self.dir / "none.jsonl", interval=60.0)
        srv.history_recorder = rec
        with mock.patch.object(history_store, "TOTAL_BYTES", 1):
            rec.write_row()  # Quota full: capture stops with the distinct state.
            status, _, body = self._get(base + "/api/history/status", auth)
            self.assertEqual(status, 200)
            capture = json.loads(body)["capture"]
            self.assertEqual(rec.state, recorder.STATE_STORAGE_FULL)
            self.assertEqual(capture["state"], "history_storage_full")
            self.assertEqual(capture["last_error"], "HistoryStorageFull")
            # The live snapshot stays healthy while capture reports storage full.
            status, _, body = self._get(base + "/api/snapshot", auth)
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body)["schema"], collector.SCHEMA)

        # A non-storage capture error exposes only its class name.
        rec2 = recorder.HistoryRecorder(self.dir / "history-status-err", store_path=self.store_path,
                                        shadow_path=self.dir / "none.jsonl", interval=60.0)
        srv.history_recorder = rec2
        with mock.patch.object(history_store, "append_row", side_effect=OSError("secret path /x")):
            rec2.write_row()
        status, _, body = self._get(base + "/api/history/status", auth)
        text = body.decode("utf-8")
        capture = json.loads(text)["capture"]
        self.assertEqual(capture["state"], "error")
        self.assertEqual(capture["last_error"], "OSError")
        self.assertNotIn("secret path", text)
        self.assertEqual(self._get(base + "/api/snapshot", auth)[0], 200)

    def test_quota_full_history_does_not_hide_live_snapshot(self):
        from unittest import mock

        from hermes_nerve.observatory import history_store

        hist_dir = self.dir / "history-full"
        hist_dir.mkdir()
        (hist_dir / "history-000001.jsonl").write_bytes(b'{"schema":"x"}\n')
        srv = server.make_server(
            "127.0.0.1", 0, store_path=self.store_path, shadow_path=self.dir / "none.jsonl",
            token=GOOD_TOKEN, history_dir=hist_dir,
        )
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        auth = {"Authorization": "Bearer " + GOOD_TOKEN}

        with mock.patch.object(history_store, "TOTAL_BYTES", 1):
            status, _, body = self._get(base + "/api/history", auth)
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body)["storage"]["state"], "full")
            # A full history quota must not hide or fail the live snapshot.
            status, _, body = self._get(base + "/api/snapshot", auth)
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body)["schema"], collector.SCHEMA)

    def test_every_bind_requires_a_token_before_any_socket_opens(self):
        for host in ("127.0.0.1", "localhost", "0.0.0.0", "192.0.2.10"):
            with self.assertRaises(ValueError, msg=host):
                server.make_server(host, 0, store_path=self.store_path, token="")
        with self.assertRaises(ValueError):
            server.make_server("127.0.0.1", 0, store_path=self.store_path, token="short")

    def test_missing_wrong_or_malformed_credentials_are_refused(self):
        _, base = self._serve(GOOD_TOKEN)
        cases = {
            "absent": {},
            "wrong": {"Authorization": "Bearer " + "x" * len(GOOD_TOKEN)},
            "empty bearer": {"Authorization": "Bearer "},
            "basic scheme": {"Authorization": "Basic " + GOOD_TOKEN},
            "bare token, no scheme": {"Authorization": GOOD_TOKEN},
            "lowercase scheme": {"Authorization": "bearer " + GOOD_TOKEN},
            "oversized": {"Authorization": "Bearer " + "A" * 600},
            "prefix of token": {"Authorization": "Bearer " + GOOD_TOKEN[:-1]},
        }
        for label, headers in cases.items():
            for path in ("/api/health", "/api/snapshot", "/api/run?task_id=t&run_id=1"):
                status, _, body = self._get(base + path, headers)
                self.assertEqual(status, 401, f"{label} on {path}")
                self.assertNotIn(GOOD_TOKEN.encode(), body)

    def test_valid_token_is_accepted_on_every_api_route(self):
        _, base = self._serve(GOOD_TOKEN)
        auth = {"Authorization": "Bearer " + GOOD_TOKEN}
        self.assertEqual(self._get(base + "/api/health", auth)[0], 200)
        self.assertEqual(self._get(base + "/api/snapshot", auth)[0], 200)
        self.assertEqual(self._get(base + "/api/run?task_id=t&run_id=1", auth)[0], 200)

    def test_run_details_and_entry_require_auth_but_shell_is_data_free(self):
        _, base = self._serve(GOOD_TOKEN)
        self.assertEqual(self._get(base + "/api/run?task_id=secret-task&run_id=1")[0], 401)
        status, _, shell = self._get(base + "/")
        self.assertEqual(status, 200)
        # The unauthenticated shell must not contain operational data or the token.
        self.assertNotIn(b"secret-task", shell)
        self.assertNotIn(GOOD_TOKEN.encode(), shell)
        for asset in ("/app.js", "/app.css"):
            status, _, body = self._get(base + asset)
            self.assertEqual(status, 200)
            self.assertNotIn(GOOD_TOKEN.encode(), body)

    def test_token_never_appears_in_urls_or_logs(self):
        srv, base = self._serve(GOOD_TOKEN)
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf), contextlib.redirect_stdout(buf):
            self._get(base + "/api/snapshot?token=" + GOOD_TOKEN)  # token in URL is not accepted
            self._get(base + "/api/snapshot", {"Authorization": "Bearer " + GOOD_TOKEN})
            self._get(base + "/no/such/path/secretpath")
        self.assertEqual(self._get(base + "/api/snapshot?token=" + GOOD_TOKEN)[0], 401)
        self.assertNotIn(GOOD_TOKEN, buf.getvalue())
        self.assertNotIn("secretpath", buf.getvalue())
        self.assertNotIn("/api/snapshot", buf.getvalue())

    def test_non_loopback_bind_with_token_still_requires_auth(self):
        srv = server.make_server("0.0.0.0", 0, store_path=self.store_path, token=GOOD_TOKEN)
        self.addCleanup(srv.server_close)
        self.assertFalse(srv.loopback_only)
        self.assertEqual(srv.server_address[0], "0.0.0.0")

    def test_dns_rebinding_host_header_is_rejected_on_loopback(self):
        _, base = self._serve(GOOD_TOKEN)
        status, _, _ = self._get(base + "/api/health", {"Host": "evil.example.com", "Authorization": "Bearer " + GOOD_TOKEN})
        self.assertEqual(status, 421)


if __name__ == "__main__":
    unittest.main()
