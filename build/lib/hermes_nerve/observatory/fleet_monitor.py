"""Evidence-graded Hermes process fleet, with durable sanitized observations.

Scope:
* Enumerate all discoverable Hermes CLI processes on j2 and a fixed trusted SSH
  peer (win4060), independently of plugin installation or profile.
* Correlate metadata-only Nerve hook events only to verifiable PID and launch.
* Store process history, resource samples and exact-once event deltas in a
  bounded private SQLite file. No argv, prompts, tool inputs, outputs, keys,
  full working paths, transcript content or environment variables are stored.
* Unknown metrics stay null. Process-alive never means task-healthy or idle.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from . import live_reader, process_probe, session_index

SCHEMA = "hermes-fleet-census/v1"
from hermes_nerve.paths import hermes_home

ROOT = hermes_home() / "observatory" / "history"
LIVE_ROOT = hermes_home() / "local" / "share" / "hermes-nerve-live"
REMOTE_HOST = "win4060"
INTERVAL = 12.0
MAX_RETAINED = 5000
MAX_SAMPLES = 60000
MAX_EVENTS = 100000
HISTORY_DAYS = 30
REMOTE_TIMEOUT = 6.0
MAX_JSON = 512 * 1024
REF_RE = re.compile(r"^([0-9]{1,10}):([a-f0-9]{12})#([0-9]{1,10})$")
SAFE_HOSTS = ("j2", "win4060")
_INT_FIELDS = ("pid", "ppid", "start_ticks", "threads")
_FLOAT_FIELDS = ("cpu_seconds", "rss_mib")

NOTES = [
    "Discovered means the OS process was observed, not that its assigned task is progressing.",
    "Observer details are linked by PID, start time and bounded event evidence. Historical log paths are not used to guess task ownership.",
    "Token counts are lower bounds from retained observer events, not provider bills. Costs and fallbacks are unknown without direct billing/fallback evidence.",
    "CPU is a per-process sampled single-core-equivalent percentage, not a worker token rate. A blocked process is not necessarily hung.",
    "A remote outage is UNKNOWN, never proof that its agents stopped. Missing activity telemetry is UNKNOWN, never idle.",
    "Session titles, human goals, prompts, tool arguments, and raw command lines are never collected.",
]


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_iso(value: Any) -> float | None:
    if not isinstance(value, str) or len(value) > 45:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _remote_probe() -> dict:
    """Use the exact local audited source over a pinned SSH peer; no remote writes."""
    code = Path(process_probe.__file__).read_text(encoding="utf-8")
    cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=3",
           "-o", "StrictHostKeyChecking=yes", "-o", "NumberOfPasswordPrompts=0",
           REMOTE_HOST, "python3 -"]
    try:
        proc = subprocess.run(cmd, input=code, capture_output=True, text=True,
                              timeout=REMOTE_TIMEOUT, check=False)
        if proc.returncode != 0 or len(proc.stdout) > MAX_JSON:
            raise ValueError("remote unavailable")
        response = json.loads(proc.stdout)
        if response.get("schema") != process_probe.SCHEMA or response.get("state") != "ok":
            raise ValueError("remote schema mismatch")
        return response
    except (OSError, ValueError, subprocess.TimeoutExpired, UnicodeError):
        return {"schema": process_probe.SCHEMA, "state": "unknown", "reason": "remote_probe_unavailable",
                "instances": [], "sampled_at": iso(time.time()), "truncated": False}


def _clean_row(row: dict, host: str) -> dict | None:
    if not isinstance(row, dict):
        return None
    try:
        fields = {k: row[k] for k in _INT_FIELDS}
        if not all(isinstance(v, int) and not isinstance(v, bool) and 0 <= v < 2**63
                   for v in fields.values()):
            return None
        if fields["pid"] == 0 or fields["start_ticks"] == 0:
            return None
        text = {}
        for key in ("profile", "mode", "os_state", "cwd_label"):
            val = row.get(key)
            if val is not None and (not isinstance(val, str) or len(val) > 80):
                return None
            text[key] = val
        if not process_probe.PROFILE_RE.fullmatch(text["profile"] or ""):
            return None
        if text["mode"] not in {"gateway", "chat", "proxy", "cron", "telegram", "discord",
                               "whatsapp", "plugins", "unknown"}:
            return None
        if text["os_state"] not in set("RSDTtZXI"):
            return None
        # Never emit an arbitrary cwd; only a short basename-safe label.
        if text["cwd_label"] is not None and process_probe.SAFE_LABEL.search(text["cwd_label"]):
            return None
        floats = {}
        for key in _FLOAT_FIELDS:
            v = row.get(key)
            if v is None:
                floats[key] = None
            elif isinstance(v, (int, float)) and not isinstance(v, bool) and 0 <= v <= 1e10:
                floats[key] = float(v)
            else:
                return None
        start_time = row.get("started_at")
        if start_time is not None and _parse_iso(start_time) is None:
            return None
    except (KeyError, TypeError, ValueError):
        return None
    return {**fields, **text, **floats, "host": host, "started_at": start_time,
            "key": f"{host}:{fields['pid']}:{fields['start_ticks']}",
            "source": "local_procfs" if host == "j2" else "remote_ssh_procfs"}


def _init(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=2000")
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS instances(
      key TEXT PRIMARY KEY, host TEXT NOT NULL, pid INTEGER NOT NULL,
      first_seen REAL NOT NULL, last_seen REAL NOT NULL,
      ended_at REAL, payload TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS ix_instances_seen ON instances(last_seen DESC);
    CREATE TABLE IF NOT EXISTS samples(
      key TEXT NOT NULL, observed REAL NOT NULL, cpu_pct REAL, rss_mib REAL,
      PRIMARY KEY(key, observed));
    CREATE INDEX IF NOT EXISTS ix_samples_observed ON samples(observed);
    CREATE TABLE IF NOT EXISTS events(
      ref TEXT PRIMARY KEY, key TEXT NOT NULL, observed REAL NOT NULL,
      event_at TEXT, kind TEXT, status TEXT, tool TEXT, model TEXT,
      provider TEXT, dur_ms INTEGER, usage TEXT);
    CREATE INDEX IF NOT EXISTS ix_events_key ON events(key, observed DESC);
    """)


def _base(row: dict, now: float) -> dict:
    mode = row["mode"]
    purpose = {
        "gateway": "Serve Hermes gateway requests",
        "proxy": "Provide Hermes inference proxy",
        "chat": "Run a Hermes conversation",
        "cron": "Run a scheduled Hermes job",
        "telegram": "Provide Telegram Hermes transport",
        "discord": "Provide Discord Hermes transport",
        "whatsapp": "Provide WhatsApp Hermes transport",
        "plugins": "Manage Hermes plugins",
        "unknown": "Hermes process; launch purpose not classified",
    }[mode]
    return {
        **row, "status": "running", "health": "process_observed",
        "purpose": purpose, "task_intent": None,
        "task_intent_source": "not_observed", "cpu_percent": None,
        "observer": "not_instrumented", "last_phase": None, "last_tool": None,
        "last_event_at": None, "model": None, "provider": None,
        "sessions": [], "last_observation_age_s": None,
        "tokens": {"input": None, "output": None, "cache_read": None, "cache_write": None,
                   "reasoning": None, "scope": "unobserved"},
        "calls": None, "tools": None, "errors": None, "timeouts": None,
        "fallbacks": None, "fallback_evidence": "not_instrumented",
        "cost_usd": None, "cost_evidence": "provider_billing_unavailable",
        "speed_tps": None, "last_api_ms": None,
        "first_seen": iso(now), "last_seen": iso(now), "ended_at": None,
    }


def _enrich_with_events(conn: sqlite3.Connection, by_key: dict[str, dict],
                        feed: dict, now: float) -> None:
    """Dedupe by real PID+generation+sequence; never count an event twice."""
    refs = feed.get("events", [])
    sessions = feed.get("agents", [])
    keyed = {}
    for entry in by_key.values():
        if entry["host"] == "j2":
            keyed[entry["pid"]] = entry
    for ev in reversed(refs):
        m = REF_RE.fullmatch(str(ev.get("origin_ref", "")))
        if not m:
            continue
        pid = int(m.group(1))
        row = keyed.get(pid)
        if row is None:
            continue
        ts = _parse_iso(ev.get("ts"))
        start = _parse_iso(row.get("started_at"))
        if ts is None or start is None or ts < start - 6.0 or ts > now + 5:
            continue
        kind = ev.get("kind")
        if kind not in live_reader.KINDS:
            continue
        usage = ev.get("usage") if isinstance(ev.get("usage"), dict) else None
        if usage is not None and not all(k in live_reader.USAGE_KEYS and
                                         type(v) is int and 0 <= v <= 10**9
                                         for k, v in usage.items()):
            usage = None
        status = ev.get("status") if ev.get("status") in live_reader.STATUSES else None
        tool = ev.get("tool") if isinstance(ev.get("tool"), str) and live_reader.TOOL_RE.fullmatch(ev["tool"]) else None
        model = ev.get("model") if isinstance(ev.get("model"), str) and live_reader.MODEL_RE.fullmatch(ev["model"]) else None
        provider = ev.get("provider") if isinstance(ev.get("provider"), str) and live_reader.PROVIDER_RE.fullmatch(ev["provider"]) else None
        duration = ev.get("dur_ms") if type(ev.get("dur_ms")) is int and 0 <= ev["dur_ms"] <= 86_400_000 else None
        cur = conn.execute("""INSERT OR IGNORE INTO events(ref,key,observed,event_at,kind,status,tool,model,provider,dur_ms,usage)
          VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
          (m.group(0), row["key"], now, ev["ts"], kind, status, tool, model, provider, duration,
           json.dumps(usage, sort_keys=True) if usage is not None else None))
        if cur.rowcount != 1:
            continue
        row["observer"] = "instrumented"
        row["last_phase"] = kind if tool is None else f"{kind}: {tool}"
        row["last_event_at"] = ev["ts"]
        row["last_observation_age_s"] = round(max(0.0, now - ts), 1)
        row["last_tool"] = tool or row["last_tool"]
        row["model"] = model or row["model"]
        row["provider"] = provider or row["provider"]
        row["calls"] = (row["calls"] or 0) + (1 if kind == "api_request" else 0)
        row["tools"] = (row["tools"] or 0) + (1 if kind == "tool_call" else 0)
        row["errors"] = (row["errors"] or 0) + (1 if kind == "api_error" or status == "error" else 0)
        row["timeouts"] = (row["timeouts"] or 0) + (1 if status == "timeout" else 0)
        if duration is not None and kind == "api_request":
            row["last_api_ms"] = duration
            if duration > 0 and usage is not None and "output_tokens" in usage:
                row["speed_tps"] = round(usage["output_tokens"] * 1000.0 / duration, 2)
        if kind == "api_request" and usage is not None:
            labels = {"input_tokens": "input", "output_tokens": "output",
                      "cache_read_tokens": "cache_read",
                      "cache_write_tokens": "cache_write",
                      "reasoning_tokens": "reasoning"}
            for raw, short in labels.items():
                if raw in usage:
                    row["tokens"][short] = (row["tokens"][short] or 0) + usage[raw]
            row["tokens"]["scope"] = "observed_lower_bound"
    # Sessions carry current known phase, even without a new event in this poll.
    for agent in sessions:
        m = REF_RE.fullmatch(str(agent.get("origin_ref", "")))
        if not m or agent.get("key") is None:
            continue
        row = keyed.get(int(m.group(1)))
        if row is None:
            continue
        age_ts = _parse_iso(agent.get("last_ts"))
        start = _parse_iso(row.get("started_at"))
        if age_ts is None or start is None or age_ts < start - 6:
            continue
        if agent.get("lease_status") == "live":
            row["observer"] = "instrumented"
        session = {"id": str(agent["key"])[:16], "role": agent.get("role"),
                   "phase": agent.get("phase"), "lease_status": agent.get("lease_status"),
                   "last_at": agent.get("last_ts")}
        if session not in row["sessions"] and len(row["sessions"]) < 12:
            row["sessions"].append(session)


def _apply_stats(conn: sqlite3.Connection, row: dict, now: float, previous: dict | None):
    if previous is not None:
        cpu = row.get("cpu_seconds")
        old = previous.get("cpu_seconds")
        older = _parse_iso(previous.get("last_seen"))
        if cpu is not None and old is not None and older is not None and now > older + .01:
            row["cpu_percent"] = round(max(0., (cpu - old) / (now - older) * 100), 1)
        # Persist cumulative OBSERVED counters, never copy a last state as proof
        # of currently active work.
        for key in ("calls", "tools", "errors", "timeouts", "last_phase", "last_tool",
                    "last_event_at", "model", "provider", "last_api_ms", "speed_tps"):
            row[key] = previous.get(key)
        row["tokens"] = previous.get("tokens", row["tokens"])
        row["observer"] = previous.get("observer", row["observer"])
        row["first_seen"] = previous.get("first_seen", row["first_seen"])
    row["last_seen"] = iso(now)
    row["status"] = "blocked_io" if row["os_state"] == "D" else "defunct" if row["os_state"] == "Z" else "running"
    row["health"] = "process_only" if row["observer"] != "instrumented" else "observer_linked"
    conn.execute("INSERT OR IGNORE INTO samples(key,observed,cpu_pct,rss_mib) VALUES(?,?,?,?)",
                 (row["key"], round(now, 2), row["cpu_percent"], row["rss_mib"]))


def _prune(conn: sqlite3.Connection, now: float) -> None:
    old = now - HISTORY_DAYS * 86400
    conn.execute("DELETE FROM samples WHERE observed < ?", (old,))
    conn.execute("DELETE FROM events WHERE observed < ?", (old,))
    # Keep history of active/seen processes, prune oldest ended ones first.
    conn.execute("""DELETE FROM instances WHERE key IN
       (SELECT key FROM instances WHERE ended_at IS NOT NULL AND last_seen < ?
        ORDER BY last_seen LIMIT 500)""", (old,))
    conn.execute("""DELETE FROM instances WHERE key IN
       (SELECT key FROM instances ORDER BY last_seen DESC LIMIT -1 OFFSET ?)""",
       (MAX_RETAINED,))
    conn.execute("""DELETE FROM samples WHERE rowid IN
       (SELECT rowid FROM samples ORDER BY observed DESC LIMIT -1 OFFSET ?)""", (MAX_SAMPLES,))
    conn.execute("""DELETE FROM events WHERE rowid IN
       (SELECT rowid FROM events ORDER BY observed DESC LIMIT -1 OFFSET ?)""", (MAX_EVENTS,))


class FleetMonitor:
    def __init__(self, path: str | Path, *, local_probe: Callable[[], dict] | None = None,
                 remote_probe: Callable[[], dict] | None = None,
                 feed_probe: Callable[[], dict] | None = None,
                 interval: float = INTERVAL):
        self.path = Path(path)
        self._local = local_probe or (lambda: process_probe.probe(host="j2"))
        self._remote = remote_probe or _remote_probe
        self._feed = feed_probe or (lambda: live_reader._read_agent_feed(
            str(LIVE_ROOT), time.time(), live_reader._pid_alive))
        self.interval = max(3.0, float(interval))
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last: dict = {"schema": SCHEMA, "status": "not_started",
                            "sources": {}, "instances": [], "counts": {}}
        self._error = None

    def start(self):
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(self.path.parent, 0o700)
            with sqlite3.connect(self.path) as conn:
                _init(conn)
            os.chmod(self.path, 0o600)
            self._thread = threading.Thread(target=self._run, name="nerve-fleet-census", daemon=True)
            self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=8.0)

    def _run(self):
        while not self._stop.is_set():
            try:
                self.poll()
                self._error = None
            except Exception:
                self._error = "fleet_collector_error"
            self._stop.wait(self.interval)

    def poll(self, *, at: float | None = None) -> dict:
        now = time.time() if at is None else float(at)
        try:
            local = self._local()
        except Exception:
            local = {"state": "unknown", "reason": "local_probe_unavailable", "instances": []}
        try:
            remote = self._remote()
        except Exception:
            remote = {"state": "unknown", "reason": "remote_probe_unavailable", "instances": []}
        try:
            feed = self._feed()
        except Exception:
            feed = {"agents": [], "events": [], "feed": {"state": "unknown"}}
        sources = {}
        found: dict[str, dict] = {}
        for host, report in (("j2", local), ("win4060", remote)):
            ok = (isinstance(report, dict) and report.get("schema") == process_probe.SCHEMA and
                  report.get("state") == "ok" and
                  isinstance(report.get("instances"), list))
            complete = ok and not bool(report.get("truncated"))
            sources[host] = {
                "state": "ok" if complete else "partial" if ok else "unknown",
                "reason": "verified_process_census" if complete else "scan_budget_reached" if ok else "probe_unavailable",
                "sampled_at": report.get("sampled_at") if isinstance(report, dict) else None,
                "truncated": bool(report.get("truncated")) if ok else None,
                "observed": len(report["instances"]) if ok else None,
            }
            if not ok:
                continue
            for item in report["instances"][:process_probe.MAX_ROWS]:
                row = _clean_row(item, host)
                if row is not None:
                    found[row["key"]] = row
        with self._lock, sqlite3.connect(self.path, timeout=3) as conn:
            _init(conn)
            existing = {}
            for key, payload in conn.execute("SELECT key,payload FROM instances"):
                try: existing[key] = json.loads(payload)
                except (ValueError, TypeError): pass
            current = {}
            for key, src in found.items():
                row = _base(src, now)
                previous = existing.get(key)
                _apply_stats(conn, row, now, previous)
                current[key] = row
            _enrich_with_events(conn, current, feed, now)
            # A process missing from a successfully probed host is ended.
            # When the remote probe is unavailable, its last state is UNKNOWN,
            # not ended. Historic records remain queryable.
            for key, old in existing.items():
                if key in current:
                    continue
                if old.get("ended_at"):
                    continue
                host = old.get("host")
                if host not in SAFE_HOSTS:
                    continue
                old["status"] = "ended" if sources[host]["state"] == "ok" else "remote_unreachable"
                old["health"] = "not_running" if sources[host]["state"] == "ok" else "unknown"
                if old["status"] == "ended":
                    old["ended_at"] = iso(now)
                current[key] = old
            for key, row in current.items():
                seen = _parse_iso(row["last_seen"]) or now
                first = _parse_iso(row["first_seen"]) or now
                end = _parse_iso(row.get("ended_at"))
                conn.execute("""INSERT INTO instances(key,host,pid,first_seen,last_seen,ended_at,payload)
                    VALUES(?,?,?,?,?,?,?)
                    ON CONFLICT(key) DO UPDATE SET last_seen=excluded.last_seen,
                      ended_at=excluded.ended_at,payload=excluded.payload""",
                    (key, row["host"], row["pid"], first, seen, end,
                     json.dumps(row, sort_keys=True, separators=(",", ":"))))
            _prune(conn, now)
            conn.commit()
            rows = []
            for (payload,) in conn.execute("SELECT payload FROM instances ORDER BY ended_at IS NOT NULL, last_seen DESC LIMIT 5000"):
                try: rows.append(json.loads(payload))
                except ValueError: continue
            for row in rows:
                last = _parse_iso(row.get("last_event_at"))
                row["last_observation_age_s"] = round(max(0, now-last), 1) if last is not None else None
                row["observed_age_s"] = round(max(0, now-(_parse_iso(row.get("last_seen")) or now)), 1)
            counts = {
                "running": sum(r["status"] in ("running", "blocked_io") for r in rows),
                "blocked_io": sum(r["status"] == "blocked_io" for r in rows),
                "defunct": sum(r["status"] == "defunct" for r in rows),
                "ended": sum(r["status"] == "ended" for r in rows),
                "remote_unknown": sum(r["status"] == "remote_unreachable" for r in rows),
                "observer_linked": sum(r.get("observer") == "instrumented" and
                                       r["status"] in ("running", "blocked_io") for r in rows),
                "process_only": sum(r.get("observer") != "instrumented" and
                                    r["status"] in ("running", "blocked_io") for r in rows),
                "shown": len(rows),
            }
            report = {"schema": SCHEMA, "generated_at": iso(now), "status": "ok",
                      "sources": sources, "counts": counts,
                      "instances": rows[:512], "notes": NOTES,
                      "records_total": len(rows), "records_returned": min(len(rows), 512),
                      "records_truncated": len(rows) > 512,
                      "coverage": {"all_detectable_processes": all(
                          src["state"] == "ok" for src in sources.values()),
                                   "activity_detail_requires_hooks": True,
                                   "remote_probe_is_ssh": True,
                                   "cost_pricing_source": "not_connected"}}
            self._last = report
            return report

    def snapshot(self) -> dict:
        with self._lock:
            data = json.loads(json.dumps(self._last))
            collected = _parse_iso(data.get("generated_at"))
            if collected is not None:
                age = round(max(0.0, time.time() - collected), 1)
                data["collector_age_s"] = age
                if age > self.interval * 3:
                    data["status"] = "stale"
                    data["collector_issue"] = "census_sampling_overdue"
                    # Every historical row is still available, but the UI must
                    # not mistake an old alive sample for current liveness.
                    for row in data.get("instances", []):
                        if row.get("status") in ("running", "blocked_io", "defunct"):
                            row["last_sampled_status"] = row["status"]
                            row["status"] = "unverified_stale_sample"
                    for source in data.get("sources", {}).values():
                        source["state"] = "stale"
            if self._error:
                data["collector_issue"] = self._error
            return data

    def page(self, *, offset: int = 0, limit: int = 100) -> dict:
        off = max(0, min(MAX_RETAINED, int(offset)))
        n = max(1, min(512, int(limit)))
        with self._lock, sqlite3.connect(self.path, timeout=3) as conn:
            total = conn.execute("SELECT COUNT(*) FROM instances").fetchone()[0]
            rows = [json.loads(record[0]) for record in conn.execute(
                "SELECT payload FROM instances ORDER BY ended_at IS NOT NULL, last_seen DESC LIMIT ? OFFSET ?",
                (n, off))]
            return {"schema": SCHEMA, "records_total": total,
                    "offset": off, "returned": len(rows),
                    "has_more": off + len(rows) < total,
                    "instances": rows,
                    "provenance": "Hermes process census, durable sanitized SQLite; not a session transcript"}

    def detail(self, key: str, *, limit: int = 60) -> dict | None:
        if not isinstance(key, str) or len(key) > 100 or not re.fullmatch(r"(j2|win4060):\d{1,10}:\d{1,20}", key):
            return None
        n = max(1, min(100, int(limit)))
        with self._lock, sqlite3.connect(self.path, timeout=3) as conn:
            row = conn.execute("SELECT payload FROM instances WHERE key=?", (key,)).fetchone()
            if row is None:
                return None
            samples = [{"at": iso(ts), "cpu_percent": cpu, "rss_mib": rss}
                       for ts, cpu, rss in conn.execute(
                           "SELECT observed,cpu_pct,rss_mib FROM samples WHERE key=? ORDER BY observed DESC LIMIT ?",
                           (key, n))]
            events = []
            for ref, at, kind, status, tool, model, provider, duration, usage in conn.execute(
                """SELECT ref,event_at,kind,status,tool,model,provider,dur_ms,usage
                   FROM events WHERE key=? ORDER BY observed DESC LIMIT ?""", (key, n)):
                events.append({"ref": ref, "at": at, "kind": kind, "status": status,
                               "tool": tool, "model": model, "provider": provider,
                               "dur_ms": duration, "usage": json.loads(usage) if usage else None})
            instance = json.loads(row[0])
            native = (session_index.load_profile(instance["profile"], limit=15)
                      if instance["host"] == "j2"
                      else {"schema": session_index.SCHEMA, "state": "unknown",
                            "reason": "remote_state_db_unavailable", "sessions": []})
            return {"schema": SCHEMA, "instance": instance,
                    "samples": samples, "events": events,
                    "samples_returned": len(samples), "events_returned": len(events),
                    "profile_sessions": native,
                    "profile_attribution": "PROFILE_SCOPE_ONLY_NOT_THIS_PID"}

    def profile_history(self, name: str, *, offset: int = 0, limit: int = 20) -> dict | None:
        if not isinstance(name, str) or not session_index.PROFILE.fullmatch(name):
            return None
        with self._lock:
            # Query only profiles that belong to an observed Hermes OS process.
            known = any(row.get("profile") == name and row.get("host") == "j2"
                        for row in self._last.get("instances", []))
        if not known:
            return None
        return session_index.load_profile(name, limit=limit, offset=offset)


__all__ = ["SCHEMA", "FleetMonitor", "_clean_row", "_remote_probe"]
