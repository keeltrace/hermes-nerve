"""Observe-only Nerve live telemetry writer (stdlib only).

Privacy model: callbacks copy ONLY allowlisted fields into an event. Raw
session/child IDs are replaced with HMAC-SHA256 digests (random key, mode 0600,
stored in the output directory). Prompts, messages, tool args/results, errors,
goals, summaries, headers, URLs, env vars and raw IDs are never read.

Values that fail validation are DROPPED, never truncated, so a partial secret
can never be written.
"""

from __future__ import annotations

import atexit
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Mapping, Optional

SCHEMA_EVENT = "nerve-live-event/v1"
SCHEMA_LEASE = "nerve-live-lease/v1"

ENV_ENABLE = "HERMES_NERVE_LIVE_ENABLED"
ENV_DIR = "HERMES_NERVE_LIVE_DIR"
DEFAULT_DIR = os.path.join(os.path.expanduser("~"), ".local", "share", "hermes-nerve-live")

KEY_NAME = ".hmac_key"
MAX_FILE_BYTES = 2 * 1024 * 1024      # per-PID events file; one .1 backup
MAX_CLOSED_GENERATIONS = 256          # bounded ephemeral feed (history store is separate)
MAX_CLOSED_AGE_S = 48 * 3600           # preserve recent exited sessions
MAX_LINE_BYTES = 1024                 # events are short and bounded
HEARTBEAT_INTERVAL_S = 5.0            # must be <= 10
STALE_AFTER_S = 30.0                  # lease older than this => stale
HASH_LEN = 16
MAX_TOKEN_LEN = 128
MAX_DURATION_MS = 86_400_000

HOOK_NAMES = (
    "on_session_start",
    "on_session_end",
    "post_tool_call",
    "post_api_request",
    "api_request_error",
    "subagent_start",
    "subagent_stop",
)

KINDS = frozenset({
    "session_start", "session_end", "tool_call", "api_request",
    "api_error", "subagent_start", "subagent_stop",
})

# Event fields that may appear. Anything else is never emitted.
EVENT_FIELDS = frozenset({
    "sess", "child", "tool", "model", "provider", "status", "dur_ms", "role", "usage",
})

USAGE_SOURCES = {
    "input_tokens": ("input_tokens", "prompt_tokens"),
    "output_tokens": ("output_tokens", "completion_tokens"),
    "cache_read_tokens": ("cache_read_tokens", "cache_read_input_tokens"),
    "cache_write_tokens": ("cache_write_tokens", "cache_creation_input_tokens"),
    "reasoning_tokens": ("reasoning_tokens",),
}

_STATUS_OK = frozenset({"ok", "error", "timeout", "interrupted", "completed", "failed"})
_TOOL_RE = re.compile(r"[a-z][a-z0-9_]{0,63}")
_MODEL_RE = re.compile(r"[a-z0-9][a-z0-9._:/-]{0,79}")
_PROVIDER_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,39}")
_ROLE_RE = re.compile(r"[a-z][a-z0-9_-]{0,31}")
_SECRETISH = ("sk-", "ghp_", "xox", "akia", "bearer", "token", "secret", "password", "key=")


# ---------------------------------------------------------------- validators

def _tok(value: Any, rx: "re.Pattern[str]") -> Optional[str]:
    """Return value only if it is a short, charset-allowlisted, non-secret-looking string."""
    if not isinstance(value, str) or not value or len(value) > MAX_TOKEN_LEN:
        return None
    if not rx.fullmatch(value):
        return None
    if "://" in value or "//" in value:
        return None
    low = value.lower()
    if any(p in low for p in _SECRETISH):
        return None
    return value


def _num(value: Any, lo: float, hi: float) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if value < lo or value > hi:
        return None
    return value


def _count(value: Any) -> Optional[int]:
    v = _num(value, 0, 10 ** 9)
    return None if v is None else int(v)


def _dur_ms(value: Any) -> Optional[int]:
    v = _num(value, 0, MAX_DURATION_MS)
    return None if v is None else int(v)


def _dur_ms_from_seconds(value: Any) -> Optional[int]:
    v = _num(value, 0, MAX_DURATION_MS / 1000.0)
    return None if v is None else int(round(v * 1000))


def _status(value: Any) -> str:
    return value if isinstance(value, str) and value in _STATUS_OK else "unknown"


def _usage(raw: Any) -> Dict[str, int]:
    """Copy numeric token counts only, from allowlisted source keys."""
    if not isinstance(raw, Mapping):
        return {}
    out: Dict[str, int] = {}
    for dst, sources in USAGE_SOURCES.items():
        for src in sources:
            if src in raw:
                count = _count(raw.get(src))
                if count is not None:
                    out[dst] = count
                break
    return out


def _iso(epoch: float) -> str:
    dt = datetime.fromtimestamp(epoch, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


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


# ------------------------------------------------------------------- key/IDs

def _load_or_create_key(out_dir: str) -> bytes:
    """Random 32-byte HMAC key, shared by all processes writing to out_dir.

    Created via write-temp + os.link so readers never see a partial key.
    """
    path = os.path.join(out_dir, KEY_NAME)
    for _ in range(3):
        try:
            with open(path, "rb") as fh:
                data = fh.read(4096)
            if len(data) >= 32:
                os.chmod(path, 0o600)
                return data[:32]
        except FileNotFoundError:
            pass
        tmp = f"{path}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(fd, secrets.token_bytes(32))
        finally:
            os.close(fd)
        try:
            os.link(tmp, path)
        except FileExistsError:
            pass
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass
    raise OSError("HMAC key unavailable")


# ------------------------------------------------------------------ recorder

class LiveRecorder:
    """Per-process writer: capped JSONL events plus a heartbeat lease file."""

    def __init__(
        self,
        out_dir: str,
        *,
        max_file_bytes: int = MAX_FILE_BYTES,
        clock: Callable[[], float] = time.time,
        mono: Callable[[], float] = time.monotonic,
        pid: Optional[int] = None,
    ) -> None:
        self.out_dir = os.path.abspath(out_dir)
        self.max_file_bytes = int(max_file_bytes)
        self.pid = os.getpid() if pid is None else int(pid)
        self.gen = secrets.token_hex(6)
        self._clock = clock
        self._mono = mono
        self._mono_start = mono()
        self._lock = threading.RLock()
        self._seq = 0
        self._hb_seq = 0
        self._fd: Optional[int] = None
        self._size = 0
        self._key: Optional[bytes] = None
        self._closed = False
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.dropped = 0
        self.io_errors = 0
        base = f"{self.pid}-{self.gen}"
        self.events_path = os.path.join(self.out_dir, f"events-{base}.jsonl")
        self.lease_path = os.path.join(self.out_dir, f"lease-{base}.json")

    # Bounded retention for EPHEMERAL observer events, never Nerve history.
    # Only closed/dead generations are eligible, and names must be exact.
    def _prune_closed(self) -> None:
        try:
            names = os.listdir(self.out_dir)
        except OSError:
            return
        candidates = []
        pattern = re.compile(r"^lease-([0-9]{1,12})-([0-9a-f]{12})\.json$")
        for name in names[:10000]:
            found = pattern.fullmatch(name)
            if not found:
                continue
            pid = int(found.group(1))
            if _pid_alive(pid):  # preserve every live producer, including other profiles
                continue
            path = os.path.join(self.out_dir, name)
            try:
                st = os.lstat(path)
                if not os.path.isfile(path) or not __import__("stat").S_ISREG(st.st_mode):
                    continue
                with open(path, encoding="utf-8") as fh:
                    lease = json.load(fh)
                if (lease.get("schema") != SCHEMA_LEASE or
                    lease.get("pid") != pid or lease.get("gen") != found.group(2)):
                    continue
                candidates.append((st.st_mtime, name, pid, found.group(2)))
            except (OSError, ValueError, TypeError, AttributeError):
                continue
        candidates.sort(reverse=True)
        cutoff = time.time() - MAX_CLOSED_AGE_S
        for index, (modified, name, pid, gen) in enumerate(candidates):
            if modified >= cutoff and index < MAX_CLOSED_GENERATIONS:
                continue
            for target in (name, f"events-{pid}-{gen}.jsonl", f"events-{pid}-{gen}.jsonl.1"):
                path = os.path.join(self.out_dir, target)
                try:
                    mode = os.lstat(path).st_mode
                    if __import__("stat").S_ISREG(mode):
                        os.unlink(path)
                except OSError:
                    pass

    # lifecycle -----------------------------------------------------------
    def start(self) -> None:
        os.makedirs(self.out_dir, mode=0o700, exist_ok=True)
        os.chmod(self.out_dir, 0o700)
        self._key = _load_or_create_key(self.out_dir)
        self._prune_closed()
        self.heartbeat()
        thread = threading.Thread(
            target=self._heartbeat_loop, name="nerve-live-heartbeat", daemon=True,
        )
        self._thread = thread
        thread.start()
        atexit.register(self.close)

    def _heartbeat_loop(self) -> None:
        while not self._stop.wait(HEARTBEAT_INTERVAL_S):
            try:
                self.heartbeat()
            except Exception:
                pass

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2)
        with self._lock:
            self._write_lease("exited")
            if self._fd is not None:
                try:
                    os.close(self._fd)
                except OSError:
                    pass
                self._fd = None

    # hashing -------------------------------------------------------------
    def hash_id(self, raw: Any) -> Optional[str]:
        if self._key is None or not isinstance(raw, str) or not 0 < len(raw) <= 512:
            return None
        digest = hmac.new(self._key, raw.encode("utf-8", "replace"), hashlib.sha256)
        return digest.hexdigest()[:HASH_LEN]

    # events --------------------------------------------------------------
    def emit(self, kind: str, fields: Mapping[str, Any]) -> bool:
        if kind not in KINDS:
            return False
        with self._lock:
            if self._closed:
                return False
            try:
                clean = {k: v for k, v in fields.items() if k in EVENT_FIELDS and v is not None}
                nxt = self._seq + 1
                event = {
                    "schema": SCHEMA_EVENT,
                    "gen": self.gen,
                    "pid": self.pid,
                    "seq": nxt,
                    "ts": _iso(self._clock()),
                    "kind": kind,
                }
                event.update(clean)
                line = (json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
                if len(line) > MAX_LINE_BYTES:
                    self.dropped += 1
                    return False
                self._append(line)
                self._seq = nxt
                return True
            except Exception:
                self.io_errors += 1
                return False

    def _append(self, line: bytes) -> None:
        if self._fd is None:
            self._fd = os.open(self.events_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            self._size = os.fstat(self._fd).st_size
        if self._size and self._size + len(line) > self.max_file_bytes:
            self._rotate()
        written = os.write(self._fd, line)
        if written != len(line):
            raise OSError("short write")
        self._size += written

    def _rotate(self) -> None:
        os.close(self._fd)
        self._fd = None
        os.replace(self.events_path, self.events_path + ".1")  # single backup, overwritten
        self._fd = os.open(self.events_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        self._size = 0

    # leases --------------------------------------------------------------
    def heartbeat(self) -> bool:
        with self._lock:
            if self._closed:
                return False
            self._hb_seq += 1
            return self._write_lease("running")

    def _write_lease(self, state: str) -> bool:
        try:
            record = {
                "schema": SCHEMA_LEASE,
                "gen": self.gen,
                "pid": self.pid,
                "state": state,
                "hb_seq": self._hb_seq,
                "hb_epoch": round(self._clock(), 3),
                "uptime_s": round(max(0.0, self._mono() - self._mono_start), 3),
                "interval_s": HEARTBEAT_INTERVAL_S,
            }
            data = (json.dumps(record, sort_keys=True) + "\n").encode("utf-8")
            tmp = os.path.join(self.out_dir, f".lease-{self.pid}-{self.gen}.tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                os.write(fd, data)
            finally:
                os.close(fd)
            os.replace(tmp, self.lease_path)
            return True
        except Exception:
            self.io_errors += 1
            return False


def lease_status(lease: Any, *, now: Optional[float] = None,
                 pid_alive: Callable[[int], bool] = _pid_alive) -> str:
    """Classify a lease record as live / stale / dead / exited / invalid."""
    if not isinstance(lease, Mapping) or lease.get("schema") != SCHEMA_LEASE:
        return "invalid"
    if lease.get("state") == "exited":
        return "exited"
    pid = lease.get("pid")
    hb = lease.get("hb_epoch")
    if not isinstance(pid, int) or isinstance(pid, bool) or _num(hb, 0, 4e10) is None:
        return "invalid"
    if not pid_alive(pid):
        return "dead"
    current = time.time() if now is None else now
    if hb - current > 5.0:
        return "invalid"
    if current - hb > STALE_AFTER_S:
        return "stale"
    return "live"


def read_leases(out_dir: str, *, now: Optional[float] = None) -> List[Dict[str, Any]]:
    """Return a status summary for every lease file in out_dir (read-only)."""
    results: List[Dict[str, Any]] = []
    try:
        names = sorted(os.listdir(out_dir))
    except OSError:
        return results
    for name in names:
        if not (name.startswith("lease-") and name.endswith(".json")):
            continue
        try:
            with open(os.path.join(out_dir, name), "r", encoding="utf-8") as fh:
                lease = json.load(fh)
        except (OSError, ValueError):
            lease = None
        results.append({
            "file": name,
            "status": lease_status(lease, now=now),
            "pid": lease.get("pid") if isinstance(lease, dict) else None,
            "gen": lease.get("gen") if isinstance(lease, dict) else None,
            "hb_seq": lease.get("hb_seq") if isinstance(lease, dict) else None,
        })
    return results


# ------------------------------------------------------------- hook payloads

_RECORDER: Optional[LiveRecorder] = None
_REG_LOCK = threading.Lock()


def _guard(fn: Callable[[LiveRecorder, Mapping[str, Any]], None]) -> Callable[..., None]:
    def callback(*_args: Any, **kwargs: Any) -> None:
        try:
            rec = _RECORDER
            if rec is not None:
                fn(rec, kwargs)
        except Exception:
            pass
        return None
    callback.__name__ = fn.__name__
    return callback


def _sess(rec: LiveRecorder, kw: Mapping[str, Any], key: str = "session_id") -> Dict[str, Any]:
    h = rec.hash_id(kw.get(key))
    return {"sess": h} if h else {}


@_guard
def on_session_start(rec: LiveRecorder, kw: Mapping[str, Any]) -> None:
    rec.emit("session_start", _sess(rec, kw))


@_guard
def on_session_end(rec: LiveRecorder, kw: Mapping[str, Any]) -> None:
    rec.emit("session_end", _sess(rec, kw))


@_guard
def on_post_tool_call(rec: LiveRecorder, kw: Mapping[str, Any]) -> None:
    result = kw.get("result")
    failed = isinstance(result, Mapping) and bool(result.get("error"))  # truthiness only
    fields = _sess(rec, kw)
    fields.update({
        "tool": _tok(kw.get("tool_name"), _TOOL_RE),
        "status": "error" if failed else "ok",
        "dur_ms": _dur_ms(kw.get("duration_ms")),
    })
    rec.emit("tool_call", fields)


@_guard
def on_post_api_request(rec: LiveRecorder, kw: Mapping[str, Any]) -> None:
    fields = _sess(rec, kw)
    fields.update({
        "model": _tok(kw.get("model"), _MODEL_RE),
        "provider": _tok(kw.get("provider"), _PROVIDER_RE),
        "status": "ok",
        "dur_ms": _dur_ms_from_seconds(kw.get("api_duration")),
        "usage": _usage(kw.get("usage")) or None,
    })
    rec.emit("api_request", fields)


@_guard
def on_api_request_error(rec: LiveRecorder, kw: Mapping[str, Any]) -> None:
    fields = _sess(rec, kw)
    fields.update({
        "model": _tok(kw.get("model"), _MODEL_RE),
        "provider": _tok(kw.get("provider"), _PROVIDER_RE),
        "status": "error",
    })
    rec.emit("api_error", fields)


@_guard
def on_subagent_start(rec: LiveRecorder, kw: Mapping[str, Any]) -> None:
    fields = _sess(rec, kw, "parent_session_id")
    child = rec.hash_id(kw.get("child_session_id"))
    if child:
        fields["child"] = child
    fields["role"] = _tok(kw.get("child_role"), _ROLE_RE)
    rec.emit("subagent_start", fields)


@_guard
def on_subagent_stop(rec: LiveRecorder, kw: Mapping[str, Any]) -> None:
    fields = _sess(rec, kw, "parent_session_id")
    child = rec.hash_id(kw.get("child_session_id"))
    if child:
        fields["child"] = child
    fields.update({
        "role": _tok(kw.get("child_role"), _ROLE_RE),
        "status": _status(kw.get("child_status")),
        "dur_ms": _dur_ms(kw.get("duration_ms")),
    })
    rec.emit("subagent_stop", fields)


_CALLBACKS = {
    "on_session_start": on_session_start,
    "on_session_end": on_session_end,
    "post_tool_call": on_post_tool_call,
    "post_api_request": on_post_api_request,
    "api_request_error": on_api_request_error,
    "subagent_start": on_subagent_start,
    "subagent_stop": on_subagent_stop,
}


def register(ctx: Any, environ: Optional[Mapping[str, str]] = None) -> None:
    """Register observe-only hooks iff HERMES_NERVE_LIVE_ENABLED == '1'.

    Off => returns immediately: no directory, no files, no threads, no hooks.
    Idempotent per process: a second call is a no-op.
    """
    global _RECORDER
    env = os.environ if environ is None else environ
    if env.get(ENV_ENABLE) != "1":
        return None
    with _REG_LOCK:
        if _RECORDER is not None:
            return None
        try:
            rec = LiveRecorder(env.get(ENV_DIR) or DEFAULT_DIR)
            rec.start()
        except Exception:
            return None
        _RECORDER = rec
    for name in HOOK_NAMES:
        try:
            ctx.register_hook(name, _CALLBACKS[name])
        except Exception:
            pass
    return None


def _reset_for_tests() -> None:
    global _RECORDER
    with _REG_LOCK:
        rec, _RECORDER = _RECORDER, None
    if rec is not None:
        rec.close()
