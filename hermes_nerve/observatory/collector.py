"""Read-only observatory collector over real Nerve stores.

Every value returned here comes from an existing store or from live host facts.
Missing or unreadable sources are reported as ``missing``/``unavailable``; no
fixture or placeholder operational data is ever synthesized.

Sanitization rules: event payloads, contract hashes, claim identities, prompts,
and raw shadow state are never read into the snapshot. Free-text fields are
truncated and stripped of non-printable characters.
"""

from __future__ import annotations

import os
import platform
import socket
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

from ..jsonl import append_jsonl
from . import quilt
from ..paths import report_home
from ..reflex import telemetry as shadow_telemetry
from ..work.supervisor import default_store_path

SCHEMA = "hermes-nerve-observatory/v1"
HISTORY_SCHEMA = "hermes-nerve-observatory-history/v1"
FRESH_SECONDS = 300.0
RUN_LIMIT = 200
EVENT_LIMIT = 200
SOURCE_LIMIT = 50
RECENT_EVENT_LIMIT = 100
SHADOW_RECENT_LIMIT = 20
# Shadow telemetry has no retention and report() reads the whole file on every
# snapshot. Past this size the snapshot reports the source as unavailable with a
# fixed reason instead of reading an unbounded file on each request.
SHADOW_MAX_BYTES = 256 * 1024 * 1024
TEXT_LIMIT = 120
UNRECORDED = "unrecorded"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _clean(value: Any, limit: int = TEXT_LIMIT) -> str | None:
    if value is None:
        return None
    text = "".join(ch for ch in str(value) if ch.isprintable())
    return text[:limit]


def _parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def freshness(last_at: Any, now: datetime, threshold: float = FRESH_SECONDS) -> dict[str, Any]:
    """Explicit freshness: fresh, stale, or unknown (no timestamp / unparseable)."""
    ts = _parse_ts(last_at)
    if ts is None:
        return {"state": "unknown", "age_seconds": None}
    age = max(0.0, (now - ts).total_seconds())
    return {"state": "fresh" if age <= threshold else "stale", "age_seconds": round(age, 1)}


def host_facts(now: datetime | None = None) -> dict[str, Any]:
    """Live facts about the single local host. Nothing is cached or invented."""
    now = now or _now()
    try:
        load: list[float] | None = [round(v, 2) for v in os.getloadavg()]
    except (AttributeError, OSError):
        load = None
    return {
        "id": "local",
        "live": True,
        "source": "live:os",
        "observed_at": now.isoformat(),
        "hostname": _clean(socket.gethostname(), 255),
        "system": _clean(platform.system()),
        "release": _clean(platform.release()),
        "python": platform.python_version(),
        "loadavg": load,
    }


def _open_readonly(path: Path) -> sqlite3.Connection:
    uri = "file:" + quote(str(path)) + "?mode=ro"
    con = sqlite3.connect(uri, uri=True, timeout=2.0)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only=ON")
    return con


def _query(con: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
    return list(con.execute(sql, params))


def _event_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "seq": int(row["seq"]),
        "event_id": _clean(row["event_id"], 96),
        "task_id": _clean(row["task_id"], 200),
        "run_id": int(row["run_id"]),
        "worker_id": _clean(row["worker_id"]) or None,
        "event_type": _clean(row["event_type"], 64),
        "criterion_id": _clean(row["criterion_id"], 96),
        "source": _clean(row["source"], 64),
        "stale": bool(row["stale"]),
        "created_at": _clean(row["created_at"], 64),
    }


def collect_store(path: Path, now: datetime) -> tuple[dict[str, Any], dict[str, Any]]:
    """Read the supervision store read-only. Returns (store_info, data)."""
    empty: dict[str, Any] = {"runs": [], "sources": [], "recent_events": [], "controls": {}, "diagnostics": [], "event_total": 0}
    info: dict[str, Any] = {"status": "missing", "file": _clean(path.name, 80), "error": None}
    if not path.exists():
        return info, empty
    try:
        con = _open_readonly(path)
    except sqlite3.Error as exc:
        info.update(status="unavailable", error=type(exc).__name__)
        return info, empty
    try:
        run_rows = _query(
            con,
            """SELECT task_id, run_id, worker_id, COUNT(*) AS events, SUM(stale) AS stale_events,
                      MIN(created_at) AS first_at, MAX(created_at) AS last_at,
                      GROUP_CONCAT(DISTINCT source) AS sources
               FROM work_events GROUP BY task_id, run_id, worker_id
               ORDER BY last_at DESC LIMIT ?""",
            (RUN_LIMIT,),
        )
        source_rows = _query(
            con,
            """SELECT source, COUNT(*) AS events, SUM(stale) AS stale_events, MAX(created_at) AS last_at
               FROM work_events GROUP BY source ORDER BY last_at DESC LIMIT ?""",
            (SOURCE_LIMIT,),
        )
        total = _query(con, "SELECT COUNT(*) AS n FROM work_events")[0]["n"]
        control_rows = _query(
            con,
            "SELECT task_id, run_id, control, acknowledged, created_at FROM run_controls ORDER BY created_at DESC LIMIT ?",
            (RUN_LIMIT,),
        )
        try:
            diag_rows = _query(
                con,
                "SELECT kind, COUNT(*) AS n, MAX(created_at) AS last_at FROM supervision_diagnostics GROUP BY kind ORDER BY last_at DESC LIMIT 50",
            )
        except sqlite3.OperationalError:
            diag_rows = []
        recent_rows = _query(
            con,
            """SELECT seq, event_id, task_id, run_id, worker_id, event_type, criterion_id, source, stale, created_at
               FROM work_events ORDER BY seq DESC LIMIT ?""",
            (RECENT_EVENT_LIMIT,),
        )
    except sqlite3.Error as exc:
        info.update(status="unavailable", error=type(exc).__name__)
        return info, empty
    finally:
        con.close()

    controls: dict[tuple[str, int], dict[str, Any]] = {}
    for row in control_rows:  # newest first: keep the first (latest) per run
        key = (str(row["task_id"]), int(row["run_id"]))
        controls.setdefault(
            key,
            {
                "control": _clean(row["control"], 32),
                "acknowledged": bool(row["acknowledged"]),
                "created_at": _clean(row["created_at"], 64),
            },
        )

    runs: list[dict[str, Any]] = []
    for row in run_rows:
        task_id = str(row["task_id"])
        run_id = int(row["run_id"])
        sources = sorted({s for s in str(row["sources"] or "").split(",") if s})
        runs.append(
            {
                "task_id": _clean(task_id, 200),
                "run_id": run_id,
                "worker_id": _clean(row["worker_id"]) or None,
                "events": int(row["events"]),
                "stale_events": int(row["stale_events"] or 0),
                "first_at": _clean(row["first_at"], 64),
                "last_at": _clean(row["last_at"], 64),
                "sources": [_clean(s, 64) for s in sources],
                "freshness": freshness(row["last_at"], now),
                "control": controls.get((task_id, run_id)),
            }
        )

    sources_out = [
        {
            "source": _clean(row["source"], 64),
            "events": int(row["events"]),
            "stale_events": int(row["stale_events"] or 0),
            "last_at": _clean(row["last_at"], 64),
            "freshness": freshness(row["last_at"], now),
        }
        for row in source_rows
    ]
    diagnostics = [
        {
            "kind": _clean(row["kind"], 64),
            "count": int(row["n"]),
            "last_at": _clean(row["last_at"], 64),
            "freshness": freshness(row["last_at"], now),
        }
        for row in diag_rows
    ]
    info["status"] = "ok"
    return info, {
        "runs": runs,
        "sources": sources_out,
        "recent_events": [_event_row(r) for r in recent_rows],
        "controls": controls,
        "diagnostics": diagnostics,
        "event_total": int(total),
    }


def run_events(path: Path | None, task_id: str, run_id: int, limit: int = EVENT_LIMIT) -> dict[str, Any]:
    """Chronological events for one run, sanitized. Read-only."""
    selected = Path(path) if path else default_store_path()
    base = {"task_id": _clean(task_id, 200), "run_id": int(run_id), "events": [], "truncated": False}
    if not selected.exists():
        return {**base, "status": "missing", "file": _clean(selected.name, 80)}
    try:
        con = _open_readonly(selected)
    except sqlite3.Error as exc:
        return {**base, "status": "unavailable", "error": type(exc).__name__}
    try:
        rows = _query(
            con,
            """SELECT seq, event_id, task_id, run_id, worker_id, event_type, criterion_id, source, stale, created_at
               FROM work_events WHERE task_id=? AND run_id=? ORDER BY seq DESC LIMIT ?""",
            (task_id, int(run_id), int(limit) + 1),
        )
    except sqlite3.Error as exc:
        return {**base, "status": "unavailable", "error": type(exc).__name__}
    finally:
        con.close()
    truncated = len(rows) > limit
    rows = list(reversed(rows[:limit]))
    return {**base, "status": "ok", "events": [_event_row(r) for r in rows], "truncated": truncated}


def collect_shadow(path: Path, now: datetime) -> dict[str, Any]:
    """D1/shadow decision telemetry. Authority stays with the primary Jev path."""
    info: dict[str, Any] = {"status": "missing", "file": _clean(path.name, 80), "authority": "primary-jev"}
    if not path.exists():
        return info
    try:
        size = path.stat().st_size
    except OSError as exc:
        info.update(status="unavailable", error=type(exc).__name__)
        return info
    if size > SHADOW_MAX_BYTES:
        # Fixed reason only: no size, path or content is echoed.
        info.update(status="unavailable", error="ShadowTooLarge")
        return info
    try:
        report = shadow_telemetry.report(path, recent_limit=SHADOW_RECENT_LIMIT)
    except (OSError, ValueError) as exc:
        info.update(status="unavailable", error=type(exc).__name__)
        return info
    recent = []
    for row in report.get("recent", []):
        primary = row.get("primary") if isinstance(row.get("primary"), dict) else {}
        shadow = row.get("shadow") if isinstance(row.get("shadow"), dict) else {}
        error_text = str(shadow.get("error") or "")
        recent.append(
            {
                "created_at": _clean(row.get("created_at"), 64),
                "freshness": freshness(row.get("created_at"), now),
                "question_ids": [_clean(q, 64) for q in (row.get("question_ids") or [])][:8],
                "primary": {
                    "provider": _clean(primary.get("provider"), 64),
                    "model": _clean(primary.get("model"), 96),
                    "latency_ms": primary.get("latency_ms"),
                    "provenance_status": _clean(primary.get("provenance_status"), 32),
                },
                "shadow": {
                    "provider": _clean(shadow.get("provider"), 64),
                    "model": _clean(shadow.get("model"), 96),
                    "latency_ms": shadow.get("latency_ms"),
                    "provenance_status": _clean(shadow.get("provenance_status"), 32),
                    # Only the error class is surfaced, never the endpoint's message body.
                    "error_class": _clean(error_text.split(":", 1)[0], 40) if error_text else None,
                },
                "agreements": {_clean(k, 64): bool(v) for k, v in (row.get("agreements") or {}).items()},
            }
        )
    info.update(
        {
            "status": "ok" if report.get("records") else "empty",
            "records": report["records"],
            "paired_answers": report["paired_answers"],
            "agreements": report["agreements"],
            "disagreements": report["disagreements"],
            "agreement_rate": report["agreement_rate"],
            "shadow_errors": report["shadow_errors"],
            "shadow_error_rate": report["shadow_error_rate"],
            "shadow_timeouts": report["shadow_timeouts"],
            "shadow_timeout_rate": report["shadow_timeout_rate"],
            "shadow_latency_ms": report["shadow_latency_ms"],
            "shadow_usage": report["shadow_usage"],
            "shadow_provenance": report["shadow_provenance"],
            "recent": recent,
            "freshness": freshness(recent[-1]["created_at"], now) if recent else {"state": "unknown", "age_seconds": None},
        }
    )
    return info


def build_snapshot(
    *,
    store_path: Path | str | None = None,
    shadow_path: Path | str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or _now()
    store = Path(store_path).expanduser() if store_path else default_store_path()
    shadow = Path(shadow_path).expanduser() if shadow_path else shadow_telemetry.default_path()
    store_info, data = collect_store(store, now)
    host = host_facts(now)

    # Agents are the recorded worker ids. Runs with no worker id are grouped
    # under an explicit "unrecorded" agent rather than being dropped or guessed.
    agents: dict[str, dict[str, Any]] = {}
    for run in data["runs"]:
        key = run["worker_id"] or UNRECORDED
        agent = agents.setdefault(
            key,
            {"worker_id": run["worker_id"], "key": key, "runs": 0, "events": 0, "last_at": None, "sources": set()},
        )
        agent["runs"] += 1
        agent["events"] += run["events"]
        agent["sources"].update(run["sources"])
        if run["last_at"] and (agent["last_at"] is None or run["last_at"] > agent["last_at"]):
            agent["last_at"] = run["last_at"]
    agent_list = []
    for agent in agents.values():
        agent_list.append(
            {
                **{k: v for k, v in agent.items() if k not in {"sources"}},
                "sources": sorted(agent["sources"]),
                "freshness": freshness(agent["last_at"], now),
            }
        )
    agent_list.sort(key=lambda a: a["last_at"] or "", reverse=True)

    return {
        "schema": SCHEMA,
        "generated_at": now.isoformat(),
        "freshness_threshold_seconds": FRESH_SECONDS,
        "fleet": {
            "hosts": [host],
            "note": "Single local host. The store records no remote hosts, so none are shown.",
        },
        "store": store_info,
        "event_total": data["event_total"],
        "harnesses": data["sources"],
        "agents": agent_list,
        "runs": data["runs"],
        "recent_events": data["recent_events"],
        "diagnostics": data["diagnostics"],
        "shadow": collect_shadow(shadow, now),
        # Nested zoom hierarchy built only from the real records above; leaves are real.
        "quilt": quilt.to_wire(quilt.build_tree({
            "fleet": {"hosts": [host], "note": ""},
            "harnesses": data["sources"],
            "agents": agent_list,
            "runs": data["runs"],
        })),
    }


def default_history_path() -> Path:
    return report_home() / "observatory" / "history.jsonl"


def history_row(snapshot: dict[str, Any], *, recorded_at: str | None = None) -> dict[str, Any]:
    runs = snapshot.get("runs", [])
    states = {"fresh": 0, "stale": 0, "unknown": 0}
    for run in runs:
        states[run["freshness"]["state"]] = states.get(run["freshness"]["state"], 0) + 1
    shadow = snapshot.get("shadow", {})
    return {
        "schema": HISTORY_SCHEMA,
        "recorded_at": recorded_at or _now().isoformat(),
        "snapshot_generated_at": snapshot.get("generated_at"),
        "store_status": snapshot.get("store", {}).get("status"),
        "event_total": snapshot.get("event_total"),
        "run_count": len(runs),
        "run_freshness": states,
        "shadow_status": shadow.get("status"),
        "shadow_records": shadow.get("records"),
        "shadow_errors": shadow.get("shadow_errors"),
        "shadow_timeouts": shadow.get("shadow_timeouts"),
    }


def append_history(snapshot: dict[str, Any], path: Path | None = None) -> Path:
    """Append one sanitized, derived row. Existing history is never rewritten or pruned."""
    selected = Path(path) if path else default_history_path()
    selected.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    append_jsonl(selected, history_row(snapshot))
    return selected
