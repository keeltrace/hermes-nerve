"""Nonblocking advisory scheduler (Nerve 0.4.1, P0).

Advisory model evaluations must NEVER wait on remote inference on the
synchronous pre_tool_call path (Hermes abandons hook workers after
plugins.hook_callback_timeout, default 30s, then fails closed — skipping
subsequent tools). This module owns all deferred advisory work:

- bounded queue (drop-on-saturation) served by one supervised daemon worker,
- REAL per-evaluation wall-clock deadline: each evaluation runs on its own
  tracked daemon thread and the worker joins it for ADVISORY_CALL_TIMEOUT,
  so a hung provider cannot occupy the worker indefinitely,
- bounded abandonment: at most MAX_ABANDONED live overrunning evaluations
  exist at any time; beyond that new evaluations are refused (backpressure)
  instead of spawning unlimited replacement threads,
- correlation-ID deduplication (no duplicate evaluations for one tool call),
- provider circuit breaker: a stall opens a cooldown so an outage cannot
  cause retry storms; recovery is automatic once the cooldown expires,
- results are recorded as redacted telemetry ONLY. They can never
  retroactively authorize, block, or cancel a tool call,
- bounded, non-interfering shutdown; daemon threads cannot outlive the
  process and never block session exit,
- no Nerve locks are held across network I/O.
"""
from __future__ import annotations

import json
import os
import queue
import threading
import time
from typing import Any, Callable

from .paths import hermes_home


MAX_QUEUE = 32
_COOLDOWN_S = 300.0
_SHUTDOWN = object()


def _env_float(name: str, default: float, lo: float, hi: float) -> float:
    try:
        return max(lo, min(hi, float(os.getenv(name, "") or default)))
    except ValueError:
        return default


_ADVISORY_CALL_TIMEOUT = _env_float("NERVE_ADVISORY_CALL_TIMEOUT", 8.0, 1.0, 25.0)
MAX_ABANDONED = int(_env_float("NERVE_ADVISORY_MAX_ABANDONED", 2.0, 1.0, 4.0))

_state_lock = threading.Lock()
_queue: "queue.Queue[Any]" = queue.Queue(maxsize=MAX_QUEUE)
_dedup: set[str] = set()
_cooldown_until = 0.0
_processed = 0
_errors = 0
_dropped = 0
_scheduled = 0
_stalls = 0
_refused = 0
_eval_threads_spawned = 0
_abandoned: list[threading.Thread] = []
_worker: threading.Thread | None = None
_stop = threading.Event()


# --- bounded evaluation execution ------------------------------------------------

def _reap_locked() -> int:
    """Drop finished evaluation threads; return the live overrun count."""
    _abandoned[:] = [t for t in _abandoned if t.is_alive()]
    return len(_abandoned)


def run_bounded(fn: Callable[[], Any], deadline: float) -> tuple[Any, BaseException | None, bool]:
    """Run `fn` on a tracked daemon thread; join at most `deadline` seconds.

    Returns (result, error, stalled). Threads are bounded: if MAX_ABANDONED
    evaluations are still running past their deadline, no new thread is
    spawned and (None, TimeoutError, True) is returned. An abandoned
    evaluation finishes on its own and is reaped; it never blocks the caller.
    """
    global _eval_threads_spawned, _refused
    with _state_lock:
        if _reap_locked() >= MAX_ABANDONED:
            _refused += 1
            return None, TimeoutError("advisory evaluation capacity exhausted"), True
        box: dict[str, Any] = {}

        def _target() -> None:
            try:
                box["result"] = fn()
            except BaseException as exc:  # noqa: BLE001 - reported to caller
                box["error"] = exc

        _eval_threads_spawned += 1
        thread = threading.Thread(
            target=_target, daemon=True,
            name=f"nerve-advisory-eval-{_eval_threads_spawned}")
        _abandoned.append(thread)
        thread.start()
    thread.join(timeout=deadline)
    with _state_lock:
        _reap_locked()
        live = thread.is_alive()
    if live:
        return None, TimeoutError(
            f"advisory evaluation exceeded {deadline:.1f}s deadline"), True
    return box.get("result"), box.get("error"), False


# --- lifecycle ------------------------------------------------------------------

def reset_for_tests() -> None:
    """Stop the current worker (bounded join), drain, reset counters, restart.

    Thread ownership is explicit: the previous worker is joined before a new
    one is created, so repeated initialization cannot accumulate workers.
    """
    global _worker, _processed, _errors, _dropped, _scheduled, _stalls, _refused
    global _eval_threads_spawned, _cooldown_until
    _stop_worker()
    with _state_lock:
        _dedup.clear()
        _processed = _errors = _dropped = _scheduled = _stalls = _refused = 0
        _eval_threads_spawned = 0
        _cooldown_until = 0.0
        _worker = None
        try:
            while True:
                _queue.get_nowait()
        except queue.Empty:
            pass
        _reap_locked()
    _ensure_worker()


def _stop_worker(timeout: float = 3.0) -> None:
    global _worker
    _stop.set()
    try:
        _queue.put_nowait(_SHUTDOWN)
    except queue.Full:
        pass
    worker = _worker
    if worker is not None and worker.is_alive():
        worker.join(timeout=timeout)
    _worker = None


def reset_provider_state() -> None:
    """Clear the circuit breaker (session reset / test isolation)."""
    global _cooldown_until
    with _state_lock:
        _cooldown_until = 0.0


def cooldown_remaining_s() -> float:
    return max(0.0, _cooldown_until - time.monotonic())


def health() -> dict[str, Any]:
    with _state_lock:
        abandoned = _reap_locked()
        return {
            "queue_depth": _queue.qsize(),
            "scheduled": _scheduled,
            "processed": _processed,
            "errors": _errors,
            "dropped": _dropped,
            "stalls": _stalls,
            "refused": _refused,
            "dedup_keys": len(_dedup),
            "cooldown_remaining_s": round(cooldown_remaining_s(), 1),
            "worker_alive": bool(_worker and _worker.is_alive()),
            "eval_threads_spawned": _eval_threads_spawned,
            "abandoned_live": abandoned,
            "max_abandoned": MAX_ABANDONED,
            "call_timeout_s": _ADVISORY_CALL_TIMEOUT,
        }


def note_provider_failure() -> None:
    """Open the circuit breaker after a provider stall."""
    global _cooldown_until
    _cooldown_until = time.monotonic() + _COOLDOWN_S


def note_provider_success() -> None:
    global _cooldown_until
    _cooldown_until = 0.0


def provider_available() -> bool:
    return cooldown_remaining_s() <= 0.0


# --- submission -----------------------------------------------------------------

def submit(tool_name: str, redacted_state: dict[str, Any],
           evaluate: Callable[[], Any], record: Callable[[dict[str, Any]], None],
           correlation: dict[str, str]) -> bool:
    """Schedule an advisory evaluation. Returns False when dropped.

    `evaluate` is executed on a bounded daemon thread owned by the worker
    (never the tool pipeline); `record` receives a redacted telemetry dict.
    Neither the verdict nor any derived directive reaches the tool pipeline.
    """
    global _scheduled, _dropped
    if not provider_available():
        return False
    key = str(correlation.get("tool_call_id") or f"{tool_name}:{time.monotonic_ns()}")
    with _state_lock:
        if key in _dedup:
            return False
        _dedup.add(key)
        _scheduled += 1
        try:
            _queue.put_nowait((key, tool_name, redacted_state, evaluate, record, dict(correlation)))
        except queue.Full:
            _dropped += 1
            _dedup.discard(key)
            return False
    _ensure_worker()
    return True


def _ensure_worker() -> None:
    global _worker
    with _state_lock:
        if _worker is not None and _worker.is_alive():
            return
        _stop.clear()
        _worker = threading.Thread(target=_work_loop, name="nerve-advisory", daemon=True)
        _worker.start()


def shutdown(timeout: float = 3.0) -> None:
    """Bounded shutdown on session end; daemon threads never block exit."""
    _stop_worker(timeout=timeout)


# --- worker ---------------------------------------------------------------------

def _work_loop() -> None:
    global _processed, _errors, _stalls
    while not _stop.is_set():
        try:
            item = _queue.get(timeout=0.5)
        except queue.Empty:
            continue
        if item is _SHUTDOWN:
            return
        key, tool_name, _redacted_state, evaluate, record, correlation = item
        try:
            result, error, stalled = run_bounded(evaluate, _ADVISORY_CALL_TIMEOUT)
            if stalled:
                with _state_lock:
                    _stalls += 1
                note_provider_failure()  # cooldown: next submits are refused
                record(_redact_result(tool_name, None, correlation, "ProviderTimeout"))
                continue
            if error is not None:
                with _state_lock:
                    _errors += 1
                    if isinstance(error, TimeoutError):
                        note_provider_failure()
                record(_redact_result(tool_name, None, correlation,
                                      f"{type(error).__name__}"))
                continue
            with _state_lock:
                _processed += 1
            record(_redact_result(tool_name, result, correlation, None))
        except Exception as exc:  # telemetry/record errors must not kill the worker
            with _state_lock:
                _errors += 1
            try:
                record(_redact_result(tool_name, None, correlation,
                                      f"{type(exc).__name__}"))
            except Exception:
                pass
        finally:
            with _state_lock:
                _dedup.discard(key)


def _redact_result(tool_name: str, result: Any, correlation: dict[str, str],
                   error: str | None) -> dict[str, Any]:
    """Telemetry-only record. No verdict text, no arguments, no provider body."""
    out: dict[str, Any] = {
        "schema": "nerve-advisory-async/v1",
        "tool_name": tool_name,
        "timestamp": time.time(),
        **{k: v for k, v in correlation.items() if k in ("turn_id", "session_id", "tool_call_id")},
    }
    if error is not None:
        out["outcome"] = "provider-error"
        out["error_type"] = error
    else:
        out["outcome"] = "evaluated-advisory-async"
        try:
            value = getattr(result, "value", None)
            latency = getattr(result, "latency_ms", None)
            model = str(getattr(result, "model", ""))[:64]
            if value is not None:
                out["advisory_value"] = str(value)[:16]
            if latency is not None:
                out["latency_ms"] = round(float(latency), 1)
            if model:
                out["model"] = model
        except Exception:
            pass
    return out


def evaluate_advisory(tool_name: str, args: dict[str, Any], task_id: str | None,
                      evaluate_fn: Callable[[], Any]) -> Callable[[], Any]:
    """Wrap an evaluation so any caller gets the real deadline guarantee."""
    def run() -> Any:
        result, error, stalled = run_bounded(evaluate_fn, _ADVISORY_CALL_TIMEOUT)
        if stalled:
            raise TimeoutError(f"advisory evaluation for {tool_name} exceeded deadline")
        if error is not None:
            raise error
        return result
    return run


def write_health_snapshot() -> None:
    """Persist an observable health metric (bounded, sanitized)."""
    try:
        path = hermes_home() / "nerve" / "advisory-health.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(health(), indent=1), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass
