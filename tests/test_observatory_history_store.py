"""Regression tests for the bounded history store's physical-line, cursor, scan-budget
and cross-process append behavior. Temp paths only; no report-home, no network."""

from __future__ import annotations

import json
import multiprocessing
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hermes_nerve.observatory import history_store as hs


def _payload_len(record: dict) -> int:
    # Mirrors append_row's serialization exactly.
    return len((json.dumps(record, sort_keys=True, ensure_ascii=False, default=str) + "\n").encode("utf-8"))


class _TempDirTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)


class OverlongAndCursorTests(_TempDirTest):
    def _segment(self, data: bytes) -> Path:
        seg = self.dir / "history-000001.jsonl"
        seg.write_bytes(data)
        return seg

    def test_overlong_line_with_valid_json_suffix_is_one_skipped_physical_line(self):
        # A junk run of exactly MAX+1 bytes followed by a valid JSON object on the
        # SAME physical line. The suffix must never be parsed as a row.
        for junk_len in (hs.MAX_LINE_BYTES + 1, hs.MAX_LINE_BYTES + 40_000):
            with self.subTest(junk_len=junk_len):
                seg = self.dir / f"seg-{junk_len}" / "history-000001.jsonl"
                seg.parent.mkdir()
                before = b"x" * junk_len + b'{"ok": 1}\n' + b'{"ok": 2}\n'
                seg.write_bytes(before)
                page = hs.read_page(seg.parent, limit=50)
                self.assertEqual(page["rows"], [{"ok": 2}])
                self.assertEqual(page["skipped"], 1)
                self.assertIsNone(page["next_cursor"])
                # Read-only: the segment bytes are unchanged.
                self.assertEqual(seg.read_bytes(), before)

    def test_cursor_offset_inside_junk_prefix_is_rejected(self):
        junk = b"JUNK"
        row = b'{"ok": 3}\n'
        self._segment(junk + row)
        # Offset 4 sits inside the junk prefix (byte 3 is not a newline).
        with self.assertRaises(hs.InvalidCursor):
            hs.read_page(self.dir, cursor="000001:4", limit=50)
        # Offset 0 is a valid line start. "JUNK" and the row share one physical line
        # (no newline after JUNK), so that line is unparseable: one skip, no rows.
        page = hs.read_page(self.dir, cursor="000001:0", limit=50)
        self.assertEqual(page["rows"], [])
        self.assertEqual(page["skipped"], 1)
        # Offset immediately after the newline is a valid boundary (end of segment here).
        page = hs.read_page(self.dir, cursor=f"000001:{len(junk) + len(row)}", limit=50)
        self.assertEqual(page["rows"], [])
        self.assertEqual(page["skipped"], 0)

    def test_scan_budget_continues_without_losing_or_double_counting(self):
        # Two segments of alternating junk and valid rows.
        seg1 = self.dir / "history-000001.jsonl"
        seg2 = self.dir / "history-000002.jsonl"
        expected_rows: list[dict] = []
        junk_total = 0
        lines1 = []
        for i in range(8):
            if i % 2:
                lines1.append(b"junk-line-%02d\n" % i)
                junk_total += 1
            else:
                row = {"n": i}
                expected_rows.append(row)
                lines1.append((json.dumps(row) + "\n").encode())
        lines2 = []
        for i in range(8, 16):
            if i % 2:
                lines2.append(b"junk-line-%02d\n" % i)
                junk_total += 1
            else:
                row = {"n": i}
                expected_rows.append(row)
                lines2.append((json.dumps(row) + "\n").encode())
        seg1.write_bytes(b"".join(lines1))
        seg2.write_bytes(b"".join(lines2))

        # Tiny budgets force many continuation pages.
        # A 40-byte request can validate the cursor and inspect one maximum legal row.
        with mock.patch.object(hs, "MAX_SCAN_BYTES", 40), mock.patch.object(hs, "MAX_SCAN_LINES", 3), mock.patch.object(hs, "MAX_LINE_BYTES", 16):
            rows: list[dict] = []
            skipped_total = 0
            cursor = None
            pages = 0
            while True:
                page = hs.read_page(self.dir, cursor=cursor, limit=50)
                pages += 1
                self.assertLessEqual(len(page["rows"]), 50)
                rows.extend(page["rows"])
                skipped_total += page["skipped"]
                cursor = page["next_cursor"]
                if cursor is None:
                    break
                self.assertLess(pages, 200, "scan budget must always make progress")
        self.assertGreater(pages, 3)
        self.assertEqual(rows, expected_rows)  # no valid row lost, order preserved
        self.assertEqual(skipped_total, junk_total)  # each junk line counted exactly once

    def test_scan_budget_truly_bounds_one_request(self):
        # A 10-row segment. A 3-line scan budget must stop the request early with a
        # continuation cursor. A budget-less implementation returns all 10 rows and
        # no cursor, which this test rejects.
        lines = b"".join((json.dumps({"n": i}) + "\n").encode() for i in range(10))
        (self.dir / "history-000001.jsonl").write_bytes(lines)
        with mock.patch.object(hs, "MAX_SCAN_LINES", 3):
            bounded = hs.read_page(self.dir, cursor=None, limit=50)
        self.assertIsNotNone(bounded["next_cursor"], "scan budget must return a continuation cursor")
        self.assertEqual(len(bounded["rows"]), 3)
        self.assertEqual([r["n"] for r in bounded["rows"]], [0, 1, 2])
        # Continuing from the cursor yields the rest, without loss.
        rest = hs.read_page(self.dir, cursor=bounded["next_cursor"], limit=50)
        self.assertEqual([r["n"] for r in rest["rows"]], list(range(3, 10)))
        self.assertIsNone(rest["next_cursor"])

    def test_overlong_line_counts_as_one_skip_under_scan_budget(self):
        # Each request has room for the maximum legal line plus cursor validation.
        # The malformed physical line spans many requests; its valid-looking suffix
        # must be discarded, counted once, and never parsed.
        line_cap = 8
        scan_budget = line_cap + 2
        big = b"x" * (line_cap + 100) + b'{"f":0}\n'
        good = b'{"o":1}\n'  # exactly line_cap bytes including newline
        (self.dir / "history-000001.jsonl").write_bytes(big + good)
        actual_reads = []
        original_open = Path.open

        class CountingFile:
            def __init__(self, handle):
                self.handle = handle
            def __enter__(self):
                return self
            def __exit__(self, *args):
                self.handle.close()
            def __getattr__(self, name):
                return getattr(self.handle, name)
            def read(self, *args):
                chunk = self.handle.read(*args)
                actual_reads[-1] += len(chunk)
                return chunk
            def readline(self, *args):
                chunk = self.handle.readline(*args)
                actual_reads[-1] += len(chunk)
                return chunk

        with mock.patch.object(hs, "MAX_SCAN_BYTES", scan_budget), mock.patch.object(hs, "MAX_SCAN_LINES", 1), mock.patch.object(hs, "MAX_LINE_BYTES", line_cap):
            rows = []
            skipped = 0
            cursor = None
            pages = 0
            seen = set()
            while True:
                actual_reads.append(0)
                with mock.patch.object(Path, "open", lambda path, *args, **kwargs: CountingFile(original_open(path, *args, **kwargs))):
                    page = hs.read_page(self.dir, cursor=cursor, limit=50)
                pages += 1
                self.assertLessEqual(actual_reads[-1], scan_budget)
                self.assertEqual(actual_reads[-1], page["scanned_bytes"])
                rows.extend(page["rows"])
                skipped += page["skipped"]
                cursor = page["next_cursor"]
                if cursor is None:
                    break
                self.assertNotIn(cursor, seen)
                seen.add(cursor)
                self.assertLess(pages, 100)
        self.assertGreater(pages, 3)
        self.assertEqual(rows, [{"o": 1}])
        self.assertEqual(skipped, 1)


def _writer(barrier, directory, worker, attempts, results):
    """Forked child. Uses the production append_row; module constants are inherited."""
    outcomes = []
    barrier.wait()  # Start every writer together to induce contention.
    for seq in range(attempts):
        try:
            hs.append_row(directory, {"w": worker, "seq": seq})
            outcomes.append(("ok", worker, seq, ""))
        except hs.HistoryStorageFull as exc:
            outcomes.append(("full", worker, seq, str(exc)))
        except Exception as exc:  # any other failure is a test failure
            outcomes.append(("error", worker, seq, f"{type(exc).__name__}: {exc}"))
    results.put(outcomes)


class CrossProcessQuotaRaceTests(_TempDirTest):
    def test_multiprocess_writers_respect_quota_and_segment_rolling(self):
        # Fixed-width records so every row has identical size L.
        sample = {"w": 0, "seq": 0}
        length = _payload_len(sample)
        workers, attempts = 4, 5  # 20 attempts competing for 7 slots
        # Three rows per segment, seven rows in total.
        segment_bytes = 3 * length
        total_bytes = 7 * length

        ctx = multiprocessing.get_context("fork")
        barrier = ctx.Barrier(workers)
        results = ctx.Queue()
        with mock.patch.object(hs, "SEGMENT_BYTES", segment_bytes), mock.patch.object(
            hs, "TOTAL_BYTES", total_bytes
        ):
            procs = [
                ctx.Process(target=_writer, args=(barrier, self.dir, w, attempts, results))
                for w in range(workers)
            ]
            for p in procs:
                p.start()
            outcomes = []
            for _ in procs:
                outcomes.extend(results.get(timeout=60))
            for p in procs:
                p.join(timeout=60)
                self.assertEqual(p.exitcode, 0)

        errors = [o for o in outcomes if o[0] == "error"]
        self.assertEqual(errors, [])
        ok = {(o[1], o[2]) for o in outcomes if o[0] == "ok"}
        full = [o for o in outcomes if o[0] == "full"]
        # Deterministic: exactly the quota's worth of rows succeed, the rest are quota refusals.
        self.assertEqual(len(ok), 7)
        self.assertEqual(len(full), workers * attempts - 7)
        for o in full:
            self.assertTrue(o[3].startswith("history quota reached"), o[3])

        segments = hs.list_segments(self.dir)
        self.assertEqual([i for i, _ in segments], [1, 2, 3])
        for _, path in segments:
            self.assertLessEqual(path.stat().st_size, segment_bytes)
            self.assertTrue(path.read_bytes().endswith(b"\n"))
        self.assertEqual(hs.total_bytes(self.dir), 7 * length)
        self.assertLessEqual(hs.total_bytes(self.dir), total_bytes)

        # Every successful write is present exactly once; nothing extra was written.
        written = []
        for _, path in segments:
            for line in path.read_bytes().splitlines():
                written.append(json.loads(line))
        written_keys = [(r["w"], r["seq"]) for r in written]
        self.assertEqual(len(written_keys), len(set(written_keys)))
        self.assertEqual(set(written_keys), ok)


if __name__ == "__main__":
    unittest.main()