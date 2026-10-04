"""Storage layer for the spot-check reconciliation subsystem.

Owns every reconciliation table and the small schema migration that adds
revision tracking to restrictions and an idempotency key to notifications.
The judgment and service layers only ever touch the database through this
class so that persistence concerns stay out of the decision logic.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS recon_batches(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_code TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'open',            -- open | concluded
    conclusion TEXT,                                -- consistent | inconsistent | pending_review
    version INTEGER NOT NULL DEFAULT 1,
    receipt_count INTEGER NOT NULL DEFAULT 0,
    processed_count INTEGER NOT NULL DEFAULT 0,
    note TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    concluded_by TEXT,
    concluded_at TEXT
);
CREATE TABLE IF NOT EXISTS receipts(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    offline_id TEXT NOT NULL UNIQUE,
    batch_id INTEGER NOT NULL REFERENCES recon_batches(id),
    plan_id INTEGER NOT NULL REFERENCES flight_plans(id),
    plan_revision INTEGER NOT NULL,
    operator_id TEXT NOT NULL,
    actual_takeoff_at TEXT,
    actual_landing_at TEXT,
    result TEXT NOT NULL,                           -- completed | diverted | cancelled | unknown
    payload_json TEXT NOT NULL DEFAULT '{}',
    received_at TEXT NOT NULL,
    processed INTEGER NOT NULL DEFAULT 0,           -- 0 = pending, 1 = handled (retry only touches 0)
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_receipts_batch ON receipts(batch_id);
CREATE INDEX IF NOT EXISTS idx_receipts_plan ON receipts(plan_id);
CREATE INDEX IF NOT EXISTS idx_receipts_processed ON receipts(processed);
CREATE TABLE IF NOT EXISTS recon_conclusions(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id INTEGER NOT NULL REFERENCES recon_batches(id),
    plan_id INTEGER NOT NULL,
    receipt_id INTEGER NOT NULL REFERENCES receipts(id),
    plan_revision INTEGER NOT NULL,
    approval_id INTEGER,
    status TEXT NOT NULL,                           -- verified | invalidated | mismatch | conflict | pending
    reason TEXT NOT NULL,
    versions_json TEXT,                             -- two versions kept for auditor adjudication
    version INTEGER NOT NULL DEFAULT 1,
    adjudicated_by TEXT,
    adjudicated_at TEXT,
    adjudication TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_conclusions_batch ON recon_conclusions(batch_id);
CREATE INDEX IF NOT EXISTS idx_conclusions_plan ON recon_conclusions(plan_id);
CREATE INDEX IF NOT EXISTS idx_conclusions_status ON recon_conclusions(status);
CREATE TABLE IF NOT EXISTS deductions(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id INTEGER NOT NULL,
    receipt_id INTEGER NOT NULL UNIQUE,             -- one deduction per receipt, backstop against duplicates
    batch_id INTEGER NOT NULL,
    points INTEGER NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS supplement_deadlines(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id INTEGER NOT NULL,
    operator_id TEXT NOT NULL,
    batch_id INTEGER NOT NULL,
    reason TEXT NOT NULL,
    deadline_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',            -- open | submitted | expired
    created_at TEXT NOT NULL,
    resolved_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_deadlines_status ON supplement_deadlines(status);
"""

# Columns added to pre-existing tables. Applied idempotently for older databases.
_MIGRATIONS: list[tuple[str, str, str]] = [
    ("restrictions", "revision", "INTEGER NOT NULL DEFAULT 1"),
    ("restrictions", "updated_at", "TEXT"),
    ("notifications", "dedup_key", "TEXT"),
    ("approvals", "restrictions_json", "TEXT"),
]


class ReconStore:
    def __init__(self, path: str | Path):
        self.conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.executescript(SCHEMA)
        self._migrate()

    def _columns(self, table: str) -> list[str]:
        return [row[1] for row in self.conn.execute(f"PRAGMA table_info({table})")]

    def _migrate(self) -> None:
        for table, column, ddl in _MIGRATIONS:
            if column not in self._columns(table):
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
        # Idempotent notification delivery: the same (receipt, kind) never inserts twice.
        self.conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_notifications_dedup "
            "ON notifications(dedup_key) WHERE dedup_key IS NOT NULL"
        )

    # -- transaction helpers -------------------------------------------------
    @contextmanager
    def tx(self):
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    def begin(self) -> None:
        self.conn.execute("BEGIN IMMEDIATE")

    def commit(self) -> None:
        self.conn.execute("COMMIT")

    def rollback(self) -> None:
        self.conn.execute("ROLLBACK")

    def savepoint(self, name: str) -> None:
        self.conn.execute(f"SAVEPOINT {name}")

    def release(self, name: str) -> None:
        self.conn.execute(f"RELEASE SAVEPOINT {name}")

    def rollback_to(self, name: str) -> None:
        self.conn.execute(f"ROLLBACK TO SAVEPOINT {name}")

    # -- generic row helper ---------------------------------------------------
    @staticmethod
    def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return dict(row) if row else None
