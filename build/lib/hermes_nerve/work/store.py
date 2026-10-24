from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .models import DoDContract, EvidenceRecord, RunIdentity, TrajectoryAssessment, WorkEvent


SCHEMA_VERSION = 2


class SupervisionStore:
    """Controller-side supervisory persistence.

    This store intentionally contains no canonical task status, dependency graph,
    scheduler queue, retry counter, or completion flag. Hermes Kanban remains the
    sole lifecycle authority.
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(str(self.path), timeout=30.0)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("PRAGMA busy_timeout=30000")
        return con

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            con = self._connect()
            try:
                con.execute("BEGIN IMMEDIATE")
                yield con
                con.commit()
            except Exception:
                con.rollback()
                raise
            finally:
                con.close()

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        con = self._connect()
        try:
            yield con
        finally:
            con.close()

    def _init_db(self) -> None:
        with self.transaction() as con:
            con.executescript(
                """
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS contracts (
                    task_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    contract_hash TEXT NOT NULL UNIQUE,
                    goal TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    reserve_tokens INTEGER NOT NULL,
                    checkpoint_json TEXT NOT NULL,
                    locked_at TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    supersedes_hash TEXT,
                    receipt_id TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY(task_id, version)
                );
                CREATE INDEX IF NOT EXISTS idx_contracts_task_hash ON contracts(task_id, contract_hash);
                CREATE TABLE IF NOT EXISTS active_contracts (
                    task_id TEXT PRIMARY KEY,
                    contract_hash TEXT NOT NULL REFERENCES contracts(contract_hash)
                );
                CREATE TABLE IF NOT EXISTS run_bindings (
                    task_id TEXT PRIMARY KEY,
                    run_id INTEGER NOT NULL,
                    contract_hash TEXT NOT NULL,
                    claim_identity TEXT NOT NULL,
                    worker_id TEXT,
                    bound_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_run_bindings_run ON run_bindings(task_id, run_id);
                CREATE TABLE IF NOT EXISTS work_events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    task_id TEXT NOT NULL,
                    run_id INTEGER NOT NULL,
                    contract_hash TEXT NOT NULL,
                    claim_identity TEXT NOT NULL,
                    worker_id TEXT,
                    event_type TEXT NOT NULL,
                    criterion_id TEXT,
                    source TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    stale INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_events_task_run ON work_events(task_id, run_id, seq);
                CREATE TABLE IF NOT EXISTS evidence (
                    evidence_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    run_id INTEGER NOT NULL,
                    contract_hash TEXT NOT NULL,
                    criterion_id TEXT,
                    source TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    preview TEXT NOT NULL,
                    pointer TEXT NOT NULL DEFAULT '',
                    tool_name TEXT NOT NULL DEFAULT '',
                    is_error INTEGER NOT NULL DEFAULT 0,
                    stale INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_evidence_task_run ON evidence(task_id, run_id, criterion_id, created_at);
                CREATE TABLE IF NOT EXISTS criterion_verdicts (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    run_id INTEGER NOT NULL,
                    contract_hash TEXT NOT NULL,
                    criterion_id TEXT NOT NULL,
                    state TEXT NOT NULL,
                    reason TEXT NOT NULL DEFAULT '',
                    receipt_id TEXT NOT NULL DEFAULT '',
                    evidence_ids_json TEXT NOT NULL,
                    stale INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_verdict_task_run ON criterion_verdicts(task_id, run_id, criterion_id, seq);
                CREATE TABLE IF NOT EXISTS budget_usage (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    run_id INTEGER NOT NULL,
                    contract_hash TEXT NOT NULL,
                    consumed_tokens INTEGER NOT NULL,
                    source TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_budget_task_run ON budget_usage(task_id, run_id, seq);
                CREATE TABLE IF NOT EXISTS budget_checkpoints (
                    task_id TEXT NOT NULL,
                    run_id INTEGER NOT NULL,
                    contract_hash TEXT NOT NULL,
                    fraction REAL NOT NULL,
                    crossed_at TEXT NOT NULL,
                    PRIMARY KEY(task_id, run_id, contract_hash, fraction)
                );
                CREATE TABLE IF NOT EXISTS trajectory_assessments (
                    decision_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    run_id INTEGER NOT NULL,
                    contract_hash TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    outcome_json TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_trajectory_task_run ON trajectory_assessments(task_id, run_id, created_at);
                CREATE TABLE IF NOT EXISTS checkpoints (
                    checkpoint_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    run_id INTEGER NOT NULL,
                    contract_hash TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    complete INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_checkpoints_task_run ON checkpoints(task_id, run_id, created_at);
                CREATE TABLE IF NOT EXISTS run_controls (
                    task_id TEXT NOT NULL,
                    run_id INTEGER NOT NULL,
                    contract_hash TEXT NOT NULL,
                    control TEXT NOT NULL,
                    decision_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    acknowledged INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(task_id, run_id, contract_hash)
                );
                CREATE TABLE IF NOT EXISTS run_context (
                    task_id TEXT NOT NULL,
                    run_id INTEGER NOT NULL,
                    contract_hash TEXT NOT NULL,
                    workspace_path TEXT NOT NULL DEFAULT '',
                    base_revision TEXT NOT NULL DEFAULT '',
                    session_id TEXT NOT NULL DEFAULT '',
                    bound_at TEXT NOT NULL,
                    PRIMARY KEY(task_id, run_id, contract_hash)
                );
                CREATE TABLE IF NOT EXISTS api_usage (
                    api_request_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    run_id INTEGER NOT NULL,
                    contract_hash TEXT NOT NULL,
                    input_tokens INTEGER NOT NULL DEFAULT 0,
                    output_tokens INTEGER NOT NULL DEFAULT 0,
                    reasoning_tokens INTEGER NOT NULL DEFAULT 0,
                    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
                    accounted_tokens INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_api_usage_task_run ON api_usage(task_id, run_id, created_at);
                CREATE TABLE IF NOT EXISTS supervisor_usage (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    run_id INTEGER NOT NULL,
                    contract_hash TEXT NOT NULL,
                    purpose TEXT NOT NULL,
                    input_tokens INTEGER NOT NULL DEFAULT 0,
                    output_tokens INTEGER NOT NULL DEFAULT 0,
                    reasoning_tokens INTEGER NOT NULL DEFAULT 0,
                    total_tokens INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_supervisor_usage_task_run ON supervisor_usage(task_id, run_id, seq);
                CREATE TABLE IF NOT EXISTS run_directives (
                    task_id TEXT NOT NULL,
                    run_id INTEGER NOT NULL,
                    contract_hash TEXT NOT NULL,
                    decision_id TEXT NOT NULL,
                    directive TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    consumed INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(task_id, run_id, contract_hash, decision_id)
                );
                CREATE INDEX IF NOT EXISTS idx_directives_pending ON run_directives(task_id, run_id, contract_hash, consumed, created_at);
                CREATE TABLE IF NOT EXISTS supervision_diagnostics (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL DEFAULT '',
                    run_id INTEGER,
                    kind TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )
            con.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )

    def raw_table_names(self) -> set[str]:
        with self.read() as con:
            return {str(r[0]) for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}

    def next_contract_version(self, task_id: str) -> int:
        with self.read() as con:
            row = con.execute("SELECT COALESCE(MAX(version), 0) + 1 FROM contracts WHERE task_id=?", (task_id,)).fetchone()
            return int(row[0])

    def save_contract(self, contract: DoDContract, payload: dict[str, Any]) -> None:
        with self.transaction() as con:
            existing = con.execute("SELECT payload_json FROM contracts WHERE contract_hash=?", (contract.contract_hash,)).fetchone()
            encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
            if existing:
                if str(existing[0]) != encoded:
                    raise ValueError("locked contract hash already exists with different payload")
                return
            con.execute(
                """INSERT INTO contracts(
                    task_id,version,contract_hash,goal,payload_json,reserve_tokens,checkpoint_json,
                    locked_at,actor,supersedes_hash,receipt_id
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    contract.task_id, contract.version, contract.contract_hash, contract.goal, encoded,
                    contract.reserve_tokens, json.dumps(list(contract.checkpoint_fractions)), contract.locked_at,
                    contract.actor, contract.supersedes_hash, contract.receipt_id,
                ),
            )
            con.execute(
                "INSERT INTO active_contracts(task_id,contract_hash) VALUES(?,?) "
                "ON CONFLICT(task_id) DO UPDATE SET contract_hash=excluded.contract_hash",
                (contract.task_id, contract.contract_hash),
            )

    def contract_row(self, contract_hash: str) -> dict[str, Any] | None:
        with self.read() as con:
            row = con.execute("SELECT * FROM contracts WHERE contract_hash=?", (contract_hash,)).fetchone()
            return dict(row) if row else None

    def active_contract_row(self, task_id: str) -> dict[str, Any] | None:
        with self.read() as con:
            row = con.execute(
                "SELECT c.* FROM active_contracts a JOIN contracts c ON c.contract_hash=a.contract_hash WHERE a.task_id=?",
                (task_id,),
            ).fetchone()
            return dict(row) if row else None

    def bind_run(self, identity: RunIdentity, *, bound_at: str) -> None:
        active = self.active_contract_row(identity.task_id)
        if not active:
            raise ValueError(f"task {identity.task_id!r} has no active supervision contract")
        if str(active["contract_hash"]) != identity.contract_hash:
            raise ValueError("run contract hash does not match active locked contract")
        with self.transaction() as con:
            con.execute(
                """INSERT INTO run_bindings(task_id,run_id,contract_hash,claim_identity,worker_id,bound_at)
                VALUES(?,?,?,?,?,?)
                ON CONFLICT(task_id) DO UPDATE SET
                    run_id=excluded.run_id, contract_hash=excluded.contract_hash,
                    claim_identity=excluded.claim_identity, worker_id=excluded.worker_id, bound_at=excluded.bound_at""",
                (identity.task_id, identity.run_id, identity.contract_hash, identity.claim_identity, identity.worker_id, bound_at),
            )

    def current_identity(self, task_id: str) -> RunIdentity | None:
        with self.read() as con:
            row = con.execute("SELECT * FROM run_bindings WHERE task_id=?", (task_id,)).fetchone()
            if not row:
                return None
            return RunIdentity(
                task_id=str(row["task_id"]), run_id=int(row["run_id"]), contract_hash=str(row["contract_hash"]),
                claim_identity=str(row["claim_identity"]), worker_id=row["worker_id"],
            )

    def is_current(self, identity: RunIdentity) -> bool:
        current = self.current_identity(identity.task_id)
        return current == identity

    def append_event(self, identity: RunIdentity, event: WorkEvent) -> bool:
        stale = not self.is_current(identity)
        with self.transaction() as con:
            con.execute(
                """INSERT OR IGNORE INTO work_events(
                    event_id,task_id,run_id,contract_hash,claim_identity,worker_id,event_type,
                    criterion_id,source,payload_json,stale,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    event.event_id, identity.task_id, identity.run_id, identity.contract_hash,
                    identity.claim_identity, identity.worker_id, event.event_type, event.criterion_id,
                    event.source, json.dumps(event.payload, sort_keys=True, ensure_ascii=False, default=str),
                    1 if stale else 0, event.created_at,
                ),
            )
        return stale

    def events(self, task_id: str, *, run_id: int | None = None, include_stale: bool = True) -> list[dict[str, Any]]:
        sql = "SELECT * FROM work_events WHERE task_id=?"
        params: list[Any] = [task_id]
        if run_id is not None:
            sql += " AND run_id=?"
            params.append(run_id)
        if not include_stale:
            sql += " AND stale=0"
        sql += " ORDER BY seq"
        with self.read() as con:
            rows = [dict(r) for r in con.execute(sql, params)]
        for row in rows:
            try:
                row["payload"] = json.loads(str(row.pop("payload_json")))
            except Exception:
                row["payload"] = {}
        return rows

    def append_evidence(self, identity: RunIdentity, record: EvidenceRecord) -> bool:
        stale = not self.is_current(identity)
        with self.transaction() as con:
            con.execute(
                """INSERT OR IGNORE INTO evidence(
                    evidence_id,task_id,run_id,contract_hash,criterion_id,source,kind,sha256,preview,
                    pointer,tool_name,is_error,stale,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    record.evidence_id, identity.task_id, identity.run_id, identity.contract_hash,
                    record.criterion_id, record.source, record.kind, record.sha256, record.preview,
                    record.pointer, record.tool_name, 1 if record.is_error else 0, 1 if stale else 0,
                    record.created_at,
                ),
            )
        return stale

    def evidence(self, identity: RunIdentity, *, criterion_id: str | None = None, include_stale: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM evidence WHERE task_id=? AND run_id=? AND contract_hash=?"
        params: list[Any] = [identity.task_id, identity.run_id, identity.contract_hash]
        if criterion_id is not None:
            sql += " AND criterion_id=?"
            params.append(criterion_id)
        if not include_stale:
            sql += " AND stale=0"
        sql += " ORDER BY created_at,evidence_id"
        with self.read() as con:
            return [dict(r) for r in con.execute(sql, params)]

    def save_verdict(
        self, identity: RunIdentity, *, criterion_id: str, state: str, reason: str,
        receipt_id: str, evidence_ids: list[str], created_at: str,
    ) -> bool:
        stale = not self.is_current(identity)
        with self.transaction() as con:
            con.execute(
                """INSERT INTO criterion_verdicts(
                    task_id,run_id,contract_hash,criterion_id,state,reason,receipt_id,evidence_ids_json,stale,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    identity.task_id, identity.run_id, identity.contract_hash, criterion_id, state, reason,
                    receipt_id, json.dumps(evidence_ids), 1 if stale else 0, created_at,
                ),
            )
        return stale

    def latest_verdicts(self, identity: RunIdentity) -> dict[str, dict[str, Any]]:
        with self.read() as con:
            rows = con.execute(
                """SELECT v.* FROM criterion_verdicts v
                JOIN (
                    SELECT criterion_id, MAX(seq) AS max_seq FROM criterion_verdicts
                    WHERE task_id=? AND run_id=? AND contract_hash=? AND stale=0
                    GROUP BY criterion_id
                ) x ON x.max_seq=v.seq""",
                (identity.task_id, identity.run_id, identity.contract_hash),
            ).fetchall()
        return {str(r["criterion_id"]): dict(r) for r in rows}

    def latest_contract_verdicts(self, task_id: str, contract_hash: str) -> dict[str, dict[str, Any]]:
        """Latest authoritative criterion verdicts across successor runs of one locked DoD.

        A verified fact survives REPLAN/retry while the contract hash is unchanged.
        A later run can supersede it with a newer verdict. Stale-run verdict writes are
        already prevented by CardSupervisor and ignored here defensively.
        """
        with self.read() as con:
            rows = con.execute(
                """SELECT v.* FROM criterion_verdicts v
                JOIN (
                    SELECT criterion_id, MAX(seq) AS max_seq FROM criterion_verdicts
                    WHERE task_id=? AND contract_hash=? AND stale=0
                    GROUP BY criterion_id
                ) x ON x.max_seq=v.seq""",
                (task_id, contract_hash),
            ).fetchall()
        return {str(r["criterion_id"]): dict(r) for r in rows}

    def record_usage(self, identity: RunIdentity, *, consumed_tokens: int, source: str, created_at: str) -> bool:
        stale = not self.is_current(identity)
        if stale:
            return True
        with self.transaction() as con:
            con.execute(
                "INSERT INTO budget_usage(task_id,run_id,contract_hash,consumed_tokens,source,created_at) VALUES(?,?,?,?,?,?)",
                (identity.task_id, identity.run_id, identity.contract_hash, max(0, int(consumed_tokens)), source, created_at),
            )
        return False

    def latest_usage(self, identity: RunIdentity) -> tuple[int, str]:
        with self.read() as con:
            row = con.execute(
                """SELECT consumed_tokens,source FROM budget_usage
                WHERE task_id=? AND run_id=? AND contract_hash=? ORDER BY seq DESC LIMIT 1""",
                (identity.task_id, identity.run_id, identity.contract_hash),
            ).fetchone()
            return (int(row[0]), str(row[1])) if row else (0, "none")

    def crossed_checkpoints(self, identity: RunIdentity) -> set[float]:
        with self.read() as con:
            return {
                float(r[0]) for r in con.execute(
                    "SELECT fraction FROM budget_checkpoints WHERE task_id=? AND run_id=? AND contract_hash=?",
                    (identity.task_id, identity.run_id, identity.contract_hash),
                )
            }

    def mark_checkpoint_crossed(self, identity: RunIdentity, fraction: float, *, crossed_at: str) -> bool:
        with self.transaction() as con:
            cur = con.execute(
                """INSERT OR IGNORE INTO budget_checkpoints(task_id,run_id,contract_hash,fraction,crossed_at)
                VALUES(?,?,?,?,?)""",
                (identity.task_id, identity.run_id, identity.contract_hash, float(fraction), crossed_at),
            )
            return cur.rowcount > 0

    def save_trajectory(self, identity: RunIdentity, assessment: TrajectoryAssessment) -> None:
        with self.transaction() as con:
            con.execute(
                """INSERT OR REPLACE INTO trajectory_assessments(
                    decision_id,task_id,run_id,contract_hash,payload_json,outcome_json,created_at
                ) VALUES(?,?,?,?,?,COALESCE((SELECT outcome_json FROM trajectory_assessments WHERE decision_id=?),NULL),?)""",
                (
                    assessment.decision_id, identity.task_id, identity.run_id, identity.contract_hash,
                    json.dumps(assessment.as_dict(), sort_keys=True, ensure_ascii=False, default=str),
                    assessment.decision_id, assessment.created_at,
                ),
            )

    def label_trajectory(self, decision_id: str, outcome: dict[str, Any]) -> None:
        with self.transaction() as con:
            con.execute(
                "UPDATE trajectory_assessments SET outcome_json=? WHERE decision_id=?",
                (json.dumps(outcome, sort_keys=True, ensure_ascii=False, default=str), decision_id),
            )

    def trajectory_rows(self, *, task_id: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM trajectory_assessments"
        params: tuple[Any, ...] = ()
        if task_id is not None:
            sql += " WHERE task_id=?"
            params = (task_id,)
        sql += " ORDER BY created_at,decision_id"
        with self.read() as con:
            rows = [dict(r) for r in con.execute(sql, params)]
        for row in rows:
            row["payload"] = json.loads(row.pop("payload_json"))
            row["outcome"] = json.loads(row["outcome_json"]) if row.get("outcome_json") else None
        return rows

    def save_checkpoint(self, payload: dict[str, Any]) -> None:
        identity = payload["identity"]
        with self.transaction() as con:
            con.execute(
                """INSERT OR REPLACE INTO checkpoints(
                    checkpoint_id,task_id,run_id,contract_hash,payload_json,complete,created_at
                ) VALUES(?,?,?,?,?,?,?)""",
                (
                    payload["checkpoint_id"], identity["task_id"], int(identity["run_id"]), identity["contract_hash"],
                    json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str),
                    1 if payload.get("checkpoint_complete") else 0, payload.get("created_at") or "",
                ),
            )

    def latest_checkpoint(self, task_id: str) -> dict[str, Any] | None:
        with self.read() as con:
            row = con.execute("SELECT payload_json FROM checkpoints WHERE task_id=? ORDER BY created_at DESC LIMIT 1", (task_id,)).fetchone()
            return json.loads(str(row[0])) if row else None

    def set_control(self, identity: RunIdentity, *, control: str, decision_id: str, payload: dict[str, Any], created_at: str) -> None:
        if not self.is_current(identity):
            raise ValueError("cannot set control for stale run")
        with self.transaction() as con:
            con.execute(
                """INSERT INTO run_controls(task_id,run_id,contract_hash,control,decision_id,payload_json,acknowledged,created_at)
                VALUES(?,?,?,?,?,?,0,?)
                ON CONFLICT(task_id,run_id,contract_hash) DO UPDATE SET
                    control=excluded.control,decision_id=excluded.decision_id,payload_json=excluded.payload_json,
                    acknowledged=0,created_at=excluded.created_at""",
                (
                    identity.task_id, identity.run_id, identity.contract_hash, control, decision_id,
                    json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str), created_at,
                ),
            )

    def control(self, identity: RunIdentity) -> dict[str, Any] | None:
        with self.read() as con:
            row = con.execute(
                "SELECT * FROM run_controls WHERE task_id=? AND run_id=? AND contract_hash=? AND acknowledged=0",
                (identity.task_id, identity.run_id, identity.contract_hash),
            ).fetchone()
            if not row:
                return None
            data = dict(row)
            data["payload"] = json.loads(data.pop("payload_json"))
            return data

    def acknowledge_control(self, identity: RunIdentity) -> None:
        with self.transaction() as con:
            con.execute(
                "UPDATE run_controls SET acknowledged=1 WHERE task_id=? AND run_id=? AND contract_hash=?",
                (identity.task_id, identity.run_id, identity.contract_hash),
            )

    def save_run_context(
        self,
        identity: RunIdentity,
        *,
        workspace_path: str = "",
        base_revision: str = "",
        session_id: str = "",
        bound_at: str,
    ) -> None:
        if not self.is_current(identity):
            raise ValueError("cannot save run context for stale run")
        with self.transaction() as con:
            con.execute(
                """INSERT INTO run_context(task_id,run_id,contract_hash,workspace_path,base_revision,session_id,bound_at)
                VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(task_id,run_id,contract_hash) DO UPDATE SET
                    workspace_path=excluded.workspace_path,
                    base_revision=CASE WHEN excluded.base_revision<>'' THEN excluded.base_revision ELSE run_context.base_revision END,
                    session_id=CASE WHEN excluded.session_id<>'' THEN excluded.session_id ELSE run_context.session_id END,
                    bound_at=excluded.bound_at""",
                (
                    identity.task_id, identity.run_id, identity.contract_hash,
                    str(workspace_path or ""), str(base_revision or ""), str(session_id or ""), str(bound_at),
                ),
            )

    def run_context(self, identity: RunIdentity) -> dict[str, Any]:
        with self.read() as con:
            row = con.execute(
                "SELECT * FROM run_context WHERE task_id=? AND run_id=? AND contract_hash=?",
                (identity.task_id, identity.run_id, identity.contract_hash),
            ).fetchone()
            return dict(row) if row else {}

    def record_api_usage(
        self,
        identity: RunIdentity,
        *,
        api_request_id: str,
        input_tokens: int,
        output_tokens: int,
        reasoning_tokens: int,
        cache_read_tokens: int,
        accounted_tokens: int,
        created_at: str,
    ) -> tuple[bool, int]:
        if not self.is_current(identity):
            return False, self.api_usage_total(identity)
        request_id = str(api_request_id or "").strip()
        if not request_id:
            return False, self.api_usage_total(identity)
        with self.transaction() as con:
            cur = con.execute(
                """INSERT OR IGNORE INTO api_usage(
                    api_request_id,task_id,run_id,contract_hash,input_tokens,output_tokens,
                    reasoning_tokens,cache_read_tokens,accounted_tokens,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    request_id, identity.task_id, identity.run_id, identity.contract_hash,
                    max(0, int(input_tokens)), max(0, int(output_tokens)),
                    max(0, int(reasoning_tokens)), max(0, int(cache_read_tokens)),
                    max(0, int(accounted_tokens)), str(created_at),
                ),
            )
            row = con.execute(
                "SELECT COALESCE(SUM(accounted_tokens),0) FROM api_usage WHERE task_id=? AND run_id=? AND contract_hash=?",
                (identity.task_id, identity.run_id, identity.contract_hash),
            ).fetchone()
            return cur.rowcount > 0, int(row[0] if row else 0)

    def api_usage_total(self, identity: RunIdentity) -> int:
        with self.read() as con:
            row = con.execute(
                "SELECT COALESCE(SUM(accounted_tokens),0) FROM api_usage WHERE task_id=? AND run_id=? AND contract_hash=?",
                (identity.task_id, identity.run_id, identity.contract_hash),
            ).fetchone()
            return int(row[0] if row else 0)

    def api_usage_rows(self, identity: RunIdentity) -> list[dict[str, Any]]:
        with self.read() as con:
            return [dict(r) for r in con.execute(
                "SELECT * FROM api_usage WHERE task_id=? AND run_id=? AND contract_hash=? ORDER BY created_at,api_request_id",
                (identity.task_id, identity.run_id, identity.contract_hash),
            )]

    def record_supervisor_usage(
        self,
        identity: RunIdentity,
        *,
        purpose: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
        reasoning_tokens: int = 0,
        total_tokens: int = 0,
        created_at: str,
    ) -> None:
        if not self.is_current(identity):
            return
        derived = max(
            max(0, int(total_tokens)),
            max(0, int(input_tokens)) + max(0, int(output_tokens)) + max(0, int(reasoning_tokens)),
        )
        with self.transaction() as con:
            con.execute(
                """INSERT INTO supervisor_usage(
                    task_id,run_id,contract_hash,purpose,input_tokens,output_tokens,reasoning_tokens,total_tokens,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    identity.task_id, identity.run_id, identity.contract_hash, str(purpose or "jev"),
                    max(0, int(input_tokens)), max(0, int(output_tokens)), max(0, int(reasoning_tokens)), derived,
                    str(created_at),
                ),
            )

    def supervisor_usage_total(self, identity: RunIdentity) -> int:
        with self.read() as con:
            row = con.execute(
                "SELECT COALESCE(SUM(total_tokens),0) FROM supervisor_usage WHERE task_id=? AND run_id=? AND contract_hash=?",
                (identity.task_id, identity.run_id, identity.contract_hash),
            ).fetchone()
            return int(row[0] if row else 0)

    def stage_directive(
        self,
        identity: RunIdentity,
        *,
        decision_id: str,
        directive: str,
        confidence: float,
        created_at: str,
    ) -> None:
        if not self.is_current(identity):
            return
        text = str(directive or "").strip()
        if not text:
            return
        with self.transaction() as con:
            con.execute(
                """INSERT OR REPLACE INTO run_directives(
                    task_id,run_id,contract_hash,decision_id,directive,confidence,consumed,created_at
                ) VALUES(?,?,?,?,?,?,0,?)""",
                (
                    identity.task_id, identity.run_id, identity.contract_hash,
                    str(decision_id), text, float(confidence), str(created_at),
                ),
            )

    def consume_directive(self, identity: RunIdentity) -> dict[str, Any] | None:
        if not self.is_current(identity):
            return None
        with self.transaction() as con:
            row = con.execute(
                """SELECT rowid,* FROM run_directives
                WHERE task_id=? AND run_id=? AND contract_hash=? AND consumed=0
                ORDER BY created_at,decision_id LIMIT 1""",
                (identity.task_id, identity.run_id, identity.contract_hash),
            ).fetchone()
            if not row:
                return None
            con.execute("UPDATE run_directives SET consumed=1 WHERE rowid=?", (int(row["rowid"]),))
            return dict(row)

    def latest_diagnostic(self, *, task_id: str, run_id: int | None = None, kind: str = "") -> dict[str, Any] | None:
        sql = "SELECT rowid,* FROM supervision_diagnostics WHERE task_id=?"
        params: list[Any] = [str(task_id or "")]
        if run_id is not None:
            sql += " AND run_id=?"
            params.append(int(run_id))
        if kind:
            sql += " AND kind=?"
            params.append(str(kind))
        sql += " ORDER BY rowid DESC LIMIT 1"
        with self.read() as con:
            row = con.execute(sql, tuple(params)).fetchone()
            if not row:
                return None
            data = dict(row)
            try:
                data["payload"] = json.loads(str(data.get("payload_json") or "{}"))
            except Exception:
                data["payload"] = {}
            return data

    def add_diagnostic(self, *, task_id: str = "", run_id: int | None = None, kind: str, payload: dict[str, Any], created_at: str) -> None:
        with self.transaction() as con:
            con.execute(
                "INSERT INTO supervision_diagnostics(task_id,run_id,kind,payload_json,created_at) VALUES(?,?,?,?,?)",
                (str(task_id or ""), int(run_id) if run_id is not None else None, str(kind), json.dumps(payload, sort_keys=True, default=str), str(created_at)),
            )
