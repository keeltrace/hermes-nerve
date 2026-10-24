"""Periodic, append-only history recorder for the observatory.

While the observatory server runs with an explicit opt-in interval, a single
daemon thread appends one derived history row per interval into the bounded,
segmented store (see ``history_store``). Rows hold only the sanitized
aggregate ``history_row`` fields; never raw events, prompts, tokens or
snapshots.

Guarantees:
- Disabled by default. The recorder only exists when the operator passes an
  interval; no capture runs otherwise.
- Interval is validated before any thread starts or any file is touched.
  NaN, infinite, bool, non-numeric and out-of-range values are refused.
- Append-only. This module never rewrites, prunes, rotates or truncates data.
- Quota full stops capture and sets the distinct ``history_storage_full``
  state. The live snapshot and API are unaffected.
- Capture status is separate from the live snapshot. A capture failure is
  recorded as a class name only and never hides the live snapshot.
- ``stop()`` is idempotent. The closing row is written only after the worker
  thread has definitely exited; a join timeout skips the final write so it
  cannot race with a still-running worker.
"""

from __future__ import annotations

import math
import threading
from pathlib import Path
from typing import Any

from . import collector, history_store

MIN_INTERVAL_SECONDS = 0.05
MAX_INTERVAL_SECONDS = 86400.0
DEFAULT_INTERVAL_SECONDS = 60.0
JOIN_TIMEOUT_SECONDS = 10.0

STATE_DISABLED = "disabled"
STATE_RUNNING = "running"
STATE_STOPPED = "stopped"
STATE_STORAGE_FULL = "history_storage_full"
STATE_ERROR = "error"


def validate_interval(value: Any) -> float:
    """Return a finite float interval in range, or raise ValueError before any side effect."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("history interval must be a finite number of seconds")
    interval = float(value)
    if not math.isfinite(interval):
        raise ValueError("history interval must be finite")
    if not (MIN_INTERVAL_SECONDS <= interval <= MAX_INTERVAL_SECONDS):
        raise ValueError(
            f"history interval must be between {MIN_INTERVAL_SECONDS} and {MAX_INTERVAL_SECONDS} seconds"
        )
    return interval


class HistoryRecorder:
    def __init__(
        self,
        directory: Path | str | None,
        *,
        store_path: Path | str | None = None,
        shadow_path: Path | str | None = None,
        interval: float = DEFAULT_INTERVAL_SECONDS,
    ) -> None:
        # Validate first so a bad interval never creates a directory or a thread.
        self.interval = validate_interval(interval)
        self.directory = Path(directory).expanduser() if directory else collector.default_history_path().parent / "history"
        self.store_path = store_path
        self.shadow_path = shadow_path
        self.rows_written = 0
        self.last_error: str | None = None
        self.state = STATE_DISABLED
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._finalized = False

    def write_row(self) -> bool:
        """Append one row now. Returns True on success; errors are recorded, not raised."""
        if self.state == STATE_STORAGE_FULL:
            return False
        try:
            snapshot = collector.build_snapshot(store_path=self.store_path, shadow_path=self.shadow_path)
            history_store.append_row(self.directory, collector.history_row(snapshot))
        except history_store.HistoryStorageFull:
            self.state = STATE_STORAGE_FULL
            self.last_error = "HistoryStorageFull"
            self._stop.set()
            return False
        except Exception as exc:  # Keep the loop alive; expose only the class name.
            self.last_error = type(exc).__name__
            self.state = STATE_ERROR
            return False
        self.rows_written += 1
        self.last_error = None
        if self.state == STATE_ERROR:
            self.state = STATE_RUNNING
        return True

    def _run(self) -> None:
        self.write_row()
        while not self._stop.wait(self.interval):
            if self.state == STATE_STORAGE_FULL:
                break
            self.write_row()

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("history recorder already started")
        self.state = STATE_RUNNING
        self._thread = threading.Thread(target=self._run, name="observatory-history", daemon=True)
        self._thread.start()

    def stop(self) -> bool:
        """Idempotent. Signal, join, and write the closing row only if the worker ran and exited.

        Returns True if the worker has definitely exited (or never started). A False
        return means the join timed out; no closing row is written in that case.
        """
        self._stop.set()
        if self._finalized:
            return True
        if self._thread is None:
            # Never started: no worker, so no closing row. Nothing to race with.
            self._finalized = True
            return True
        self._thread.join(timeout=JOIN_TIMEOUT_SECONDS)
        if self._thread.is_alive():
            # Worker still running: refuse a competing write. Leave final row to it.
            self.last_error = self.last_error or "JoinTimeout"
            return False
        if self.state != STATE_STORAGE_FULL:
            self.write_row()  # Closing row, only after the worker has definitely exited.
        self._finalized = True
        if self.state not in (STATE_STORAGE_FULL, STATE_ERROR):
            self.state = STATE_STOPPED
        return True

    def status(self) -> dict[str, Any]:
        """Capture status only. Deliberately separate from the live snapshot status."""
        # No directory name or path: status exposes only sanitized capture state.
        return {
            "enabled": True,
            "state": self.state,
            "interval_seconds": self.interval,
            "rows_written": self.rows_written,
            "last_error": self.last_error,
            "running": bool(self._thread and self._thread.is_alive()),
            "storage": history_store.storage_state(self.directory),
        }
