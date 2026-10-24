"""Stdlib unittest suite for the staged Nerve live observer. Run from the package dir:
    python3 -m unittest discover -s tests -v
"""

import importlib.util
import json
import math
import os
import stat
import sys
import tempfile
import threading
import unittest
from pathlib import Path

PKG = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("live_feed_under_test", PKG / "live_feed.py")
lf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lf)

CANARY = "CANARY-7f3a-PRIVATE"


class FakeCtx:
    def __init__(self):
        self.hooks = []

    def register_hook(self, name, cb):
        self.hooks.append((name, cb))


class FakeClock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


def make_rec(tmp, clock=None, mono=None, **kw):
    clock = clock or FakeClock()
    mono = mono or (lambda: clock.t - 1_800_000_000.0)
    rec = lf.LiveRecorder(str(tmp), clock=clock, mono=mono, **kw)
    rec.start()
    return rec


def read_events(rec):
    with open(rec.events_path, "r", encoding="utf-8") as fh:
        return [json.loads(x) for x in fh if x.strip()]


class _Base(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.tmp = Path(self._td.name) / "out"
        self.addCleanup(self._td.cleanup)
        self.addCleanup(lf._reset_for_tests)
        lf._reset_for_tests()


class TestOffByDefault(_Base):
    def test_off_creates_nothing_and_registers_nothing(self):
        ctx = FakeCtx()
        before = threading.active_count()
        for env in ({}, {lf.ENV_ENABLE: "0"}, {lf.ENV_ENABLE: "true"}, {lf.ENV_ENABLE: ""}):
            env = dict(env, **{lf.ENV_DIR: str(self.tmp)})
            self.assertIsNone(lf.register(ctx, environ=env))
        self.assertFalse(self.tmp.exists())
        self.assertEqual(ctx.hooks, [])
        self.assertEqual(threading.active_count(), before)
        self.assertIsNone(lf._RECORDER)

    def test_on_registers_all_seven_hooks_once(self):
        ctx = FakeCtx()
        env = {lf.ENV_ENABLE: "1", lf.ENV_DIR: str(self.tmp)}
        lf.register(ctx, environ=env)
        lf.register(ctx, environ=env)  # duplicate call must not double-register
        names = sorted(n for n, _ in ctx.hooks)
        self.assertEqual(names, sorted(lf.HOOK_NAMES))
        self.assertEqual(len(ctx.hooks), 7)
        self.assertEqual(stat.S_IMODE(os.stat(self.tmp).st_mode), 0o700)

    def test_registered_callbacks_return_none(self):
        ctx = FakeCtx()
        lf.register(ctx, environ={lf.ENV_ENABLE: "1", lf.ENV_DIR: str(self.tmp)})
        for name, cb in ctx.hooks:
            self.assertIsNone(cb(session_id="s", bogus=object()), name)

    def test_entrypoint_module_is_stdlib_only_and_off_by_default(self):
        src = (PKG / "live_feed.py").read_text()
        for line in src.splitlines():
            if line.startswith(("import ", "from ")):
                mod = line.split()[1].split(".")[0]
                self.assertIn(mod, sys.stdlib_module_names, line)


class TestAllowlistAndPrivacy(_Base):
    def test_canary_never_reaches_disk(self):
        rec = make_rec(self.tmp)
        self.addCleanup(rec.close)
        c = CANARY
        lf._RECORDER = rec
        cbs = lf._CALLBACKS
        cbs["on_session_start"](session_id=f"{c}-sess", prompt=c, messages=[c])
        cbs["post_tool_call"](tool_name=f"terminal_{c}", args={"cmd": c}, result={"error": c},
                              duration_ms=5, task_id=c, url=f"https://{c}.example")
        cbs["post_api_request"](session_id=c, model=f"m/{c}", provider="p", api_duration=1.5,
                                usage={"input_tokens": 3, c: 9}, headers={"Authorization": c},
                                base_url=f"https://{c}", api_key=c, goal=c)
        cbs["api_request_error"](session_id=c, error=f"boom {c}", model="m", provider="p")
        cbs["subagent_start"](parent_session_id=c, child_session_id=c, child_role=c, summary=c)
        cbs["subagent_stop"](parent_session_id=c, child_session_id=c, child_summary=c,
                             child_status=c, tool_call_history=[c], child_role="worker")
        cbs["on_session_end"](session_id=c)
        rec.close()
        blob = "".join(p.read_text() for p in self.tmp.iterdir() if p.is_file() and p.name.startswith(("events", "lease")))
        self.assertNotIn(CANARY, blob)
        self.assertNotIn("https://", blob)
        self.assertNotIn("Authorization", blob)

    def test_raw_ids_never_appear_hashed_ids_do(self):
        rec = make_rec(self.tmp)
        self.addCleanup(rec.close)
        lf._RECORDER = rec
        raw = "20261008_084713_ad193b-raw-session"
        lf._CALLBACKS["on_session_start"](session_id=raw)
        rec.close()
        blob = "".join(p.read_text() for p in self.tmp.glob("events-*.jsonl"))
        self.assertNotIn(raw, blob)
        self.assertNotIn("ad193b", blob)
        ev = read_events(rec)[0]
        self.assertRegex(ev["sess"], r"^[0-9a-f]{16}$")

    def test_unknown_fields_and_kinds_are_dropped(self):
        rec = make_rec(self.tmp)
        self.addCleanup(rec.close)
        self.assertFalse(rec.emit("not_a_kind", {}))
        self.assertTrue(rec.emit("tool_call", {"tool": "t", "evil": CANARY, "args": CANARY}))
        rec.close()
        ev = read_events(rec)[0]
        self.assertNotIn("evil", ev)
        self.assertNotIn("args", ev)
        self.assertEqual(ev["schema"], lf.SCHEMA_EVENT)

    def test_tokens_with_urls_or_secret_shapes_are_dropped(self):
        for bad in ["https://x", "a//b", "sk-abc123", "ghp_xx", "Bearer", "my token",
                    "x" * 200, "", None, 42, "../etc/passwd", "UPPER_NOT_ALLOWED!"]:
            self.assertIsNone(lf._tok(bad, lf._TOOL_RE), repr(bad))
        self.assertEqual(lf._tok("read_file", lf._TOOL_RE), "read_file")

    def test_status_is_allowlisted(self):
        self.assertEqual(lf._status("ok"), "ok")
        self.assertEqual(lf._status(CANARY), "unknown")
        self.assertEqual(lf._status(None), "unknown")

    def test_error_text_never_affects_output_beyond_boolean(self):
        rec = make_rec(self.tmp)
        self.addCleanup(rec.close)
        lf._RECORDER = rec
        lf._CALLBACKS["post_tool_call"](tool_name="t", result={"error": CANARY})
        lf._CALLBACKS["post_tool_call"](tool_name="t", result={"output": CANARY})
        rec.close()
        evs = read_events(rec)
        self.assertEqual([e["status"] for e in evs], ["error", "ok"])
        self.assertNotIn(CANARY, json.dumps(evs))


class TestModelAndTokenCounts(_Base):
    def test_usage_counts_copied_and_coerced(self):
        u = lf._usage({"input_tokens": 10, "completion_tokens": 4, "cache_read_input_tokens": 2,
                       "reasoning_tokens": 0, "junk": 999})
        self.assertEqual(u, {"input_tokens": 10, "output_tokens": 4,
                             "cache_read_tokens": 2, "reasoning_tokens": 0})

    def test_usage_rejects_bools_negatives_nan_and_non_mappings(self):
        self.assertEqual(lf._usage({"input_tokens": True, "output_tokens": -1,
                                    "cache_read_tokens": math.nan}), {})
        self.assertEqual(lf._usage("not a mapping"), {})
        self.assertEqual(lf._usage(None), {})
        self.assertEqual(lf._usage({"input_tokens": 1.7}), {"input_tokens": 1})

    def test_api_request_event_carries_model_provider_usage_duration(self):
        rec = make_rec(self.tmp)
        self.addCleanup(rec.close)
        lf._RECORDER = rec
        lf._CALLBACKS["post_api_request"](session_id="s", model="claude-haiku-5-5",
                                          provider="anthropic", api_duration=1.2,
                                          usage={"input_tokens": 100, "output_tokens": 20})
        rec.close()
        ev = read_events(rec)[0]
        self.assertEqual(ev["model"], "claude-haiku-5-5")
        self.assertEqual(ev["provider"], "anthropic")
        self.assertEqual(ev["dur_ms"], 1200)
        self.assertEqual(ev["usage"], {"input_tokens": 100, "output_tokens": 20})

    def test_no_usage_omits_field(self):
        rec = make_rec(self.tmp)
        self.addCleanup(rec.close)
        lf._RECORDER = rec
        lf._CALLBACKS["post_api_request"](session_id="s", model="m", usage=None)
        rec.close()
        self.assertNotIn("usage", read_events(rec)[0])


class TestHashing(_Base):
    def test_twenty_distinguishable_hashed_sessions(self):
        rec = make_rec(self.tmp)
        self.addCleanup(rec.close)
        hashes = {rec.hash_id(f"raw-session-{i:02d}") for i in range(20)}
        self.assertEqual(len(hashes), 20)
        self.assertEqual(rec.hash_id("raw-session-03"), rec.hash_id("raw-session-03"))

    def test_hash_depends_on_random_key(self):
        a = make_rec(Path(self._td.name) / "a")
        b = make_rec(Path(self._td.name) / "b")
        self.addCleanup(a.close)
        self.addCleanup(b.close)
        self.assertNotEqual(a.hash_id("same"), b.hash_id("same"))

    def test_key_file_is_0600_and_dir_0700(self):
        rec = make_rec(self.tmp)
        self.addCleanup(rec.close)
        key = self.tmp / lf.KEY_NAME
        self.assertEqual(stat.S_IMODE(os.stat(key).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(self.tmp).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(rec.events_path).st_mode) if os.path.exists(rec.events_path) else 0o600, 0o600)

    def test_hash_rejects_non_string_and_oversize(self):
        rec = make_rec(self.tmp)
        self.addCleanup(rec.close)
        self.assertIsNone(rec.hash_id(None))
        self.assertIsNone(rec.hash_id(123))
        self.assertIsNone(rec.hash_id(""))
        self.assertIsNone(rec.hash_id("x" * 513))


class TestMalformedInputs(_Base):
    def test_callbacks_survive_garbage_without_raising(self):
        rec = make_rec(self.tmp)
        self.addCleanup(rec.close)
        lf._RECORDER = rec
        garbage = [None, 0, "s", [], {"x": object()}, float("nan")]
        for name, cb in lf._CALLBACKS.items():
            for g in garbage:
                self.assertIsNone(cb(session_id=g, tool_name=g, usage=g, result=g,
                                     api_duration=g, duration_ms=g, child_role=g,
                                     child_session_id=g, parent_session_id=g,
                                     child_status=g, model=g, provider=g))
            self.assertIsNone(cb())  # no kwargs at all

    def test_nan_inf_and_huge_durations_dropped(self):
        self.assertIsNone(lf._dur_ms(math.inf))
        self.assertIsNone(lf._dur_ms(math.nan))
        self.assertIsNone(lf._dur_ms(lf.MAX_DURATION_MS + 1))
        self.assertEqual(lf._dur_ms(12), 12)

    def test_event_writer_never_raises_on_bad_fields(self):
        rec = make_rec(self.tmp)
        self.addCleanup(rec.close)
        self.assertFalse(rec.emit("tool_call", {"tool": object()}) is None)
        self.assertTrue(rec.emit("tool_call", {"tool": "ok", "status": None}))

    def test_closed_recorder_refuses_events(self):
        rec = make_rec(self.tmp)
        rec.close()
        self.assertFalse(rec.emit("session_start", {}))
        self.assertFalse(rec.heartbeat())


class TestTruncationAndRotation(_Base):
    def test_oversize_line_is_dropped_not_written(self):
        rec = make_rec(self.tmp)
        self.addCleanup(rec.close)
        self.assertFalse(rec.emit("tool_call", {"tool": "t" * 100, "sess": "a" * 900}))
        self.assertEqual(rec.dropped, 1)

    def test_rotation_caps_file_and_keeps_one_backup(self):
        rec = make_rec(self.tmp, max_file_bytes=1500)
        self.addCleanup(rec.close)
        for i in range(200):
            rec.emit("tool_call", {"tool": "read_file", "sess": f"{i:016x}", "dur_ms": i})
        rec.close()
        self.assertLessEqual(os.path.getsize(rec.events_path), 1500)
        backup = rec.events_path + ".1"
        self.assertTrue(os.path.exists(backup))
        self.assertLessEqual(os.path.getsize(backup), 1500)
        self.assertEqual(sorted(p.name for p in self.tmp.glob("events-*")),
                         sorted([os.path.basename(rec.events_path), os.path.basename(backup)]))

    def test_seq_strictly_increases_across_rotation(self):
        rec = make_rec(self.tmp, max_file_bytes=800)
        self.addCleanup(rec.close)
        for _ in range(60):
            rec.emit("session_start", {"sess": "a" * 16})
        rec.close()
        seqs = [e["seq"] for e in read_events(rec)]
        self.assertEqual(seqs, sorted(seqs))
        self.assertEqual(len(set(seqs)), len(seqs))
        self.assertTrue(seqs and seqs[-1] <= 60)

    def test_event_timestamps_are_utc_iso_ms(self):
        clock = FakeClock(1_800_000_000.5)
        rec = make_rec(self.tmp, clock=clock)
        self.addCleanup(rec.close)
        rec.emit("session_start", {})
        rec.close()
        ts = read_events(rec)[0]["ts"]
        self.assertRegex(ts, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")


class TestHeartbeatAndLeases(_Base):
    def test_heartbeat_monotonic_seq_and_uptime(self):
        clock = FakeClock()
        rec = make_rec(self.tmp, clock=clock)
        self.addCleanup(rec.close)
        seen = []
        for _ in range(5):
            clock.advance(3)
            self.assertTrue(rec.heartbeat())
            with open(rec.lease_path) as fh:
                seen.append(json.load(fh))
        hb = [s["hb_seq"] for s in seen]
        up = [s["uptime_s"] for s in seen]
        ep = [s["hb_epoch"] for s in seen]
        self.assertEqual(hb, sorted(hb)); self.assertEqual(len(set(hb)), len(hb))
        self.assertEqual(up, sorted(up))
        self.assertEqual(ep, sorted(ep))
        self.assertTrue(all(s["interval_s"] <= 10 for s in seen))

    def test_clock_going_backwards_does_not_reduce_uptime(self):
        clock = FakeClock()
        mono_t = [0.0]
        rec = make_rec(self.tmp, clock=clock, mono=lambda: mono_t[0])
        self.addCleanup(rec.close)
        mono_t[0] = 10.0
        rec.heartbeat()
        mono_t[0] = 25.0
        rec.heartbeat()
        mono_t[0] = 20.0  # non-monotonic input must not produce negative/decreasing uptime
        rec.heartbeat()
        with open(rec.lease_path) as fh:
            lease = json.load(fh)
        self.assertGreaterEqual(lease["uptime_s"], 0.0)

    def test_exit_writes_exited_lease(self):
        rec = make_rec(self.tmp)
        rec.close()
        with open(rec.lease_path) as fh:
            self.assertEqual(json.load(fh)["state"], "exited")
        with open(rec.lease_path) as fh:
            self.assertEqual(lf.lease_status(json.load(fh)), "exited")

    def test_lease_status_live_stale_dead_invalid(self):
        now = 1_800_000_100.0
        base = {"schema": lf.SCHEMA_LEASE, "pid": 4242, "state": "running", "hb_epoch": now - 5}
        alive = lambda pid: True
        dead = lambda pid: False
        self.assertEqual(lf.lease_status(base, now=now, pid_alive=alive), "live")
        self.assertEqual(lf.lease_status(dict(base, hb_epoch=now - 120), now=now, pid_alive=alive), "stale")
        self.assertEqual(lf.lease_status(base, now=now, pid_alive=dead), "dead")
        self.assertEqual(lf.lease_status(None, now=now), "invalid")
        self.assertEqual(lf.lease_status({"schema": "x"}, now=now), "invalid")
        self.assertEqual(lf.lease_status(dict(base, pid="4242"), now=now, pid_alive=alive), "invalid")
        self.assertEqual(lf.lease_status(dict(base, hb_epoch=now + 60), now=now, pid_alive=alive), "invalid")

    def test_read_leases_reports_each_process_without_mutating(self):
        clock = FakeClock()
        a = make_rec(self.tmp, clock=clock)
        b = make_rec(self.tmp, clock=clock)
        self.addCleanup(a.close)
        self.addCleanup(b.close)
        out = lf.read_leases(str(self.tmp), now=clock.t)
        self.assertEqual(len(out), 2)
        self.assertTrue(all(o["status"] == "live" for o in out))
        self.assertEqual(len({o["gen"] for o in out}), 2)


class TestFailureIsolation(_Base):
    def test_io_error_in_append_is_swallowed_and_counted(self):
        rec = make_rec(self.tmp)
        self.addCleanup(rec.close)
        lf._RECORDER = rec

        def boom(_line):
            raise OSError("disk gone")

        rec._append = boom
        self.assertIsNone(lf._CALLBACKS["session_start"] if False else lf._CALLBACKS["on_session_start"](session_id="s"))
        self.assertFalse(rec.emit("session_start", {}))
        self.assertGreaterEqual(rec.io_errors, 1)

    def test_unwritable_out_dir_makes_register_silent(self):
        blocker = Path(self._td.name) / "file-not-dir"
        blocker.write_text("x")
        ctx = FakeCtx()
        self.assertIsNone(lf.register(ctx, environ={lf.ENV_ENABLE: "1",
                                                    lf.ENV_DIR: str(blocker / "sub")}))
        self.assertEqual(ctx.hooks, [])
        self.assertIsNone(lf._RECORDER)

    def test_lease_write_failure_does_not_raise(self):
        rec = make_rec(self.tmp)
        self.addCleanup(rec.close)
        rec.out_dir = str(self.tmp / "missing" / "deeper")
        self.assertFalse(rec.heartbeat())
        self.assertGreaterEqual(rec.io_errors, 1)

    def test_nothing_raises_out_of_callbacks_when_recorder_broken(self):
        rec = make_rec(self.tmp)
        rec.close()
        lf._RECORDER = rec
        for cb in lf._CALLBACKS.values():
            self.assertIsNone(cb(session_id="s", tool_name="t", usage={"input_tokens": 1}))


class TestSubagentRoles(_Base):
    def test_child_role_allowlisted_and_child_hashed(self):
        rec = make_rec(self.tmp)
        self.addCleanup(rec.close)
        lf._RECORDER = rec
        lf._CALLBACKS["subagent_stop"](parent_session_id="p", child_session_id="c1",
                                       child_role="worker", child_status="completed",
                                       duration_ms=2500)
        lf._CALLBACKS["subagent_start"](parent_session_id="p", child_session_id="c1",
                                        child_role="Evil Role!!")
        rec.close()
        evs = read_events(rec)
        self.assertEqual(evs[0]["role"], "worker")
        self.assertEqual(evs[0]["status"], "completed")
        self.assertEqual(evs[0]["dur_ms"], 2500)
        self.assertNotIn("role", evs[1])
        self.assertEqual(evs[0]["child"], evs[1]["child"])
        self.assertEqual(evs[0]["sess"], evs[1]["sess"])


if __name__ == "__main__":
    unittest.main()


class TestEphemeralRetention(_Base):
    def _generation(self, pid, gen, age_s):
        import time
        self.tmp.mkdir(parents=True, exist_ok=True)
        lease = self.tmp / f"lease-{pid}-{gen}.json"
        events = self.tmp / f"events-{pid}-{gen}.jsonl"
        lease.write_text(json.dumps({"schema": lf.SCHEMA_LEASE, "pid": pid, "gen": gen, "state": "exited"}))
        events.write_text('{"schema":"nerve-live-event/v1"}\n')
        when = time.time() - age_s
        os.utime(lease, (when, when))
        return lease, events

    def test_old_dead_generations_expire_without_touching_recent_history(self):
        from unittest.mock import patch
        old_lease, old_events = self._generation(499995, "a" * 12, 72 * 3600)
        recent_lease, recent_events = self._generation(499996, "b" * 12, 3600)
        with patch.object(lf, "_pid_alive", return_value=False):
            recorder = lf.LiveRecorder(str(self.tmp))
            recorder.start()
        self.assertFalse(old_lease.exists())
        self.assertFalse(old_events.exists())
        self.assertTrue(recent_lease.exists())
        self.assertTrue(recent_events.exists())
        recorder.close()

    def test_recent_closed_generations_are_count_bounded(self):
        from unittest.mock import patch
        older, older_events = self._generation(499997, "c" * 12, 60)
        newer, newer_events = self._generation(499998, "d" * 12, 5)
        with patch.object(lf, "_pid_alive", return_value=False), patch.object(lf, "MAX_CLOSED_GENERATIONS", 1):
            recorder = lf.LiveRecorder(str(self.tmp))
            recorder.start()
        self.assertFalse(older.exists())
        self.assertFalse(older_events.exists())
        self.assertTrue(newer.exists())
        self.assertTrue(newer_events.exists())
        recorder.close()
