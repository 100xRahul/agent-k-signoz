"""SQLite store for investigations and actions — WAL mode."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from config import settings

# Genesis hash for the ledger chain — the "previous hash" of the very first entry.
GENESIS_HASH = "0" * 64


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical(payload: dict[str, Any]) -> str:
    """Deterministic JSON encoding so the hash is stable across processes."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def ledger_entry_hash(prev_hash: str, payload_canonical: str) -> str:
    """Compute a ledger entry hash from the previous hash + canonical payload."""
    return hashlib.sha256((prev_hash + payload_canonical).encode("utf-8")).hexdigest()


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
            CREATE TABLE IF NOT EXISTS ledger (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                investigation_id TEXT NOT NULL,
                ts TEXT NOT NULL,
                entry_type TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                prev_hash TEXT NOT NULL,
                entry_hash TEXT NOT NULL
            );
        """)
        # Migration: full LLM/tool transcript + auditor verdict per investigation.
        cols = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(investigations)").fetchall()
        }
        if "transcript_json" not in cols:
            conn.execute("ALTER TABLE investigations ADD COLUMN transcript_json TEXT")
        # audit_grounded stored as INTEGER (0/1) or NULL when the audit errored.
        if "audit_grounded" not in cols:
            conn.execute("ALTER TABLE investigations ADD COLUMN audit_grounded INTEGER")
        if "audit_score" not in cols:
            conn.execute("ALTER TABLE investigations ADD COLUMN audit_score REAL")
        if "audit_json" not in cols:
            conn.execute("ALTER TABLE investigations ADD COLUMN audit_json TEXT")
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
            "status",
            "finished_at",
            "report_md",
            "root_cause",
            "cost_usd",
            "tokens_in",
            "tokens_out",
            "trace_id",
            "transcript_json",
            "audit_grounded",
            "audit_score",
            "audit_json",
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

    def latest_investigation_for_alert(self, alertname: str) -> dict[str, Any] | None:
        """Most recent investigation (any status) for an alertname — used for cooldown."""
        conn = self._get_conn()
        row = conn.execute(
            """SELECT * FROM investigations
               WHERE trigger_json LIKE ?
               ORDER BY started_at DESC LIMIT 1""",
            (f'%"alertname":"{alertname}"%',),
        ).fetchone()
        return dict(row) if row else None

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

    # ── Hash-chained audit ledger ─────────────────────────────────

    def append_ledger(
        self,
        investigation_id: str,
        entry_type: str,
        payload: dict[str, Any],
    ) -> str:
        """Append a tamper-evident entry to the hash-chained ledger.

        Each entry's hash covers the previous entry's hash plus this entry's
        canonical payload, so any later edit to a row breaks the chain from that
        point on. Single-writer via the shared connection keeps ordering and the
        prev-hash lookup consistent. Returns the new entry_hash.
        """
        conn = self._get_conn()
        row = conn.execute(
            "SELECT entry_hash FROM ledger ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        prev_hash = row["entry_hash"] if row else GENESIS_HASH
        ts = _now_iso()
        # Bind ts/type/investigation into the hashed payload so they can't be
        # altered without breaking the chain either.
        sealed = {
            "investigation_id": investigation_id,
            "ts": ts,
            "entry_type": entry_type,
            "payload": payload,
        }
        canonical = _canonical(sealed)
        entry_hash = ledger_entry_hash(prev_hash, canonical)
        conn.execute(
            """INSERT INTO ledger
               (investigation_id, ts, entry_type, payload_json, prev_hash, entry_hash)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (investigation_id, ts, entry_type, canonical, prev_hash, entry_hash),
        )
        conn.commit()
        return entry_hash

    def list_ledger(
        self, investigation_id: str | None = None
    ) -> list[dict[str, Any]]:
        """Return ledger entries in chain order (optionally for one investigation)."""
        conn = self._get_conn()
        if investigation_id:
            rows = conn.execute(
                "SELECT * FROM ledger WHERE investigation_id = ? ORDER BY seq",
                (investigation_id,),
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM ledger ORDER BY seq").fetchall()
        return [dict(r) for r in rows]

    def verify_ledger(
        self, investigation_id: str | None = None
    ) -> tuple[bool, int, int | None]:
        """Recompute the hash chain. Returns (ok, entries_checked, bad_seq).

        The chain is global (prev_hash links every row regardless of
        investigation), so full verification always walks the whole table; an
        investigation_id filter only narrows which rows are *reported*, not how
        the chain is recomputed.
        """
        rows = self.list_ledger()
        prev_hash = GENESIS_HASH
        checked = 0
        for row in rows:
            if row["prev_hash"] != prev_hash:
                return False, checked, row["seq"]
            expected = ledger_entry_hash(prev_hash, row["payload_json"])
            if expected != row["entry_hash"]:
                return False, checked, row["seq"]
            prev_hash = row["entry_hash"]
            checked += 1
        return True, checked, None


# Singleton
store = Store()
