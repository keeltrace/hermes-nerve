"""Bounded, append-only, segmented history store for the observatory.

Layout: a directory of segment files named ``history-000001.jsonl`` etc. Each
segment is capped at ``SEGMENT_BYTES`` and the directory total at
``TOTAL_BYTES``. Guarantees:

- Append-only. Existing segments are never deleted, pruned, truncated or
  rewritten. Recovery is read-only: a torn tail is skipped, not repaired.
- Torn-tail safety. A segment whose final byte is not a newline (a torn write)
  is never appended to. The writer rolls to a new segment instead, so a valid
  row can never be glued onto a partial row.
- Cross-process writers. Quota check, segment selection and append run under an
  exclusive ``flock`` on ``.history.lock`` in the directory, so writers in
  different processes cannot both pick the same segment or overrun the quota.
  (POSIX only; ``fcntl`` is required.)
- Quota full is a distinct, non-destructive state (``HistoryStorageFull``).
  The writer stops; readers continue to work.
- Readers work on physical lines. A line longer than ``MAX_LINE_BYTES`` is
  drained incrementally within the byte budget and counted as one skipped
  line; its suffix is never parsed as a row. Malformed or torn lines are skipped
  and counted, never raised to the HTTP layer.
- Pagination uses a stable cursor of the form ``<segment>:<byte_offset>``. A
  cursor offset must be 0 or immediately follow a newline in an existing
  segment. Each request is bounded by ``MAX_SCAN_BYTES`` and ``MAX_SCAN_LINES``;
  when the scan budget is exhausted the response carries a continuation cursor
  at the next boundary or with a discard-only :d state, so no valid row is lost and no skipped
  line is counted twice.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from ..jsonl import append_jsonl

SEGMENT_BYTES = 16 * 1024 * 1024
TOTAL_BYTES = 256 * 1024 * 1024
MAX_PAGE_SIZE = 200
DEFAULT_PAGE_SIZE = 50
MAX_LINE_BYTES = 64 * 1024
# Strict per-request scan budget, including continuation cursor validation.
MAX_SCAN_BYTES = 1024 * 1024
MAX_SCAN_LINES = 2000
_DRAIN_CHUNK = 64 * 1024
LOCK_FILE_NAME = ".history.lock"

_SEGMENT_RE = re.compile(r"^history-(\d{6})\.jsonl$")
_CURSOR_RE = re.compile(r"^(\d{6}):(\d{1,12})$")


class HistoryStorageFull(RuntimeError):
    """The total history quota is reached. Capture stops; reads continue."""


class InvalidCursor(ValueError):
    """A pagination cursor failed validation."""


def _segment_name(index: int) -> str:
    return f"history-{index:06d}.jsonl"


def list_segments(directory: Path) -> list[tuple[int, Path]]:
    """Return (index, path) for every segment file, sorted by index. Read-only."""
    if not directory.is_dir():
        return []
    found: list[tuple[int, Path]] = []
    for entry in directory.iterdir():
        match = _SEGMENT_RE.match(entry.name)
        if match and entry.is_file():
            found.append((int(match.group(1)), entry))
    return sorted(found)


def total_bytes(directory: Path) -> int:
    return sum(path.stat().st_size for _, path in list_segments(directory))


def _tail_is_clean(path: Path) -> bool:
    """True when the file is empty or ends with a newline (no torn tail)."""
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return True
    if size == 0:
        return True
    with path.open("rb") as handle:
        handle.seek(size - 1)
        return handle.read(1) == b"\n"


@dataclass(frozen=True)
class AppendResult:
    segment: int
    path: Path


_APPEND_GUARD = threading.Lock()


@contextmanager
def _append_lock(directory: Path) -> Iterator[None]:
    """Serialize writers across threads AND processes.

    ``threading.Lock`` only covers threads of one process. The ``flock`` on a
    dedicated lock file covers every process that opens the same directory.
    The lock file is not a segment and is never counted, read or removed.
    """
    with _APPEND_GUARD:
        fd = os.open(directory / LOCK_FILE_NAME, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)


def append_row(directory: Path, record: dict[str, Any]) -> AppendResult:
    """Append one row to the newest clean segment, rolling when needed.

    The quota check, segment selection and append happen under the cross-process
    lock. Raises ``HistoryStorageFull`` before writing anything if the row would
    not fit the total quota. Never touches existing bytes.
    """
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    payload = (json.dumps(record, sort_keys=True, ensure_ascii=False, default=str) + "\n").encode("utf-8")
    if len(payload) > MAX_LINE_BYTES:
        raise ValueError("history row exceeds the per-line bound")
    with _append_lock(directory):
        segments = list_segments(directory)
        used = sum(path.stat().st_size for _, path in segments)
        if used + len(payload) > TOTAL_BYTES:
            raise HistoryStorageFull("history quota reached; capture stopped, existing segments kept")

        if segments:
            index, current = segments[-1]
            size = current.stat().st_size
            if size + len(payload) <= SEGMENT_BYTES and _tail_is_clean(current):
                append_jsonl(current, record)
                return AppendResult(index, current)
            next_index = index + 1
        else:
            next_index = 1
        target = directory / _segment_name(next_index)
        if target.exists():
            raise HistoryStorageFull("next segment already exists; refusing to overwrite")
        append_jsonl(target, record)
        return AppendResult(next_index, target)


def parse_cursor(raw: str | None) -> tuple[int, int, bool] | None:
    if raw is None or raw == "":
        return None
    if not isinstance(raw, str):
        raise InvalidCursor("malformed cursor")
    match = re.fullmatch(r"(\d{6}):(\d{1,12})(:d)?", raw)
    if not match:
        raise InvalidCursor("malformed cursor")
    return int(match.group(1)), int(match.group(2)), bool(match.group(3))


def format_cursor(segment: int, offset: int, draining: bool = False) -> str:
    return f"{segment:06d}:{offset}" + (":d" if draining else "")


def read_page(directory: Path, *, cursor: str | None = None,
              limit: int = DEFAULT_PAGE_SIZE) -> dict[str, Any]:
    """Read bounded payload bytes; :d cursors resume discard-only long lines.

    An overlong physical line is counted once when first detected. Continuation
    pages drain it without parsing any suffix or counting it again. Boundary
    cursors retain the legacy segment:offset representation. No files are written.
    """
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_PAGE_SIZE:
        raise InvalidCursor("invalid page size")
    if MAX_SCAN_BYTES < MAX_LINE_BYTES + 2 or MAX_SCAN_LINES < 1:
        raise ValueError("scan budget must fit cursor validation and one bounded line plus overflow")
    start = parse_cursor(cursor)
    segments = list_segments(directory)
    segment_ids = {index for index, _ in segments}
    if start is not None and start[0] not in segment_ids:
        raise InvalidCursor("cursor segment does not exist")
    rows: list[dict[str, Any]] = []
    skipped = scanned_bytes = scanned_lines = 0

    def page(next_cursor: str | None) -> dict[str, Any]:
        return {"rows": rows, "next_cursor": next_cursor, "skipped": skipped,
                "scanned_bytes": scanned_bytes, "scanned_lines": scanned_lines}

    for index, path in segments:
        if start is not None and index < start[0]:
            continue
        at_start = start is not None and index == start[0]
        offset = start[1] if at_start else 0
        draining = start[2] if at_start else False
        with path.open("rb") as handle:
            size = os.fstat(handle.fileno()).st_size
            if offset > size:
                raise InvalidCursor("cursor offset is outside the segment")
            if offset:
                handle.seek(offset - 1)
                previous = handle.read(1)
                scanned_bytes += len(previous)
                if not draining and previous != b"\n":
                    raise InvalidCursor("cursor offset is not at a line boundary")
                if draining and previous == b"\n":
                    raise InvalidCursor("drain cursor is already at a line boundary")
            elif draining:
                raise InvalidCursor("drain cursor cannot begin at zero")
            handle.seek(offset)
            position = offset
            while position < size:
                remaining = MAX_SCAN_BYTES - scanned_bytes
                if len(rows) >= limit or remaining <= 0 or (not draining and scanned_lines >= MAX_SCAN_LINES):
                    return page(format_cursor(index, position, draining))
                if draining:
                    raw = handle.readline(min(_DRAIN_CHUNK, remaining, size - position))
                    if not raw:
                        break
                    position += len(raw)
                    scanned_bytes += len(raw)
                    if raw.endswith(b"\n"):
                        draining = False
                    continue
                # Reserve enough budget to distinguish a legal line from an
                # oversized one. Prior progress makes deferral safe.
                if remaining < MAX_LINE_BYTES + 1:
                    return page(format_cursor(index, position))
                raw = handle.readline(min(MAX_LINE_BYTES + 1, size - position))
                if not raw:
                    break
                position += len(raw)
                scanned_bytes += len(raw)
                scanned_lines += 1
                if len(raw) > MAX_LINE_BYTES:
                    skipped += 1
                    draining = not raw.endswith(b"\n")
                    continue
                if not raw.endswith(b"\n"):
                    skipped += 1
                    break
                try:
                    value = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    skipped += 1
                    continue
                if not isinstance(value, dict):
                    skipped += 1
                    continue
                rows.append(value)
    return page(None)


def storage_state(directory: Path) -> dict[str, Any]:
    """Explicit storage state for status output. Never raises on read."""
    try:
        used = total_bytes(directory)
        count = len(list_segments(directory))
    except OSError as exc:
        return {"state": "unavailable", "error": type(exc).__name__}
    state = "full" if used >= TOTAL_BYTES else "ok"
    return {
        "state": state,
        "segments": count,
        "bytes_used": used,
        "bytes_quota": TOTAL_BYTES,
        "segment_bytes": SEGMENT_BYTES,
    }


__all__ = [
    "AppendResult",
    "HistoryStorageFull",
    "InvalidCursor",
    "MAX_SCAN_BYTES",
    "MAX_SCAN_LINES",
    "SEGMENT_BYTES",
    "TOTAL_BYTES",
    "append_row",
    "list_segments",
    "parse_cursor",
    "read_page",
    "storage_state",
]