"""DRK PR2+PR3: evidence ledger + claim registry (SQLite, receipts-first).

Every retrieval operation produces a durable receipt row. Every claim links to
evidence by id. Nothing in the registry is reported without a stored receipt.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from hermes_nerve.research.contracts import (
    Authority, Claim, ClaimKind, ClaimStatus, Evidence, Freshness, Method,
    can_upgrade_to_supported,
)


def _uuid() -> str:
    return uuid.uuid4().hex[:12]


class Ledger:
    def __init__(self, db_path: str | Path = "nerve_drk_ledger.db"):
        self._lock = threading.Lock()
        self.db = sqlite3.connect(str(db_path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY, question TEXT, created_at REAL
            );
            CREATE TABLE IF NOT EXISTS evidence (
                evidence_id TEXT PRIMARY KEY, run_id TEXT, source_url TEXT,
                source_name TEXT, retrieved_at TEXT, published_at TEXT,
                effective_at TEXT, method TEXT, authority TEXT,
                content_hash TEXT, excerpt TEXT, normalized_value TEXT
            );
            CREATE TABLE IF NOT EXISTS claims (
                claim_id TEXT PRIMARY KEY, run_id TEXT, text TEXT, kind TEXT,
                freshness TEXT, status TEXT, blocking INTEGER,
                candidate TEXT, evidence_ids TEXT, contradiction_ids TEXT
            );
            CREATE TABLE IF NOT EXISTS referee_decisions (
                decision_id TEXT PRIMARY KEY, run_id TEXT, candidate TEXT,
                passed INTEGER, reasons TEXT, decided_at TEXT
            );
            """
        )
        self.db.commit()

    # ---- runs ----
    def start_run(self, question: str) -> str:
        run_id = _uuid()
        with self._lock:
            self.db.execute(
            "INSERT INTO runs (run_id, question, created_at) VALUES (?,?,?)",
            (run_id, question, datetime.now(timezone.utc).timestamp()))
            self.db.commit()
        return run_id

    # ---- evidence receipts ----
    def store_evidence(self, run_id: str, ev: Evidence) -> str:
        eid = f"ev-{_uuid()}"
        with self._lock:
            self.db.execute(
            "INSERT INTO evidence VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (eid, run_id, ev.source_url, ev.source_name,
             ev.retrieved_at.isoformat(),
             ev.published_at.isoformat() if ev.published_at else None,
             ev.effective_at.isoformat() if ev.effective_at else None,
             ev.method.value, ev.authority.value, ev.content_hash(),
             ev.extracted_text[:2000],
             json.dumps(ev.normalized_value) if ev.normalized_value is not None else None))
            self.db.commit()
        return eid

    # ---- claims ----
    def register_claim(self, run_id: str, claim: Claim, candidate: str = "") -> str:
        cid = claim.id or f"claim-{_uuid()}"
        with self._lock:
            self.db.execute(
            "INSERT OR REPLACE INTO claims VALUES (?,?,?,?,?,?,?,?,?,?)",
            (cid, run_id, claim.text, claim.kind.value, claim.freshness.value,
             claim.status.value, int(claim.blocking), candidate,
             json.dumps(claim.evidence_ids), json.dumps(claim.contradiction_ids)))
            self.db.commit()
        return cid

    def verify_claim(self, run_id: str, claim: Claim,
                     evidence_by_id: dict[str, Evidence], now: datetime) -> str:
        """Determine status deterministically and persist it.
        Evidence receipts must be stored (store_evidence) by the caller first."""
        if claim.contradiction_ids:
            claim.status = ClaimStatus.CONTRADICTED
        elif can_upgrade_to_supported(claim, evidence_by_id, now):
            claim.status = ClaimStatus.SUPPORTED
        elif claim.evidence_ids:
            claim.status = ClaimStatus.UNRESOLVED
        else:
            claim.status = ClaimStatus.UNVERIFIED
        return self.register_claim(run_id, claim)

    def claim_status(self, claim_id: str) -> Optional[ClaimStatus]:
        with self._lock:
            row = self.db.execute("SELECT status FROM claims WHERE claim_id=?",
                                  (claim_id,)).fetchone()
        return ClaimStatus(row["status"]) if row else None

    def run_claims(self, run_id: str) -> list[dict]:
        with self._lock:
            rows = self.db.execute(
                "SELECT * FROM claims WHERE run_id=? ORDER BY rowid", (run_id,)).fetchall()
        return [dict(r) for r in rows]

    def referee_decision(self, run_id: str, candidate: str, passed: bool,
                         reasons: list[str]) -> str:
        did = f"ref-{_uuid()}"
        with self._lock:
            self.db.execute(
            "INSERT INTO referee_decisions VALUES (?,?,?,?,?,?)",
            (did, run_id, candidate, int(passed), json.dumps(reasons),
             datetime.now(timezone.utc).isoformat()))
            self.db.commit()
        return did

    def close(self):
        self.db.close()
