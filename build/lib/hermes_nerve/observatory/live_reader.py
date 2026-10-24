"""Read-only live reader and host telemetry aggregator (stdlib only, STAGING).

Reads the files written by the staged Nerve live observer
(nerve-live-event/v1 events and nerve-live-lease/v1 leases, see
live-bridge-staging/hermes-nerve-live-observer/live_feed.py) and fetches the
loopback telemetry sidecar on 127.0.0.1:4174.

Guarantees:
- Read only. No file is created, modified, renamed or deleted.
- Only filenames matching the fixed patterns lease-<pid>-<gen>.json and
  events-<pid>-<gen>.jsonl(.1) under the caller-supplied root are opened.
  Symlinks and non-regular files are refused (O_NOFOLLOW + S_ISREG).
- Bounded: 64 lease files, 256 KiB tail per events file, 2000 events total,
  4096 directory entries scanned, 1 KiB per line, 64 KiB per HTTP body.
- Host telemetry: fixed loopback base URL, four fixed paths, 1-2 s timeout,
  no redirects, no caching. Failures are reported as unknown.
- Privacy: only allowlisted, strictly typed fields are copied out. Session and
  child IDs are the writer's HMAC digests (16 hex). Lines carrying any other
  key, a non-matching type, or a secret-looking token are rejected whole.

Public entry point: read_live_snapshot(root, *, now=None, host_fetch=None,
pid_alive=None) -> JSON-safe dict.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import stat
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

SCHEMA_SNAPSHOT = "live-reader-snapshot/v1"
SCHEMA_EVENT = "nerve-live-event/v1"
SCHEMA_LEASE = "nerve-live-lease/v1"
SCHEMA_GPU = "gpu-telemetry-v1"
SCHEMA_METRICS = "host-metrics-v1"

HOST_BASE_URL = "http://127.0.0.1:4174"
# (slot, host, fixed path, expected scope or None)
HOST_CHECKS: Tuple[Tuple[str, str, str, Optional[str]], ...] = (
    ("gpu", "j2", "/api/v1/gpu", None),
    ("gpu", "win4060", "/api/v1/hosts/win4060/gpu", None),
    ("metrics", "j2", "/api/v1/hosts/j2/metrics", "linux_host"),
    ("metrics", "win4060", "/api/v1/hosts/win4060/metrics", "wsl2_guest"),
)
ALLOWED_PATHS = frozenset(path for _, _, path, _ in HOST_CHECKS)
SCOPE_LABELS = {
    ("gpu", "j2"): "j2 host NVIDIA GPU (nvidia-smi on Linux host)",
    ("gpu", "win4060"): "win4060 NVIDIA GPU via WSL2 nvidia-smi (host-level GPU resource)",
    ("metrics", "j2"): "j2 Linux /proc CPU and RAM",
    ("metrics", "win4060"): "win4060 WSL2 GUEST /proc CPU and RAM (guest scope, not Windows host)",
}

MAX_LEASE_FILES = 64
MAX_LEASE_BYTES = 4096
TAIL_BYTES = 256 * 1024
MAX_EVENTS_TOTAL = 2000
MAX_DISPLAY_AGENTS = 200
MAX_DISPLAY_EVENTS = 200
MAX_LINE_BYTES = 1024
MAX_SCAN_ENTRIES = 4096
STALE_AFTER_S = 30.0
FUTURE_SKEW_S = 5.0
HOST_TIMEOUT_S = 1.5
HOST_MAX_BODY = 65536
HOST_STALE_S = 30.0
MAX_GPUS = 16
MAX_DURATION_MS = 86_400_000
MAX_TOKEN_LEN = 128

KINDS = frozenset({
    "session_start", "session_end", "tool_call", "api_request",
    "api_error", "subagent_start", "subagent_stop",
})
EVENT_FIELDS = frozenset({"sess", "child", "tool", "model", "provider", "status", "dur_ms", "role", "usage"})
ALLOWED_EVENT_KEYS = frozenset({"schema", "gen", "pid", "seq", "ts", "kind"}) | EVENT_FIELDS
STATUSES = frozenset({"ok", "error", "timeout", "interrupted", "completed", "failed", "unknown"})
USAGE_KEYS = frozenset({"input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "reasoning_tokens"})
LEASE_KEYS = frozenset({"schema", "gen", "pid", "state", "hb_seq", "hb_epoch", "uptime_s", "interval_s"})
METRICS_KEYS = frozenset({"schema_version", "status", "source", "host", "scope", "physical_host_metrics",
                          "sampled_at", "cpu_percent", "cpu_sample_seconds", "memory_total_mib",
                          "memory_available_mib", "error"})
GPU_KEYS = frozenset({"schema_version", "status", "source", "host", "sampled_at", "gpus", "error"})
GPU_ROW_KEYS = frozenset({"index", "name", "utilization_percent", "memory_total_mib", "memory_used_mib",
                          "temperature_celsius", "driver_version"})

PHASE_LABELS = {
    "session_start": "session started",
    "session_end": "session ended",
    "tool_call": "tool call",
    "api_request": "model response",
    "api_error": "model request error",
    "subagent_start": "subagent started",
    "subagent_stop": "subagent stopped",
}

LEASE_NAME_RE = re.compile(r"lease-(\d{1,10})-([0-9a-f]{12})\.json")
EVENTS_NAME_RE = re.compile(r"events-(\d{1,10})-([0-9a-f]{12})\.jsonl(\.1)?")
HEX16_RE = re.compile(r"[0-9a-f]{16}")
TS_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z")
TOOL_RE = re.compile(r"[a-z][a-z0-9_]{0,63}")
MODEL_RE = re.compile(r"[a-z0-9][a-z0-9._:/-]{0,79}")
PROVIDER_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,39}")
ROLE_RE = re.compile(r"[a-z][a-z0-9_-]{0,31}")
GPU_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9 .()\-]{0,79}")
DRIVER_RE = re.compile(r"[0-9][0-9.]{0,15}")
SECRETISH = ("sk-", "ghp_", "xox", "akia", "bearer", "token", "secret", "password", "key=")

NOTES = [
    "Read-only. Lease status: live = pid alive and heartbeat <30s old; stale = heartbeat >30s; "
    "dead = pid not alive; exited = writer closed cleanly; unknown = missing, invalid or clock-skewed.",
    "Agents are keyed by the writer's HMAC session digests. Labels are hash prefixes, not names.",
    "Phases are the last observed lifecycle event, not a task title. Prompts, tool args and results are never read.",
    "WSL2 CPU/RAM is guest-scoped. GPU rows are host-level NVIDIA telemetry. Neither is attributed to any agent.",
]


# --------------------------------------------------------------- validators

def _int_field(value: Any, lo: int, hi: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and lo <= value <= hi


def _float_field(value: Any, lo: float, hi: float) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(float(value)) or value < lo or value > hi:
        return None
    return float(value)


def _safe_token(value: Any, rx: "re.Pattern[str]") -> Optional[str]:
    if not isinstance(value, str) or not value or len(value) > MAX_TOKEN_LEN:
        return None
    if not rx.fullmatch(value) or "://" in value or "//" in value:
        return None
    low = value.lower()
    if any(p in low for p in SECRETISH):
        return None
    return value


def _parse_iso(value: Any) -> Optional[float]:
    """Epoch seconds for a timezone-aware ISO-8601 string, else None."""
    if not isinstance(value, str) or len(value) > 40:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        return None
    return dt.timestamp()


def _iso(epoch: float) -> str:
    dt = datetime.fromtimestamp(epoch, tz=timezone.utc)
    return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


# ------------------------------------------------------------ safe file IO

def _open_regular(path: str) -> int:
    """Open read-only, refuse symlinks and FIFOs, require a regular file."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("not a regular file")
    except OSError:
        os.close(fd)
        raise
    return fd


def _read_regular(path: str, limit: int) -> bytes:
    fd = _open_regular(path)
    try:
        data = os.read(fd, limit + 1)
        while len(data) <= limit:
            chunk = os.read(fd, limit + 1 - len(data))
            if not chunk:
                break
            data += chunk
    finally:
        os.close(fd)
    if len(data) > limit:
        raise OSError("file too large")
    return data


def _tail_lines(path: str) -> Tuple[List[bytes], Dict[str, Any]]:
    """Last TAIL_BYTES of a file as complete lines. Partial trailing line is dropped."""
    fd = _open_regular(path)
    try:
        size = os.fstat(fd).st_size
        start = max(0, size - TAIL_BYTES)
        os.lseek(fd, start, os.SEEK_SET)
        chunks: List[bytes] = []
        remaining = size - start
        while remaining > 0:
            chunk = os.read(fd, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
    finally:
        os.close(fd)
    data = b"".join(chunks)
    if start > 0:
        nl = data.find(b"\n")
        data = data[nl + 1:] if nl >= 0 else b""
    partial = bool(data) and not data.endswith(b"\n")
    parts = data.split(b"\n")
    if parts and parts[-1] == b"":
        parts.pop()
    elif partial and parts:
        parts.pop()
    return parts, {"bytes_read": len(data), "window_start": start > 0, "partial_tail": partial}


# ---------------------------------------------------------------- events

def _parse_event(raw: bytes, pid: int, gen: str, now: float) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    if len(raw) > MAX_LINE_BYTES:
        return None, "oversize"
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None, "malformed_json"
    if not isinstance(obj, dict):
        return None, "not_object"
    if not set(obj) <= ALLOWED_EVENT_KEYS:
        return None, "unknown_key"
    if obj.get("schema") != SCHEMA_EVENT:
        return None, "bad_schema"
    if not _int_field(obj.get("pid"), 0, 2 ** 31 - 1) or obj["pid"] != pid or obj.get("gen") != gen:
        return None, "identity_mismatch"
    seq = obj.get("seq")
    if not _int_field(seq, 1, 10 ** 9):
        return None, "bad_seq"
    ts = obj.get("ts")
    if not isinstance(ts, str) or not TS_RE.fullmatch(ts):
        return None, "bad_ts"
    epoch = _parse_iso(ts)
    if epoch is None:
        return None, "bad_ts"
    if epoch - now > FUTURE_SKEW_S:
        return None, "time_skew"
    kind = obj.get("kind")
    if kind not in KINDS:
        return None, "bad_kind"
    ev: Dict[str, Any] = {"kind": kind, "seq": seq, "ts": ts, "_epoch": epoch, "pid": pid, "gen": gen}
    for key in ("sess", "child"):
        if key in obj:
            if not (isinstance(obj[key], str) and HEX16_RE.fullmatch(obj[key])):
                return None, f"bad_{key}"
            ev[key] = obj[key]
    if "tool" in obj:
        tool = _safe_token(obj["tool"], TOOL_RE)
        if tool is None:
            return None, "bad_tool"
        ev["tool"] = tool
    if "model" in obj:
        model = _safe_token(obj["model"], MODEL_RE)
        if model is None:
            return None, "bad_model"
        ev["model"] = model
    if "provider" in obj:
        provider = _safe_token(obj["provider"], PROVIDER_RE)
        if provider is None:
            return None, "bad_provider"
        ev["provider"] = provider
    if "role" in obj:
        role = _safe_token(obj["role"], ROLE_RE)
        if role is None:
            return None, "bad_role"
        ev["role"] = role
    if "status" in obj:
        if obj["status"] not in STATUSES:
            return None, "bad_status"
        ev["status"] = obj["status"]
    if "dur_ms" in obj:
        if not _int_field(obj["dur_ms"], 0, MAX_DURATION_MS):
            return None, "bad_dur_ms"
        ev["dur_ms"] = obj["dur_ms"]
    if "usage" in obj:
        usage = obj["usage"]
        if not isinstance(usage, dict) or not set(usage) <= USAGE_KEYS:
            return None, "bad_usage"
        if not all(_int_field(v, 0, 10 ** 9) for v in usage.values()):
            return None, "bad_usage"
        ev["usage"] = dict(usage)
    return ev, None


def _skip(counters: Dict[str, Any], reason: str) -> None:
    counters["lines_skipped"] += 1
    counters["skipped"][reason] = counters["skipped"].get(reason, 0) + 1


def _lease_status(lease: Optional[Dict[str, Any]], reason: Optional[str], now: float,
                  alive: Callable[[int], bool]) -> Tuple[str, str]:
    if lease is None:
        return "unknown", reason or "lease_missing"
    if lease["state"] == "exited":
        return "exited", "writer_closed_cleanly"
    if lease["hb_epoch"] - now > FUTURE_SKEW_S:
        return "unknown", "heartbeat_in_future"
    if not alive(lease["pid"]):
        return "dead", "pid_not_alive"
    if now - lease["hb_epoch"] > STALE_AFTER_S:
        return "stale", "heartbeat_older_than_30s"
    return "live", "pid_alive_heartbeat_fresh"


def _read_lease(path: str, pid: int, gen: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    try:
        raw = _read_regular(path, MAX_LEASE_BYTES)
    except OSError:
        return None, "lease_unreadable"
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None, "lease_malformed"
    if not isinstance(obj, dict) or set(obj) != LEASE_KEYS:
        return None, "lease_bad_shape"
    if obj["schema"] != SCHEMA_LEASE or obj["gen"] != gen or obj["pid"] != pid:
        return None, "lease_identity_mismatch"
    if not _int_field(obj["pid"], 0, 2 ** 31 - 1):
        return None, "lease_bad_pid"
    if obj["state"] not in ("running", "exited"):
        return None, "lease_bad_state"
    if not _int_field(obj["hb_seq"], 0, 10 ** 12):
        return None, "lease_bad_hb_seq"
    hb = _float_field(obj["hb_epoch"], 1.0, 4e10)
    uptime = _float_field(obj["uptime_s"], 0.0, 1e10)
    interval = _float_field(obj["interval_s"], 0.001, 10.0)
    if hb is None or uptime is None or interval is None:
        return None, "lease_bad_numbers"
    return {"pid": pid, "gen": gen, "state": obj["state"], "hb_seq": obj["hb_seq"],
            "hb_epoch": hb, "uptime_s": uptime, "interval_s": interval}, None


def _scan_root(root: str, counters: Dict[str, Any]) -> Tuple[List[Tuple[int, str, str]],
                                                             Dict[Tuple[int, str], Dict[str, str]]]:
    leases: List[Tuple[int, str, str]] = []
    lease_mtimes: Dict[str, int] = {}
    events: Dict[Tuple[int, str], Dict[str, str]] = {}
    with os.scandir(root) as it:
        for index, entry in enumerate(it):
            if index >= MAX_SCAN_ENTRIES:
                counters["scan_cap_hit"] = True
                break
            counters["entries_scanned"] += 1
            name = entry.name
            m = LEASE_NAME_RE.fullmatch(name)
            if m and entry.is_file(follow_symlinks=False):
                try:
                    lease_mtimes[name] = entry.stat(follow_symlinks=False).st_mtime_ns
                except OSError:
                    continue
                leases.append((int(m.group(1)), m.group(2), name))
                continue
            m = EVENTS_NAME_RE.fullmatch(name)
            if m and entry.is_file(follow_symlinks=False):
                slot = "rollover" if m.group(3) else "current"
                events.setdefault((int(m.group(1)), m.group(2)), {})[slot] = name
    # Prefer the most recently written producer leases. Sorting by PID used
    # to hide recent sessions once >64 generations existed on disk.
    leases.sort(key=lambda lease: (-lease_mtimes.get(lease[2], 0), lease[0], lease[1]))
    return leases, events


def _phase(ev: Dict[str, Any]) -> str:
    label = PHASE_LABELS[ev["kind"]]
    if ev["kind"] == "tool_call" and "tool" in ev:
        return f"{label}: {ev['tool']}"
    return label


def _build_agents(events: List[Dict[str, Any]], status_by_gen: Dict[str, str]) -> Tuple[List[Dict[str, Any]], int]:
    nodes: Dict[str, Dict[str, Any]] = {}

    def node(key: str) -> Dict[str, Any]:
        if key not in nodes:
            nodes[key] = {"key": key, "parent": None, "children": set(), "role": None,
                          "first_ts": None, "last_ts": None, "_last_epoch": None, "event_count": 0,
                          "last_kind": None, "phase": None, "origin_ref": None, "last_gen": None,
                          "last_tool": None, "last_status": None, "model": None, "provider": None,
                          "tool_calls": 0, "api_requests": 0, "api_errors": 0, "dur_ms": None,
                          "usage": {}}
        return nodes[key]

    def apply(n: Dict[str, Any], ev: Dict[str, Any]) -> None:
        n["event_count"] += 1
        if n["first_ts"] is None:
            n["first_ts"] = ev["ts"]
        n["last_ts"] = ev["ts"]
        n["_last_epoch"] = ev["_epoch"]
        n["last_kind"] = ev["kind"]
        n["phase"] = _phase(ev)
        n["origin_ref"] = f"{ev['pid']}:{ev['gen']}#{ev['seq']}"
        n["last_gen"] = ev["gen"]
        if "tool" in ev:
            n["last_tool"] = ev["tool"]
        if ev["kind"] == "tool_call":
            n["tool_calls"] += 1
        if "status" in ev:
            n["last_status"] = ev["status"]
        if "model" in ev:
            n["model"] = ev["model"]
        if "provider" in ev:
            n["provider"] = ev["provider"]
        if ev["kind"] == "api_request":
            n["api_requests"] += 1
            for k, v in ev.get("usage", {}).items():
                n["usage"][k] = n["usage"].get(k, 0) + v
        if ev["kind"] == "api_error":
            n["api_errors"] += 1
        if "dur_ms" in ev:
            n["dur_ms"] = ev["dur_ms"]

    unattributed = 0
    for ev in sorted(events, key=lambda e: (e["_epoch"], e["gen"], e["seq"])):
        s = ev.get("sess")
        ch = ev.get("child")
        if not s and not ch:
            unattributed += 1
            continue
        if ch:
            cn = node(ch)
            if s and cn["parent"] is None:
                cn["parent"] = s
            if s and cn["parent"] == s:
                node(s)["children"].add(ch)
            if "role" in ev:
                cn["role"] = ev["role"]
            apply(cn, ev)
        if s:
            apply(node(s), ev)

    items = []
    for n in nodes.values():
        lease_status = status_by_gen.get(n["last_gen"], "unknown")
        ended = n["last_kind"] in ("session_end", "subagent_stop")
        children = sorted(n["children"])
        items.append({
            "label": f"agent {n['key'][:8]}",
            "key": n["key"],
            "parent": n["parent"],
            "role": n["role"],
            "children_count": len(children),
            "children": children[:20],
            "state": "ended" if ended else "open",
            "lease_status": lease_status,
            "first_ts": n["first_ts"],
            "last_ts": n["last_ts"],
            "event_count": n["event_count"],
            "phase": n["phase"],
            "last_kind": n["last_kind"],
            "origin_ref": n["origin_ref"],
            "last_tool": n["last_tool"],
            "last_status": n["last_status"],
            "tool_calls": n["tool_calls"],
            "model": n["model"],
            "provider": n["provider"],
            "api_requests": n["api_requests"],
            "api_errors": n["api_errors"],
            "dur_ms": n["dur_ms"],
            "usage": dict(sorted(n["usage"].items())) or None,
            "_sort": n["_last_epoch"] or 0.0,
        })
    items.sort(key=lambda it: (-it["_sort"], it["key"]))
    for it in items:
        del it["_sort"]
    return items, unattributed


def _read_agent_feed(root: str, now: float, alive: Callable[[int], bool]) -> Dict[str, Any]:
    counters: Dict[str, Any] = {
        "lease_files_seen": 0, "lease_files_read": 0, "entries_scanned": 0, "scan_cap_hit": False,
        "events_files_read": 0, "events_bytes_read": 0, "lines_seen": 0, "lines_skipped": 0,
        "skipped": {}, "partial_tail_lines": 0, "seq_gaps": 0, "seq_missing": 0,
        "seq_resets": 0, "seq_regress": 0, "pid_generation_changes": 0,
        "orphan_event_files": 0, "events_budget_exhausted": False, "events_unreadable": 0,
        "unattributed_events": 0,
    }
    result: Dict[str, Any] = {"feed": {}, "agents": [], "events": [], "accepted": 0, "counters": counters}
    try:
        if not os.path.isdir(root):
            raise OSError("root missing")
        leases, event_files = _scan_root(root, counters)
    except OSError:
        result["feed"] = {"state": "unknown", "reason": "root_unavailable"}
        return result

    counters["lease_files_seen"] = len(leases)
    selected = leases[:MAX_LEASE_FILES]
    counters["lease_files_read"] = len(selected)
    counters["orphan_event_files"] = sum(1 for key in event_files if key not in {(p, g) for p, g, _ in selected})
    summaries: List[Dict[str, Any]] = []
    status_by_gen: Dict[str, str] = {}
    for pid, gen, name in selected:
        lease, reason = _read_lease(os.path.join(root, name), pid, gen)
        status, why = _lease_status(lease, reason, now, alive)
        status_by_gen[gen] = status
        summaries.append({"pid": pid, "gen": gen, "status": status, "reason": why,
                          "hb_seq": lease["hb_seq"] if lease else None})
    pids: Dict[int, int] = {}
    for pid, _, _ in selected:
        pids[pid] = pids.get(pid, 0) + 1
    counters["pid_generation_changes"] = sum(c - 1 for c in pids.values() if c > 1)

    events: List[Dict[str, Any]] = []
    accepted_budget_hit = False
    for pid, gen, _ in selected:
        if accepted_budget_hit:
            break
        files = event_files.get((pid, gen), {})
        last_seq: Optional[int] = None
        for slot in ("rollover", "current"):
            name = files.get(slot)
            if not name or accepted_budget_hit:
                continue
            try:
                lines, info = _tail_lines(os.path.join(root, name))
            except OSError:
                counters["events_unreadable"] += 1
                continue
            counters["events_files_read"] += 1
            counters["events_bytes_read"] += info["bytes_read"]
            if info["partial_tail"]:
                counters["partial_tail_lines"] += 1
            for raw in lines:
                counters["lines_seen"] += 1
                if not raw.strip():
                    _skip(counters, "blank")
                    continue
                ev, reason = _parse_event(raw, pid, gen, now)
                if ev is None:
                    _skip(counters, reason or "invalid")
                    continue
                if last_seq is not None:
                    if ev["seq"] <= last_seq:
                        if ev["seq"] == 1:
                            counters["seq_resets"] += 1
                            _skip(counters, "seq_reset")
                        else:
                            counters["seq_regress"] += 1
                            _skip(counters, "seq_regress")
                        continue
                    if ev["seq"] > last_seq + 1:
                        counters["seq_gaps"] += 1
                        counters["seq_missing"] += ev["seq"] - last_seq - 1
                if len(events) >= MAX_EVENTS_TOTAL:
                    counters["events_budget_exhausted"] = True
                    accepted_budget_hit = True
                    break
                last_seq = ev["seq"]
                events.append(ev)

    if not selected:
        feed = {"state": "unknown", "reason": "no_lease_files"}
    elif all(s == "unknown" for s in status_by_gen.values()):
        feed = {"state": "unknown", "reason": "no_valid_lease"}
    else:
        feed = {"state": "available", "reason": "lease_and_event_files_read"}
    feed.update({"lease_files_seen": len(leases), "lease_files_read": len(selected),
                 "lease_files_truncated": len(leases) > MAX_LEASE_FILES, "leases": summaries})

    agents, unattributed = _build_agents(events, status_by_gen)
    counters["unattributed_events"] = unattributed
    display_events = []
    for ev in sorted(events, key=lambda e: (e["_epoch"], e["gen"], e["seq"]), reverse=True)[:MAX_DISPLAY_EVENTS]:
        item = {"ts": ev["ts"], "kind": ev["kind"], "origin_ref": f"{ev['pid']}:{ev['gen']}#{ev['seq']}",
                "lease_status": status_by_gen.get(ev["gen"], "unknown")}
        for key in ("sess", "child", "tool", "model", "provider", "status", "dur_ms", "role", "usage"):
            if key in ev:
                item[key] = ev[key]
        display_events.append(item)
    result.update({"feed": feed, "agents": agents, "events": display_events, "accepted": len(events)})
    return result


# ----------------------------------------------------------- host telemetry

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None


_OPENER = urllib.request.build_opener(_NoRedirect())


def _http_get(url: str, timeout: float) -> Tuple[int, bytes]:
    req = urllib.request.Request(url, method="GET", headers={"Accept": "application/json"})
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            return resp.status, resp.read(HOST_MAX_BODY + 1)
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read(HOST_MAX_BODY + 1)
        finally:
            exc.close()
        return exc.code, body


def _allowed_url(path: str) -> str:
    if path not in ALLOWED_PATHS:
        raise ValueError("path not allowlisted")
    url = HOST_BASE_URL + path
    if not url.startswith(HOST_BASE_URL + "/"):
        raise ValueError("url outside loopback base")
    return url


def _validate_gpu(obj: Dict[str, Any], host: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    if not set(obj) <= GPU_KEYS:
        return None, "unknown_key"
    if obj.get("schema_version") != SCHEMA_GPU or obj.get("host") != host:
        return None, "schema_or_host_mismatch"
    rows = obj.get("gpus")
    if not isinstance(rows, list) or len(rows) > MAX_GPUS:
        return None, "bad_gpu_rows"
    if not rows:
        return None, "no_gpu_rows"
    gpus = []
    for row in rows:
        if not isinstance(row, dict) or set(row) != GPU_ROW_KEYS:
            return None, "bad_gpu_row"
        name = row["name"]
        driver = row["driver_version"]
        if not _int_field(row["index"], 0, 63):
            return None, "bad_gpu_index"
        if not (isinstance(name, str) and GPU_NAME_RE.fullmatch(name)):
            return None, "bad_gpu_name"
        if not (isinstance(driver, str) and DRIVER_RE.fullmatch(driver)):
            return None, "bad_driver"
        nums = {}
        for key, hi in (("utilization_percent", 100), ("memory_total_mib", 10 ** 7),
                        ("memory_used_mib", 10 ** 7), ("temperature_celsius", 150)):
            v = row[key]
            if v is not None and not _int_field(v, 0, hi):
                return None, f"bad_{key}"
            nums[key] = v  # None = not reported by the probe; kept as unknown
        if nums["memory_total_mib"] is not None and nums["memory_used_mib"] is not None \
                and nums["memory_used_mib"] > nums["memory_total_mib"]:
            return None, "memory_used_exceeds_total"
        gpus.append({"index": row["index"], "name": name, "driver_version": driver, **nums})
    return {"gpus": gpus}, None


def _validate_metrics(obj: Dict[str, Any], host: str, scope: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    if not set(obj) <= METRICS_KEYS:
        return None, "unknown_key"
    if obj.get("schema_version") != SCHEMA_METRICS or obj.get("host") != host or obj.get("scope") != scope:
        return None, "schema_host_or_scope_mismatch"
    if obj.get("physical_host_metrics") is not False:
        return None, "physical_flag_not_false"
    cpu = _float_field(obj.get("cpu_percent"), 0.0, 100.0)
    total = obj.get("memory_total_mib")
    avail = obj.get("memory_available_mib")
    if cpu is None or not _int_field(total, 1, 10 ** 7) or not _int_field(avail, 0, total):
        return None, "bad_metrics_values"
    sample = _float_field(obj.get("cpu_sample_seconds"), 0.0, 60.0)
    return {"cpu_percent": round(cpu, 2), "memory_total_mib": total, "memory_available_mib": avail,
            "cpu_sample_seconds": sample}, None


def _host_check(slot: str, host: str, path: str, scope: Optional[str], now: float,
                fetch: Callable[[str, float], Tuple[int, bytes]]) -> Dict[str, Any]:
    out: Dict[str, Any] = {"state": "unknown", "reason": None, "host": host,
                           "scope_label": SCOPE_LABELS[(slot, host)], "sampled_at": None,
                           "age_s": None, "attribution": "none"}
    if slot == "gpu":
        out["source_scope"] = "host-level NVIDIA telemetry (not guest-scoped)"
    else:
        out["scope"] = scope
        out["physical_host_metrics"] = False
    try:
        status, body = fetch(_allowed_url(path), HOST_TIMEOUT_S)
    except Exception:  # noqa: BLE001 - any transport failure is unknown, never green
        out["reason"] = "unreachable"
        return out
    if status != 200:
        out["reason"] = f"http_{int(status)}"
        return out
    if not isinstance(body, (bytes, bytearray)) or len(body) > HOST_MAX_BODY:
        out["reason"] = "body_invalid"
        return out
    try:
        obj = json.loads(bytes(body).decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        out["reason"] = "bad_json"
        return out
    if not isinstance(obj, dict):
        out["reason"] = "bad_shape"
        return out
    if obj.get("status") != "ok":
        out["reason"] = "sidecar_reported_unknown"
        return out
    sampled = _parse_iso(obj.get("sampled_at"))
    if sampled is None:
        out["reason"] = "bad_sampled_at"
        return out
    age = now - sampled
    if age < -FUTURE_SKEW_S:
        out["reason"] = "time_skew"
        return out
    parsed, why = (_validate_gpu(obj, host) if slot == "gpu"
                   else _validate_metrics(obj, host, scope or ""))
    if parsed is None:
        out["reason"] = why
        return out
    out.update(parsed)
    out["sampled_at"] = obj["sampled_at"]
    out["age_s"] = round(max(age, 0.0), 3)
    if age > HOST_STALE_S:
        out["state"], out["reason"] = "stale", "sample_older_than_30s"
    else:
        out["state"], out["reason"] = "ok", "fresh_sample"
    return out


def _host_snapshot(now: float, fetch: Callable[[str, float], Tuple[int, bytes]]) -> Dict[str, Any]:
    block: Dict[str, Any] = {"base": HOST_BASE_URL, "gpu": {}, "metrics": {},
                             "cache": "none; every read is a fresh request"}
    for slot, host, path, scope in HOST_CHECKS:
        block[slot][host] = _host_check(slot, host, path, scope, now, fetch)
    return block


# ------------------------------------------------------------- entry point

def read_live_snapshot(root: str, *, now: Optional[float] = None,
                       host_fetch: Optional[Callable[[str, float], Tuple[int, bytes]]] = None,
                       pid_alive: Optional[Callable[[int], bool]] = None) -> Dict[str, Any]:
    """Read-only snapshot of Nerve live agents, events, and host telemetry.

    root: directory written by the live observer (caller-fixed, never derived from input).
    host_fetch / pid_alive: injectable for tests; defaults use the fixed loopback sidecar and os.kill.
    """
    ts_now = time.time() if now is None else float(now)
    alive = pid_alive or _pid_alive
    fetch = host_fetch or _http_get
    feed_block = _read_agent_feed(str(root), ts_now, alive)
    host_block = _host_snapshot(ts_now, fetch)
    agents = feed_block["agents"]
    events = feed_block["events"]
    snapshot = {
        "schema": SCHEMA_SNAPSHOT,
        "generated_at": _iso(ts_now),
        "read_only": True,
        "agent_feed": feed_block["feed"],
        "agents": {"total": len(agents), "shown": min(len(agents), MAX_DISPLAY_AGENTS),
                   "items": agents[:MAX_DISPLAY_AGENTS]},
        "events": {"accepted": feed_block["accepted"], "shown": len(events),
                   "items": events[:MAX_DISPLAY_EVENTS]},
        "counters": feed_block["counters"],
        "host": host_block,
        "notes": NOTES,
    }
    json.dumps(snapshot, allow_nan=False)  # JSON-safe guarantee; raises if not
    return snapshot


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only live snapshot (staging).")
    parser.add_argument("--root", required=True, help="observer output directory (read only)")
    args = parser.parse_args(argv)
    print(json.dumps(read_live_snapshot(args.root), indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
