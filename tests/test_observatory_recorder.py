"""Focused tests for the bounded, segmented history recorder and store.

All paths are temporary. No network, no service, no real profile data.
Deterministic: no test depends on wall-clock timing beyond bounded waits.
"""

from __future__ import annotations

import json
import math
import os
import stat
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from hermes_nerve.observatory import collector, history_store, recorder

SECRET = "recorder-test-secret-0123456789abcdefghijklmnop"


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.dir = self.root / "observatory" / "history"
        self.store = self.root / "absent-store.sqlite3"
        self.shadow = self.root / "absent-shadow.jsonl"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _rows(self) -> list[dict]:
        rows = []
        for _, path in history_store.list_segments(self.dir):
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    rows.append(json.loads(line))
        return rows

    def _recorder(self, interval: float = 60) -> recorder.HistoryRecorder:
        return recorder.HistoryRecorder(self.dir, store_path=self.store, shadow_path=self.shadow, interval=interval)


class IntervalValidationTests(_Base):
    def test_rejects_nan_infinite_bool_and_out_of_range_before_any_side_effect(self) -> None:
        bad = [float("nan"), float("inf"), float("-inf"), -1, 0, recorder.MAX_INTERVAL_SECONDS + 1, True, "5", None]
        for value in bad:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    recorder.HistoryRecorder(self.dir, interval=value)
        self.assertFalse(self.dir.exists(), "invalid interval must not create the history directory")

    def test_accepts_bounds(self) -> None:
        self.assertEqual(recorder.validate_interval(recorder.MIN_INTERVAL_SECONDS), recorder.MIN_INTERVAL_SECONDS)
        self.assertEqual(recorder.validate_interval(recorder.MAX_INTERVAL_SECONDS), recorder.MAX_INTERVAL_SECONDS)
        self.assertFalse(math.isnan(recorder.validate_interval(60)))


class LifecycleTests(_Base):
    def test_disabled_by_default_means_no_recorder_and_no_rows(self) -> None:
        self.assertFalse(self.dir.exists())
        self.assertEqual(self._rows(), [])

    def test_periodic_loop_writes_rows_and_stop_is_idempotent(self) -> None:
        rec = self._recorder(interval=0.05)
        rec.start()
        deadline = 5.0
        end = threading.Event()
        while rec.rows_written < 3 and deadline > 0:
            end.wait(0.02)
            deadline -= 0.02
        self.assertTrue(rec.stop())
        count_after_first = len(self._rows())
        self.assertTrue(rec.stop(), "repeated stop must succeed")
        self.assertTrue(rec.stop(), "repeated stop must succeed")
        self.assertEqual(len(self._rows()), count_after_first, "repeated stop must not write extra rows")
        self.assertEqual(len(self._rows()), rec.rows_written)

    def test_join_timeout_skips_competing_final_write(self) -> None:
        rec = self._recorder(interval=60)
        gate = threading.Event()

        def blocked_run() -> None:
            gate.wait(10)  # Worker that refuses to exit in time.

        rec._run = blocked_run  # type: ignore[method-assign]
        with mock.patch.object(recorder, "JOIN_TIMEOUT_SECONDS", 0.05):
            rec.start()
            written_before = rec.rows_written
            self.assertFalse(rec.stop(), "stop must report the worker did not exit")
            self.assertEqual(rec.rows_written, written_before, "no closing row may race a live worker")
        gate.set()
        rec._thread.join(5)

    def test_stop_before_start_is_safe_and_idempotent(self) -> None:
        rec = self._recorder()
        self.assertTrue(rec.stop())
        self.assertTrue(rec.stop())
        self.assertEqual(self._rows(), [])

    def test_start_twice_is_refused(self) -> None:
        rec = self._recorder()
        rec.start()
        try:
            with self.assertRaises(RuntimeError):
                rec.start()
        finally:
            rec.stop()


class PersistenceAndRestartTests(_Base):
    def test_rows_survive_restart_and_are_never_rewritten(self) -> None:
        first = self._recorder()
        first.start()
        first.stop()
        before = {p: p.read_bytes() for _, p in history_store.list_segments(self.dir)}
        second = self._recorder()
        second.start()
        second.stop()
        for path, old in before.items():
            self.assertTrue(path.read_bytes().startswith(old), "restart must only append")
        self.assertGreaterEqual(len(self._rows()), 2)

    def test_segment_files_are_owner_only(self) -> None:
        rec = self._recorder()
        self.assertTrue(rec.write_row())
        for _, path in history_store.list_segments(self.dir):
            mode = stat.S_IMODE(os.stat(path).st_mode)
            self.assertEqual(mode & 0o077, 0, f"history must not be group/world accessible, got {oct(mode)}")

    def test_rows_hold_no_secrets_tokens_or_raw_shadow_state(self) -> None:
        rec = self._recorder()
        rec.write_row()
        text = "".join(p.read_text(encoding="utf-8") for _, p in history_store.list_segments(self.dir))
        self.assertNotIn(SECRET, text)
        self.assertNotIn("Authorization", text)
        self.assertNotIn("Bearer", text)
        for forbidden in ("prompt", "raw_state", "api_key", "token"):
            self.assertNotIn(forbidden, text.lower())

    def test_write_failure_is_recorded_by_class_and_loop_survives(self) -> None:
        blocked = self.root / "blocked"
        blocked.write_text("a file, so mkdir of its child must fail")
        rec = recorder.HistoryRecorder(blocked / "history", store_path=self.store, shadow_path=self.shadow, interval=60)
        self.assertFalse(rec.write_row())
        self.assertIsNotNone(rec.last_error)
        self.assertNotIn(str(self.root), rec.last_error)
        self.assertEqual(rec.state, recorder.STATE_ERROR)


class QuotaAndRotationTests(_Base):
    def test_quota_full_stops_capture_with_distinct_state_and_keeps_segments(self) -> None:
        self.dir.mkdir(parents=True)
        seg = self.dir / "history-000001.jsonl"
        seg.write_bytes(b'{"schema":"x"}\n')
        before = seg.read_bytes()
        with mock.patch.object(history_store, "TOTAL_BYTES", len(before) + 5):
            rec = self._recorder()
            self.assertFalse(rec.write_row())
            self.assertEqual(rec.state, recorder.STATE_STORAGE_FULL)
            self.assertEqual(rec.last_error, "HistoryStorageFull")
            # Further attempts stay stopped and never write.
            self.assertFalse(rec.write_row())
        self.assertEqual(seg.read_bytes(), before, "quota full must never delete or rewrite segments")

    def test_segment_rolls_when_full_without_touching_existing_segment(self) -> None:
        self.dir.mkdir(parents=True)
        seg = self.dir / "history-000001.jsonl"
        seg.write_bytes(b'{"a":1}\n')
        before = seg.read_bytes()
        with mock.patch.object(history_store, "SEGMENT_BYTES", len(before) + 1):
            result = history_store.append_row(self.dir, {"b": 2})
        self.assertEqual(result.segment, 2)
        self.assertEqual(seg.read_bytes(), before)

    def test_storage_state_reports_full_explicitly(self) -> None:
        self.dir.mkdir(parents=True)
        (self.dir / "history-000001.jsonl").write_bytes(b"x" * 10)
        with mock.patch.object(history_store, "TOTAL_BYTES", 10):
            self.assertEqual(history_store.storage_state(self.dir)["state"], "full")
        self.assertEqual(history_store.storage_state(self.dir / "missing")["state"], "ok")


class TornTailRecoveryTests(_Base):
    def test_torn_tail_is_skipped_not_repaired_and_never_appended_into(self) -> None:
        self.dir.mkdir(parents=True)
        seg = self.dir / "history-000001.jsonl"
        good = b'{"schema":"ok","n":1}\n'
        torn = b'{"schema":"ok","n":2'  # No newline: a torn write.
        seg.write_bytes(good + torn)
        before = seg.read_bytes()

        page = history_store.read_page(self.dir, limit=10)
        self.assertEqual([r["n"] for r in page["rows"]], [1], "torn row must be unreadable and skipped")
        self.assertEqual(page["skipped"], 1)
        self.assertEqual(seg.read_bytes(), before, "reading must never truncate or repair a torn tail")

        result = history_store.append_row(self.dir, {"schema": "ok", "n": 3})
        self.assertEqual(result.segment, 2, "a valid row must roll to a new segment, not glue onto a torn tail")
        self.assertEqual(seg.read_bytes(), before, "existing segment bytes must be untouched")
        page = history_store.read_page(self.dir, limit=10)
        self.assertEqual([r["n"] for r in page["rows"]], [1, 3])

    def test_malformed_complete_line_is_skipped(self) -> None:
        self.dir.mkdir(parents=True)
        (self.dir / "history-000001.jsonl").write_bytes(b'not json\n{"n":5}\n[1,2]\n')
        page = history_store.read_page(self.dir, limit=10)
        self.assertEqual([r["n"] for r in page["rows"]], [5])
        self.assertEqual(page["skipped"], 2)


class ConcurrencyTests(_Base):
    def test_concurrent_writers_produce_only_whole_parseable_rows(self) -> None:
        from concurrent.futures import ThreadPoolExecutor

        def write(idx: int) -> None:
            history_store.append_row(self.dir, {"schema": "s", "idx": idx, "payload": "x" * 128})

        with ThreadPoolExecutor(max_workers=12) as pool:
            list(pool.map(write, range(300)))
        page = history_store.read_page(self.dir, limit=history_store.MAX_PAGE_SIZE)
        seen = set()
        rows = list(page["rows"])
        cursor = page["next_cursor"]
        while cursor:
            page = history_store.read_page(self.dir, cursor=cursor, limit=history_store.MAX_PAGE_SIZE)
            rows.extend(page["rows"])
            cursor = page["next_cursor"]
        for row in rows:
            seen.add(row["idx"])
        self.assertEqual(seen, set(range(300)))
        self.assertEqual(len(rows), 300)

    def test_readers_during_writes_never_see_partial_rows(self) -> None:
        from concurrent.futures import ThreadPoolExecutor

        stop = threading.Event()
        errors: list[str] = []

        def writer() -> None:
            for i in range(200):
                history_store.append_row(self.dir, {"schema": "s", "idx": i})

        def reader() -> None:
            while not stop.is_set():
                try:
                    page = history_store.read_page(self.dir, limit=50)
                    for row in page["rows"]:
                        if not isinstance(row.get("idx"), int):
                            errors.append("partial row observed")
                except Exception as exc:  # pragma: no cover - reported as failure
                    errors.append(type(exc).__name__)

        with ThreadPoolExecutor(max_workers=3) as pool:
            r = pool.submit(reader)
            pool.submit(writer).result()
            stop.set()
            r.result()
        self.assertEqual(errors, [])


class PaginationAndCursorTests(_Base):
    def test_limit_and_cursor_validation(self) -> None:
        self.dir.mkdir(parents=True)
        for bad in (0, -1, history_store.MAX_PAGE_SIZE + 1, True, "5"):
            with self.subTest(limit=bad):
                with self.assertRaises(history_store.InvalidCursor):
                    history_store.read_page(self.dir, limit=bad)
        for bad_cursor in ("garbage", "1:2", "000001:-1", "000001:abc", "../../etc"):
            with self.subTest(cursor=bad_cursor):
                with self.assertRaises(history_store.InvalidCursor):
                    history_store.read_page(self.dir, cursor=bad_cursor)

    def test_pagination_is_stable_across_pages(self) -> None:
        for i in range(7):
            history_store.append_row(self.dir, {"schema": "s", "idx": i})
        collected = []
        cursor = None
        for _ in range(10):
            page = history_store.read_page(self.dir, cursor=cursor, limit=3)
            collected.extend(r["idx"] for r in page["rows"])
            cursor = page["next_cursor"]
            if not cursor:
                break
        self.assertEqual(collected, list(range(7)))

    def test_cursor_offset_past_segment_end_is_refused(self) -> None:
        history_store.append_row(self.dir, {"schema": "s", "idx": 0})
        with self.assertRaises(history_store.InvalidCursor):
            history_store.read_page(self.dir, cursor=history_store.format_cursor(1, 10**9))


class CliIntervalTests(unittest.TestCase):
    """The serve CLI validates the opt-in interval before any server or thread exists."""

    def _fake_server(self):
        class FakeServer:
            server_address = ("127.0.0.1", 0)

            def serve_forever(self):
                raise KeyboardInterrupt

            def server_close(self):
                return None

        return FakeServer()

    def test_invalid_interval_is_refused_before_server_is_created(self) -> None:
        from hermes_nerve.observatory import __main__ as obs_main
        from hermes_nerve.observatory import server as obs_server

        for bad in ("nan", "inf", "-inf", "0", "-1", "86401"):
            with self.subTest(interval=bad):
                with mock.patch.object(obs_server, "make_server") as make, mock.patch.object(
                    recorder, "HistoryRecorder"
                ) as rec_cls, mock.patch("sys.stderr"):
                    # The = form stops argparse treating "-inf" as an option flag.
                    try:
                        code = obs_main.main(["serve", f"--history-interval={bad}"])
                    except SystemExit as exc:  # argparse refusal is also a refusal
                        code = exc.code
                self.assertEqual(code, 2)
                make.assert_not_called()
                rec_cls.assert_not_called()

    def test_capture_is_disabled_by_default(self) -> None:
        from hermes_nerve.observatory import __main__ as obs_main
        from hermes_nerve.observatory import server as obs_server

        with mock.patch.object(obs_server, "make_server", return_value=self._fake_server()) as make, mock.patch.object(
            recorder, "HistoryRecorder"
        ) as rec_cls:
            code = obs_main.main(["serve", "--port", "0"])
        self.assertEqual(code, 0)
        make.assert_called_once()
        rec_cls.assert_not_called()

    def test_valid_opt_in_interval_starts_and_stops_recorder(self) -> None:
        from hermes_nerve.observatory import __main__ as obs_main
        from hermes_nerve.observatory import server as obs_server

        with mock.patch.object(obs_server, "make_server", return_value=self._fake_server()), mock.patch.object(
            recorder, "HistoryRecorder"
        ) as rec_cls:
            code = obs_main.main(["serve", "--port", "0", "--history-interval", "60"])
        self.assertEqual(code, 0)
        rec_cls.assert_called_once()
        self.assertEqual(rec_cls.call_args.kwargs["interval"], 60.0)
        rec_cls.return_value.start.assert_called_once()
        rec_cls.return_value.stop.assert_called_once()


class RecordCommandTests(unittest.TestCase):
    """The one-shot `record` command uses the segmented store. Temp paths only."""

    def setUp(self):
        import tempfile
        from unittest import mock

        from hermes_nerve.observatory import collector

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.store_path = self.dir / "store.sqlite"
        self.shadow = self.dir / "none.jsonl"
        # Redirect report-home so no test can write to the real one, even by default.
        self.fake_home = self.dir / "report-home"
        patcher = mock.patch.object(collector, "report_home", return_value=self.fake_home)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_default_history_dir_is_under_redirected_report_home_only(self):
        from hermes_nerve.observatory import history_store

        self.assertEqual(self._record(), 0)
        default_dir = self.fake_home / "observatory" / "history"
        self.assertEqual(
            sorted(p.name for p in default_dir.glob("history-*.jsonl")), ["history-000001.jsonl"]
        )
        self.assertEqual(len(history_store.read_page(default_dir, cursor=None, limit=50)["rows"]), 1)
        # The legacy file is never created by the default path.
        self.assertFalse((self.fake_home / "observatory" / "history.jsonl").exists())

    def _record(self, *extra):
        from hermes_nerve.observatory import __main__ as obs_main

        return obs_main.main(["record", "--store", str(self.store_path), "--shadow", str(self.shadow), *extra])

    def test_record_appends_to_segmented_store_in_history_dir(self):
        from hermes_nerve.observatory import history_store

        hist = self.dir / "history"
        self.assertEqual(self._record("--history-dir", str(hist)), 0)
        self.assertEqual(self._record("--history-dir", str(hist)), 0)
        page = history_store.read_page(hist, cursor=None, limit=50)
        self.assertEqual(len(page["rows"]), 2)
        self.assertEqual(sorted(p.name for p in hist.glob("history-*.jsonl")), ["history-000001.jsonl"])

    def test_record_rolls_to_next_segment_without_deleting_prior(self):
        from unittest import mock

        from hermes_nerve.observatory import history_store

        hist = self.dir / "history-roll"
        self.assertEqual(self._record("--history-dir", str(hist)), 0)
        before = (hist / "history-000001.jsonl").read_bytes()
        # Force the next append to roll: the first segment has reached its cap.
        with mock.patch.object(history_store, "SEGMENT_BYTES", len(before)):
            self.assertEqual(self._record("--history-dir", str(hist)), 0)
        names = sorted(p.name for p in hist.glob("history-*.jsonl"))
        self.assertEqual(names, ["history-000001.jsonl", "history-000002.jsonl"])
        # Prior segment is preserved byte-for-byte: append-only, never pruned.
        self.assertEqual((hist / "history-000001.jsonl").read_bytes(), before)

    def test_record_refuses_when_storage_full_and_deletes_nothing(self):
        from unittest import mock

        from hermes_nerve.observatory import history_store

        hist = self.dir / "history-full"
        hist.mkdir()
        seg = hist / "history-000001.jsonl"
        seg.write_bytes(b'{"schema":"x"}\n')
        with mock.patch.object(history_store, "TOTAL_BYTES", 1), mock.patch("sys.stderr"):
            self.assertEqual(self._record("--history-dir", str(hist)), 3)
        self.assertEqual(seg.read_bytes(), b'{"schema":"x"}\n')
        self.assertEqual(sorted(p.name for p in hist.glob("history-*.jsonl")), ["history-000001.jsonl"])

    def test_legacy_history_file_is_only_written_when_explicitly_requested(self):
        legacy = self.dir / "legacy" / "history.jsonl"
        hist = self.dir / "history-legacy"
        # Without --history, the legacy file is never created or touched.
        self.assertEqual(self._record("--history-dir", str(hist)), 0)
        self.assertFalse(legacy.exists())
        # Explicit legacy request writes only that file, labeled as legacy.
        self.assertEqual(self._record("--history", str(legacy)), 0)
        self.assertTrue(legacy.exists())
        self.assertFalse((self.dir / "history-legacy" / "history-000002.jsonl").exists())

    def test_record_rejects_both_history_flags_and_touches_neither_target(self):
        legacy = self.dir / "conflict" / "history.jsonl"
        hist = self.dir / "conflict-segments"
        with mock.patch("sys.stderr"):
            self.assertEqual(self._record("--history", str(legacy), "--history-dir", str(hist)), 2)
        self.assertFalse(legacy.exists())
        self.assertFalse(hist.exists())
        self.assertFalse((self.fake_home / "observatory").exists())


if __name__ == "__main__":
    unittest.main()
