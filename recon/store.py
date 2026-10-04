"""存储：抽查对账的表结构、幂等键与并发守卫。所有 SQL 只出现在本模块。"""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from common import iso

SCHEMA = """
CREATE TABLE IF NOT EXISTS recon_batches(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    created_by TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    receipt_deadline TEXT NOT NULL,
    evidence_hours INTEGER NOT NULL DEFAULT 72,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS recon_items(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id INTEGER NOT NULL REFERENCES recon_batches(id),
    plan_id INTEGER NOT NULL REFERENCES flight_plans(id),
    operator_id TEXT NOT NULL,
    callsign TEXT NOT NULL,
    plan_revision INTEGER NOT NULL,
    approval_id INTEGER NOT NULL REFERENCES approvals(id),
    expected_start TEXT NOT NULL,
    expected_end TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    finding TEXT,
    finding_detail TEXT,
    evidence_deadline TEXT,
    conclusion_kind TEXT,
    conclusion_note TEXT,
    concluded_by TEXT,
    concluded_at TEXT,
    accepted_receipt_id INTEGER,
    sheet_offline_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(batch_id, plan_id, plan_revision)
);
CREATE TABLE IF NOT EXISTS recon_receipts(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id INTEGER NOT NULL REFERENCES recon_items(id),
    batch_id INTEGER NOT NULL REFERENCES recon_batches(id),
    plan_id INTEGER NOT NULL REFERENCES flight_plans(id),
    kind TEXT NOT NULL DEFAULT 'flight',
    offline_id TEXT NOT NULL UNIQUE,
    reporter TEXT NOT NULL,
    operator_id TEXT NOT NULL,
    plan_revision_reported INTEGER,
    revision_mismatch INTEGER NOT NULL DEFAULT 0,
    actual_takeoff TEXT,
    actual_landing TEXT,
    note TEXT,
    standing TEXT NOT NULL DEFAULT 'candidate',
    post_conclusion INTEGER NOT NULL DEFAULT 0,
    received_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS recon_sheets(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id INTEGER NOT NULL REFERENCES recon_batches(id),
    auditor TEXT NOT NULL,
    sheet_offline_id TEXT NOT NULL UNIQUE,
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS score_deductions(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id INTEGER NOT NULL REFERENCES recon_items(id),
    operator_id TEXT NOT NULL,
    points INTEGER NOT NULL,
    reason TEXT NOT NULL,
    dedupe_key TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);
"""


class ReconStore:
    """复用应用 Repository 的连接与事务，把对账相关的 SQL 集中维护。"""

    def __init__(self, repo: Any):
        self.repo = repo
        self.conn = repo.conn
        self.conn.executescript(SCHEMA)
        columns = {row[1] for row in self.conn.execute("PRAGMA table_info(notifications)")}
        if "dedupe_key" not in columns:
            self.conn.execute("ALTER TABLE notifications ADD COLUMN dedupe_key TEXT")
        self.conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_notifications_dedupe ON notifications(dedupe_key)")

    # ---- 监管批次 ----
    def insert_batch(self, conn: sqlite3.Connection, code: str, title: str, created_by: str,
                     receipt_deadline: str, evidence_hours: int) -> int:
        cur = conn.execute("INSERT INTO recon_batches(code,title,created_by,receipt_deadline,evidence_hours,created_at) VALUES(?,?,?,?,?,?)",
                           (code, title, created_by, receipt_deadline, evidence_hours, iso()))
        return cur.lastrowid

    def batch_by_id(self, conn: sqlite3.Connection, batch_id: int) -> sqlite3.Row | None:
        return conn.execute("SELECT * FROM recon_batches WHERE id=?", (batch_id,)).fetchone()

    def batch_by_code(self, conn: sqlite3.Connection, code: str) -> sqlite3.Row | None:
        return conn.execute("SELECT * FROM recon_batches WHERE code=?", (code,)).fetchone()

    def list_batches(self, conn: sqlite3.Connection, operator: str | None = None) -> list[sqlite3.Row]:
        base = """SELECT b.*, (SELECT COUNT(*) FROM recon_items i WHERE i.batch_id=b.id{op}) AS item_count,
                         (SELECT COUNT(*) FROM recon_items i WHERE i.batch_id=b.id{op} AND i.status='reconciled') AS reconciled_count
                  FROM recon_batches b {where} ORDER BY b.id DESC"""
        if operator is None:
            return conn.execute(base.format(op="", where="")).fetchall()
        return conn.execute(base.format(op=" AND i.operator_id=?", where="WHERE EXISTS(SELECT 1 FROM recon_items i WHERE i.batch_id=b.id AND i.operator_id=?)"),
                            (operator, operator, operator)).fetchall()

    # ---- 对账条目 ----
    def insert_item(self, conn: sqlite3.Connection, batch_id: int, plan: sqlite3.Row, approval: sqlite3.Row) -> int:
        cur = conn.execute("""INSERT INTO recon_items(batch_id,plan_id,operator_id,callsign,plan_revision,approval_id,expected_start,expected_end,created_at,updated_at)
                              VALUES(?,?,?,?,?,?,?,?,?,?)""",
                           (batch_id, plan["id"], plan["operator_id"], plan["callsign"], plan["revision"], approval["id"],
                            plan["starts_at"], plan["ends_at"], iso(), iso()))
        return cur.lastrowid

    def item_by_id(self, conn: sqlite3.Connection, item_id: int) -> sqlite3.Row | None:
        return conn.execute("SELECT * FROM recon_items WHERE id=?", (item_id,)).fetchone()

    def item_by_batch_plan(self, conn: sqlite3.Connection, batch_id: int, plan_id: int) -> sqlite3.Row | None:
        return conn.execute("SELECT * FROM recon_items WHERE batch_id=? AND plan_id=? ORDER BY id DESC LIMIT 1", (batch_id, plan_id)).fetchone()

    def items_for_batch(self, conn: sqlite3.Connection, batch_id: int, operator: str | None = None) -> list[sqlite3.Row]:
        if operator is None:
            return conn.execute("SELECT * FROM recon_items WHERE batch_id=? ORDER BY id", (batch_id,)).fetchall()
        return conn.execute("SELECT * FROM recon_items WHERE batch_id=? AND operator_id=? ORDER BY id", (batch_id, operator)).fetchall()

    def console_items(self, conn: sqlite3.Connection, operator: str | None = None) -> list[sqlite3.Row]:
        base = """SELECT i.*, b.code AS batch_code, b.title AS batch_title, b.receipt_deadline,
                         (SELECT COALESCE(SUM(d.points),0) FROM score_deductions d WHERE d.item_id=i.id) AS points
                  FROM recon_items i JOIN recon_batches b ON b.id=i.batch_id"""
        if operator is None:
            return conn.execute(base + " ORDER BY i.id").fetchall()
        return conn.execute(base + " WHERE i.operator_id=? ORDER BY i.id", (operator,)).fetchall()

    def open_items_for_plan_revision(self, conn: sqlite3.Connection, plan_id: int, revision: int) -> list[sqlite3.Row]:
        return conn.execute("SELECT * FROM recon_items WHERE plan_id=? AND plan_revision=? AND status!='reconciled'", (plan_id, revision)).fetchall()

    def invalidate_item(self, conn: sqlite3.Connection, item_id: int, evidence_deadline: str) -> None:
        conn.execute("UPDATE recon_items SET status='invalidated', finding='invalidated', evidence_deadline=?, updated_at=? WHERE id=?",
                     (evidence_deadline, iso(), item_id))

    def mark_contested(self, conn: sqlite3.Connection, item_id: int) -> None:
        conn.execute("""UPDATE recon_items SET status='contested', finding='contested', finding_detail=NULL,
                        conclusion_kind=NULL, conclusion_note=NULL, concluded_by=NULL, concluded_at=NULL, accepted_receipt_id=NULL, updated_at=? WHERE id=?""",
                     (iso(), item_id))

    def mark_deviation(self, conn: sqlite3.Connection, item_id: int, reasons: list[str]) -> None:
        conn.execute("""UPDATE recon_items SET status='deviation', finding='deviation', finding_detail=?,
                        conclusion_kind=NULL, conclusion_note=NULL, concluded_by=NULL, concluded_at=NULL, accepted_receipt_id=NULL, updated_at=? WHERE id=?""",
                     (json.dumps(reasons, ensure_ascii=False), iso(), item_id))

    def auto_reconcile(self, conn: sqlite3.Connection, item_id: int) -> None:
        conn.execute("""UPDATE recon_items SET status='reconciled', finding=NULL, finding_detail=NULL, conclusion_kind='auto_matched',
                        concluded_by='system', concluded_at=?, updated_at=? WHERE id=?""", (iso(), iso(), item_id))

    def mark_no_show(self, conn: sqlite3.Connection, item_id: int) -> None:
        conn.execute("UPDATE recon_items SET status='no_show', finding='no_show', updated_at=? WHERE id=?", (iso(), item_id))

    def mark_evidence_submitted(self, conn: sqlite3.Connection, item_id: int) -> None:
        conn.execute("UPDATE recon_items SET status='evidence_submitted', updated_at=? WHERE id=?", (iso(), item_id))

    def conclude_item(self, conn: sqlite3.Connection, item_id: int, verdict: str, note: str, auditor: str,
                      accept_receipt_id: int | None, sheet_offline_id: str) -> None:
        # WHERE status!='reconciled' 是先写先得的守卫：并发下只有第一个事务能改写结论
        conn.execute("""UPDATE recon_items SET status='reconciled', conclusion_kind=?, conclusion_note=?, concluded_by=?, concluded_at=?,
                        accepted_receipt_id=?, sheet_offline_id=?, updated_at=? WHERE id=? AND status!='reconciled'""",
                     (verdict, note, auditor, iso(), accept_receipt_id, sheet_offline_id, iso(), item_id))

    def pending_past_receipt_deadline(self, conn: sqlite3.Connection, now: str) -> list[sqlite3.Row]:
        return conn.execute("""SELECT i.*, b.code AS batch_code FROM recon_items i JOIN recon_batches b ON b.id=i.batch_id
                               WHERE i.status='pending' AND b.receipt_deadline<?""", (now,)).fetchall()

    def invalidated_past_evidence_deadline(self, conn: sqlite3.Connection, now: str) -> list[sqlite3.Row]:
        return conn.execute("""SELECT i.*, b.code AS batch_code FROM recon_items i JOIN recon_batches b ON b.id=i.batch_id
                               WHERE i.status='invalidated' AND i.evidence_deadline IS NOT NULL AND i.evidence_deadline<?""", (now,)).fetchall()

    # ---- 回执 ----
    def receipt_by_offline_id(self, conn: sqlite3.Connection, offline_id: str) -> sqlite3.Row | None:
        return conn.execute("SELECT * FROM recon_receipts WHERE offline_id=?", (offline_id,)).fetchone()

    def insert_receipt(self, conn: sqlite3.Connection, item: sqlite3.Row, kind: str, offline_id: str, reporter: str,
                       reported_revision: int | None, mismatch: int, actual_takeoff: str | None,
                       actual_landing: str | None, note: str) -> int:
        cur = conn.execute("""INSERT INTO recon_receipts(item_id,batch_id,plan_id,kind,offline_id,reporter,operator_id,plan_revision_reported,
                              revision_mismatch,actual_takeoff,actual_landing,note,received_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                           (item["id"], item["batch_id"], item["plan_id"], kind, offline_id, reporter, item["operator_id"],
                            reported_revision, mismatch, actual_takeoff, actual_landing, note, iso()))
        return cur.lastrowid

    def receipts_for_item(self, conn: sqlite3.Connection, item_id: int) -> list[sqlite3.Row]:
        return conn.execute("SELECT * FROM recon_receipts WHERE item_id=? ORDER BY id", (item_id,)).fetchall()

    def settle_receipt_standings(self, conn: sqlite3.Connection, item_id: int, accepted_id: int) -> None:
        conn.execute("UPDATE recon_receipts SET standing='accepted' WHERE id=?", (accepted_id,))
        conn.execute("UPDATE recon_receipts SET standing='rejected' WHERE item_id=? AND kind='flight' AND id!=? AND standing='candidate'",
                     (item_id, accepted_id))

    def mark_post_conclusion(self, conn: sqlite3.Connection, receipt_id: int) -> None:
        conn.execute("UPDATE recon_receipts SET post_conclusion=1 WHERE id=?", (receipt_id,))

    # ---- 裁决单 ----
    def sheet_by_offline_id(self, conn: sqlite3.Connection, sheet_offline_id: str) -> sqlite3.Row | None:
        return conn.execute("SELECT * FROM recon_sheets WHERE sheet_offline_id=?", (sheet_offline_id,)).fetchone()

    def insert_sheet(self, conn: sqlite3.Connection, batch_id: int, auditor: str, sheet_offline_id: str, result_json: str) -> None:
        conn.execute("INSERT INTO recon_sheets(batch_id,auditor,sheet_offline_id,result_json,created_at) VALUES(?,?,?,?,?)",
                     (batch_id, auditor, sheet_offline_id, result_json, iso()))

    # ---- 扣分台账 ---- 扣分只写一次，dedupe_key 挡住重试与重启后的重复扣分 ----
    def insert_deduction(self, conn: sqlite3.Connection, item_id: int, operator_id: int | str, points: int,
                         reason: str, dedupe_key: str) -> None:
        conn.execute("INSERT INTO score_deductions(item_id,operator_id,points,reason,dedupe_key,created_at) VALUES(?,?,?,?,?,?)",
                     (item_id, operator_id, points, reason, dedupe_key, iso()))

    def deductions_for_item(self, conn: sqlite3.Connection, item_id: int) -> list[sqlite3.Row]:
        return conn.execute("SELECT * FROM score_deductions WHERE item_id=? ORDER BY id", (item_id,)).fetchall()

    # ---- 通知（去重键保证重试、重放、重启后不重复发通知）----
    def notify_dedupe(self, conn: sqlite3.Connection, plan_id: int, kind: str, message: str, dedupe_key: str) -> None:
        conn.execute("INSERT OR IGNORE INTO notifications(plan_id,kind,message,created_at,dedupe_key) VALUES(?,?,?,?,?)",
                     (plan_id, kind, message, iso(), dedupe_key))
