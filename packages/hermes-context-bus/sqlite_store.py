from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

AGENT_IDS = (
    "hermes-whatsapp",
    "hermes-discord",
    "hermes-cli",
    "hermes-desktop",
)


class SQLiteBlackboard:
    """Small transparent multi-process forum used when MegaMCP bb.* is unavailable."""

    def __init__(self, db_path: str | Path, projection_path: str | Path):
        self.db_path = Path(db_path).expanduser()
        self.projection_path = Path(projection_path).expanduser()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.projection_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS context_entries (
                    entry_id TEXT PRIMARY KEY,
                    board TEXT NOT NULL,
                    title TEXT NOT NULL,
                    body TEXT NOT NULL,
                    author TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    tags_json TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_context_entries_board_created
                    ON context_entries(board, created_at, entry_id);

                CREATE TABLE IF NOT EXISTS context_messages (
                    message_id TEXT PRIMARY KEY,
                    thread_id TEXT NOT NULL,
                    from_id TEXT NOT NULL,
                    to_id TEXT NOT NULL,
                    body TEXT NOT NULL,
                    priority TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    acked_at REAL
                );
                CREATE INDEX IF NOT EXISTS idx_context_messages_to_created
                    ON context_messages(to_id, created_at, message_id);
                CREATE INDEX IF NOT EXISTS idx_context_messages_thread_created
                    ON context_messages(thread_id, created_at, message_id);

                CREATE TRIGGER IF NOT EXISTS context_entries_no_update
                BEFORE UPDATE ON context_entries
                BEGIN SELECT RAISE(ABORT, 'context entries are immutable'); END;

                CREATE TRIGGER IF NOT EXISTS context_entries_no_delete
                BEFORE DELETE ON context_entries
                BEGIN SELECT RAISE(ABORT, 'context entries are immutable'); END;
                """
            )

    @staticmethod
    def _entry(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "entry_id": row["entry_id"],
            "board": row["board"],
            "title": row["title"],
            "body": row["body"],
            "author": row["author"],
            "kind": row["kind"],
            "tags": json.loads(row["tags_json"]),
            "created_at": row["created_at"],
        }

    @staticmethod
    def _message(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "message_id": row["message_id"],
            "thread_id": row["thread_id"],
            "from_id": row["from_id"],
            "to_id": row["to_id"],
            "body": row["body"],
            "priority": row["priority"],
            "created_at": row["created_at"],
            "acked_at": row["acked_at"],
        }

    def board(self, board: str, limit: int = 100) -> dict[str, Any]:
        limit = max(1, min(int(limit), 500))
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM context_entries
                WHERE board=?
                ORDER BY created_at DESC, entry_id DESC
                LIMIT ?
                """,
                (board, limit),
            ).fetchall()
        entries = [self._entry(row) for row in reversed(rows)]
        return {
            "board": board,
            "kind": "freeform",
            "count": len(entries),
            "entries": entries,
            "backend": "sqlite",
        }

    def inbox(
        self, recipient: str, unread_only: bool = True, limit: int = 100
    ) -> dict[str, Any]:
        limit = max(1, min(int(limit), 500))
        where = "to_id=? AND acked_at IS NULL" if unread_only else "to_id=?"
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM context_messages
                WHERE {where}
                ORDER BY created_at DESC, message_id DESC
                LIMIT ?
                """,
                (recipient, limit),
            ).fetchall()
            unread = conn.execute(
                "SELECT COUNT(*) FROM context_messages WHERE to_id=? AND acked_at IS NULL",
                (recipient,),
            ).fetchone()[0]
        messages = [self._message(row) for row in reversed(rows)]
        return {
            "recipient": recipient,
            "count": len(messages),
            "unread": int(unread),
            "messages": messages,
            "backend": "sqlite",
        }

    def post(
        self,
        *,
        board: str,
        title: str,
        body: str,
        author: str,
        kind: str = "freeform",
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        now = time.time()
        entry = {
            "entry_id": f"entry-{uuid.uuid4()}",
            "board": board,
            "title": title,
            "body": body,
            "author": author,
            "kind": kind,
            "tags": list(tags or []),
            "created_at": now,
        }
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                INSERT INTO context_entries
                    (entry_id, board, title, body, author, kind, tags_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    entry["entry_id"],
                    board,
                    title,
                    body,
                    author,
                    kind,
                    json.dumps(entry["tags"], ensure_ascii=False),
                    now,
                ),
            )
            conn.commit()
        self.write_projection()
        return {**entry, "backend": "sqlite"}

    def send(
        self,
        *,
        to: str,
        body: str,
        from_id: str,
        priority: str = "normal",
        thread_id: str = "",
    ) -> dict[str, Any]:
        if to not in AGENT_IDS:
            raise ValueError(f"unknown shared-context recipient: {to}")
        now = time.time()
        message_id = f"msg-{uuid.uuid4()}"
        thread_id = thread_id or f"thread-{uuid.uuid4()}"
        message = {
            "message_id": message_id,
            "thread_id": thread_id,
            "from_id": from_id,
            "to_id": to,
            "body": body,
            "priority": priority,
            "created_at": now,
            "acked_at": None,
        }
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                INSERT INTO context_messages
                    (message_id, thread_id, from_id, to_id, body, priority, created_at, acked_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, NULL)
                """,
                (message_id, thread_id, from_id, to, body, priority, now),
            )
            conn.commit()
        self.write_projection()
        return {
            "message_id": message_id,
            "thread_id": thread_id,
            "from_id": from_id,
            "to": to,
            "priority": priority,
            "delivered_to": [to],
            "deliveries": [message],
            "backend": "sqlite",
        }

    def ack(self, recipient: str, message_id: str) -> dict[str, Any]:
        now = time.time()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM context_messages WHERE message_id=?",
                (message_id,),
            ).fetchone()
            if row is None:
                conn.rollback()
                raise KeyError(f"unknown message: {message_id}")
            if row["to_id"] != recipient:
                conn.rollback()
                raise ValueError("only the owning recipient may acknowledge a message")
            already = row["acked_at"] is not None
            if not already:
                conn.execute(
                    "UPDATE context_messages SET acked_at=? WHERE message_id=?",
                    (now, message_id),
                )
            conn.commit()
        self.write_projection()
        return {
            "message_id": message_id,
            "recipient": recipient,
            "already_acked": already,
            "acked_at": row["acked_at"] if already else now,
            "backend": "sqlite",
        }

    def thread(
        self, thread_id: str, recipient: str = "", limit: int = 200
    ) -> dict[str, Any]:
        limit = max(1, min(int(limit), 500))
        sql = "SELECT * FROM context_messages WHERE thread_id=?"
        params: list[Any] = [thread_id]
        if recipient:
            sql += " AND (to_id=? OR from_id=?)"
            params.extend([recipient, recipient])
        sql += " ORDER BY created_at ASC, message_id ASC LIMIT ?"
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return {
            "thread_id": thread_id,
            "count": len(rows),
            "messages": [self._message(row) for row in rows],
            "backend": "sqlite",
        }

    def write_projection(self) -> Path:
        board = self.board("hermes-shared-context", limit=100)
        inboxes = {
            agent_id: self.inbox(agent_id, unread_only=False, limit=50)
            for agent_id in AGENT_IDS
        }
        lines = [
            "# Hermes Shared Context",
            "",
            "Derived projection of the local shared-context SQLite forum.",
            f"Database: {self.db_path}",
            "This file is not the concurrency primitive; the SQLite database is authoritative.",
            "",
            "## Shared board",
            "",
        ]
        entries = board["entries"]
        if not entries:
            lines.extend(["_No shared entries._", ""])
        else:
            for entry in entries:
                lines.extend(
                    [
                        f"### {entry['title']}",
                        f"Author: {entry['author']}",
                        "",
                        entry["body"],
                        "",
                    ]
                )

        lines.extend(["## Agent inboxes", ""])
        for agent_id in AGENT_IDS:
            lines.extend([f"### {agent_id}", ""])
            messages = inboxes[agent_id]["messages"]
            if not messages:
                lines.extend(["_No messages._", ""])
                continue
            for message in messages:
                status = "acked" if message["acked_at"] is not None else "unread"
                lines.append(
                    f"- [{status}] from {message['from_id']} "
                    f"(thread {message['thread_id']}): {message['body']}"
                )
            lines.append("")

        rendered = "\n".join(lines).rstrip() + "\n"
        tmp = self.projection_path.with_name(
            self.projection_path.name + f".{uuid.uuid4().hex}.tmp"
        )
        try:
            tmp.write_text(rendered, encoding="utf-8")
            tmp.replace(self.projection_path)
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass
        return self.projection_path