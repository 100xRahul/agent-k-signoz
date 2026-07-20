"""SQLite store for investigations and actions — WAL mode."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from config import settings


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    """SQLite-backed persistence for Agent K state."""

    def __init__(self, db_path: str | None = None) -> None:
        self._db_path = db_path or settings.db_path
        self._conn: sqlite3.Connection | None = None

    # ── Connection ────────────────────────────────────────────────

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.row_factory = sqlite3.Row
            self._create_tables()
        return self._conn

    def _create_tables(self) -> None:
        conn = self._conn
        assert conn is not None
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS investigations (
                id TEXT PRIMARY KEY,
                trigger_json TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'running',
                started_at TEXT NOT NULL,
                finished_at TEXT,
                report_md TEXT,
                root_cause TEXT,
                cost_usd REAL DEFAULT 0.0,
                tokens_in INTEGER DEFAULT 0,
                tokens_out INTEGER DEFAULT 0,
                trace_id TEXT
            );
            CREATE TABLE IF NOT EXISTS actions (
                id TEXT PRIMARY KEY,
                investigation_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                params_json TEXT NOT NULL DEFAULT '{}',
                status TEXT NOT NULL DEFAULT 'proposed',
                created_at TEXT NOT NULL,
                executed_at TEXT,
                verification_md TEXT,
                FOREIGN KEY (investigation_id) REFERENCES investigations(id)
            );
        """)
        conn.commit()

    # ── Investigations ────────────────────────────────────────────

    def create_investigation(
        self,
        trigger_json: str,
        trace_id: str = "",
    ) -> str:
        """Create a new investigation record. Returns the investigation ID."""
        inv_id = uuid4().hex[:12]
        conn = self._get_conn()
        conn.execute(
            """INSERT INTO investigations
               (id, trigger_json, status, started_at, trace_id)
               VALUES (?, ?, 'running', ?, ?)""",
            (inv_id, trigger_json, _now_iso(), trace_id),
        )
        conn.commit()
        return inv_id

    def update_investigation(self, inv_id: str, **kwargs: Any) -> None:
        """Update investigation fields. Only updates provided fields."""
        if not kwargs:
            return
        allowed = {
            "status", "finished_at", "report_md", "root_cause",
            "cost_usd", "tokens_in", "tokens_out", "trace_id",
        }
        fields = {k: v for k, v in kwargs.items() if k in allowed}
        if not fields:
            return
        set_clause = ", ".join(f"{k} = ?" for k in fields)
        values = list(fields.values()) + [inv_id]
        conn = self._get_conn()
        conn.execute(
            f"UPDATE investigations SET {set_clause} WHERE id = ?",
            values,
        )
        conn.commit()

    def get_investigation(self, inv_id: str) -> dict[str, Any] | None:
        """Get a single investigation by ID."""
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM investigations WHERE id = ?", (inv_id,)
        ).fetchone()
        return dict(row) if row else None

    def list_investigations(self, limit: int = 50) -> list[dict[str, Any]]:
        """List recent investigations, newest first."""
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM investigations ORDER BY started_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def has_running_investigation(self, alertname: str) -> bool:
        """Check if there's already a running investigation for this alert."""
        conn = self._get_conn()
        rows = conn.execute(
            """SELECT id FROM investigations
               WHERE status = 'running'
               AND trigger_json LIKE ?""",
            (f'%"alertname":"{alertname}"%',),
        ).fetchall()
        return len(rows) > 0

    # ── Actions ───────────────────────────────────────────────────

    def create_action(
        self,
        investigation_id: str,
        kind: str,
        params_json: str,
    ) -> str:
        """Create a new action record. Returns the action ID."""
        action_id = uuid4().hex[:12]
        conn = self._get_conn()
        conn.execute(
            """INSERT INTO actions
               (id, investigation_id, kind, params_json, status, created_at)
               VALUES (?, ?, ?, ?, 'proposed', ?)""",
            (action_id, investigation_id, kind, params_json, _now_iso()),
        )
        conn.commit()
        return action_id

    def update_action(self, action_id: str, **kwargs: Any) -> None:
        """Update action fields."""
        if not kwargs:
            return
        allowed = {"status", "executed_at", "verification_md"}
        fields = {k: v for k, v in kwargs.items() if k in allowed}
        if not fields:
            return
        set_clause = ", ".join(f"{k} = ?" for k in fields)
        values = list(fields.values()) + [action_id]
        conn = self._get_conn()
        conn.execute(
            f"UPDATE actions SET {set_clause} WHERE id = ?",
            values,
        )
        conn.commit()

    def get_action(self, action_id: str) -> dict[str, Any] | None:
        """Get a single action by ID."""
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM actions WHERE id = ?", (action_id,)
        ).fetchone()
        return dict(row) if row else None

    def get_actions_for_investigation(
        self, investigation_id: str
    ) -> list[dict[str, Any]]:
        """Get all actions for an investigation."""
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM actions WHERE investigation_id = ? ORDER BY created_at",
            (investigation_id,),
        ).fetchall()
        return [dict(r) for r in rows]


# Singleton
store = Store()
