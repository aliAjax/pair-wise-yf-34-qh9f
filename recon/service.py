"""Service layer: orchestrates storage and the judge.

Responsible for the parts that must survive failures and concurrency:

* offline receipts are idempotent on ``offline_id`` (replays record once);
* a receipt lands on the approved revision it names;
* each receipt is processed in its own transaction so a write failure only
  leaves ``processed=0`` entries to retry;
* two auditors concluding the same batch are serialized; the first write
  wins and the second reads the current conclusion;
* deductions and notifications carry idempotency keys so a restart never
  double-charges or double-notifies;
* the coordination desk is assembled per role.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import timedelta
from typing import Any
from uuid import uuid4

from . import util
from .judge import CONFLICT, INVALIDATED, MISMATCH, PENDING, VERIFIED, ReconJudge, Verdict
from .storage import ReconStore
from .util import iso, utcnow

BATCH_OPEN = "open"
BATCH_CONCLUDED = "concluded"


class ReconciliationService:
    def __init__(self, path: str):
        self.store = ReconStore(path)
        self.judge = ReconJudge(self.store.conn)
        # Serializes in-process threads; BEGIN IMMEDIATE serializes across processes.
        self._lock = threading.RLock()

    # -- helpers --------------------------------------------------------------
    def _require_role(self, role: str, allowed: set[str], action: str) -> None:
        if role not in allowed:
            raise util.ReconError(403, "recon_forbidden", f"当前角色无权{action}")

    def _batch_row(self, batch_id: int) -> sqlite3.Row:
        row = self.store.conn.execute("SELECT * FROM recon_batches WHERE id=?", (batch_id,)).fetchone()
        if not row:
            raise util.ReconError(404, "batch_not_found", "监管批次不存在")
        return row

    @staticmethod
    def _generate_batch_code() -> str:
        return f"BATCH-{utcnow():%Y%m%d}-{uuid4().hex[:6].upper()}"

    def _notify(self, conn: sqlite3.Connection, plan_id: int, kind: str, message: str, receipt_id: int) -> None:
        # Idempotent: the same (receipt, kind) notification is inserted at most once.
        conn.execute(
            "INSERT OR IGNORE INTO notifications(plan_id,kind,message,created_at,dedup_key) VALUES(?,?,?,?,?)",
            (plan_id, kind, message, iso(), f"recon:{receipt_id}:{kind}"),
        )

    # -- batches --------------------------------------------------------------
    def create_batch(self, actor: str, role: str, body: dict[str, Any]) -> dict[str, Any]:
        self._require_role(role, {"airspace_reviewer", "commander", "auditor"}, "创建监管批次")
        with self._lock:
            code = str(body.get("batch_code", "")).strip() or self._generate_batch_code()
            note = str(body.get("note", "")).strip()
            with self.store.tx():
                try:
                    cur = self.store.conn.execute(
                        "INSERT INTO recon_batches(batch_code,note,created_by,created_at) VALUES(?,?,?,?)",
                        (code, note, actor, iso()),
                    )
                except sqlite3.IntegrityError as exc:
                    raise util.ReconError(409, "batch_duplicate", "监管批次编号已存在") from exc
                return self._batch_dict(cur.lastrowid)

    def _batch_dict(self, batch_id: int) -> dict[str, Any]:
        batch = dict(self._batch_row(batch_id))
        receipts = [dict(r) for r in self.store.conn.execute(
            "SELECT * FROM receipts WHERE batch_id=? ORDER BY id", (batch_id,))]
        conclusions = [dict(r) for r in self.store.conn.execute(
            "SELECT * FROM recon_conclusions WHERE batch_id=? ORDER BY id", (batch_id,))]
        batch["receipts"] = receipts
        batch["conclusions"] = conclusions
        return batch

    def list_batches(self, role: str) -> dict[str, Any]:
        self._require_role(role, {"airspace_reviewer", "commander", "auditor"}, "查看监管批次")
        rows = [dict(r) for r in self.store.conn.execute("SELECT * FROM recon_batches ORDER BY id DESC")]
        return {"batches": rows}

    def get_batch(self, batch_id: int, role: str) -> dict[str, Any]:
        self._require_role(role, {"airspace_reviewer", "commander", "auditor"}, "查看监管批次")
        return self._batch_dict(batch_id)

    # -- receipts -------------------------------------------------------------
    def submit_receipt(self, batch_id: int, actor: str, role: str, operator: str, body: dict[str, Any]) -> dict[str, Any]:
        if role != "operator":
            raise util.ReconError(403, "receipt_forbidden", "只有运营方可以回传回执")
        offline_id = str(body.get("offline_id", "")).strip()
        result = str(body.get("result", "")).strip()
        if not offline_id:
            raise util.ReconError(400, "offline_id_required", "回执 offline_id 必填，用于重放去重")
        if result not in {"completed", "diverted", "cancelled", "unknown"}:
            raise util.ReconError(400, "invalid_result", "回执 result 必须是 completed/diverted/cancelled/unknown")
        try:
            plan_id = int(body.get("plan_id"))
            plan_revision = int(body.get("plan_revision"))
        except (TypeError, ValueError):
            raise util.ReconError(400, "invalid_receipt", "plan_id 和 plan_revision 必须为整数")
        takeoff = body.get("actual_takeoff_at")
        landing = body.get("actual_landing_at")
        if takeoff:
            util.parse_time(takeoff)
        if landing:
            util.parse_time(landing)

        with self._lock:
            batch = self._batch_row(batch_id)
            # Replay: the same offline_id is recorded exactly once.
            existing = self.store.conn.execute("SELECT * FROM receipts WHERE offline_id=?", (offline_id,)).fetchone()
            if existing:
                if existing["batch_id"] != batch_id or existing["plan_id"] != plan_id:
                    raise util.ReconError(409, "offline_id_conflict", "该回执编号已用于其他批次或计划")
                conclusion = self.store.conn.execute(
                    "SELECT * FROM recon_conclusions WHERE receipt_id=? ORDER BY id DESC LIMIT 1", (existing["id"],)).fetchone()
                return {"receipt": dict(existing), "conclusion": dict(conclusion) if conclusion else None, "idempotent": True}

            with self.store.tx():
                cur = self.store.conn.execute(
                    """INSERT INTO receipts(offline_id,batch_id,plan_id,plan_revision,operator_id,
                                            actual_takeoff_at,actual_landing_at,result,payload_json,received_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (offline_id, batch_id, plan_id, plan_revision, operator, takeoff, landing, result,
                     json.dumps(body.get("payload", {}), ensure_ascii=False), iso()),
                )
                receipt_id = cur.lastrowid
                self.store.conn.execute("UPDATE recon_batches SET receipt_count=receipt_count+1 WHERE id=?", (batch_id,))

            receipt = self.store.conn.execute("SELECT * FROM receipts WHERE id=?", (receipt_id,)).fetchone()
            # Late arrival after the batch was concluded: process it immediately.
            processed = False
            if batch["status"] == BATCH_CONCLUDED:
                self._process_receipt(receipt_id)
                processed = True
            return {"receipt": dict(receipt), "processed": processed, "idempotent": False}

    def _process_receipt(self, receipt_id: int) -> dict[str, Any] | None:
        """Process one receipt in its own transaction.

        Re-reads the receipt inside the transaction so a concurrent auditor
        that already handled it makes this one a no-op. Only ``processed=0``
        entries are ever written; a failure rolls everything back so the
        entry stays unprocessed for a later retry.
        """
        with self._lock:
            return self._process_receipt_locked(receipt_id)

    def _process_receipt_locked(self, receipt_id: int) -> dict[str, Any] | None:
        conn = self.store.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            receipt = conn.execute("SELECT * FROM receipts WHERE id=?", (receipt_id,)).fetchone()
            if not receipt or receipt["processed"]:
                conn.execute("COMMIT")
                existing = conn.execute(
                    "SELECT * FROM recon_conclusions WHERE receipt_id=? ORDER BY id DESC LIMIT 1", (receipt_id,)).fetchone()
                return dict(existing) if existing else None

            verdict = self.judge.judge(receipt)
            conclusion_id = self._apply_verdict(conn, receipt, verdict)
            conn.execute("UPDATE receipts SET processed=1, error=NULL WHERE id=?", (receipt_id,))
            conn.execute("UPDATE recon_batches SET processed_count=processed_count+1 WHERE id=?", (receipt["batch_id"],))
            conn.execute("COMMIT")
            row = conn.execute("SELECT * FROM recon_conclusions WHERE id=?", (conclusion_id,)).fetchone()
            return dict(row)
        except Exception:
            conn.execute("ROLLBACK")
            raise

    def _apply_verdict(self, conn: sqlite3.Connection, receipt: sqlite3.Row, verdict: Verdict) -> int:
        plan = conn.execute("SELECT * FROM flight_plans WHERE id=?", (receipt["plan_id"],)).fetchone()
        versions_json = json.dumps(verdict.versions, ensure_ascii=False) if verdict.versions else None
        cur = conn.execute(
            """INSERT INTO recon_conclusions(batch_id,plan_id,receipt_id,plan_revision,approval_id,
                                            status,reason,versions_json,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (receipt["batch_id"], receipt["plan_id"], receipt["id"], receipt["plan_revision"],
             verdict.approval_id, verdict.status, verdict.reason, versions_json, iso(), iso()),
        )
        conclusion_id = cur.lastrowid

        if verdict.deduction_points is not None:
            # UNIQUE(receipt_id) backstop: a restart/retry can never deduct twice.
            conn.execute(
                "INSERT OR IGNORE INTO deductions(plan_id,receipt_id,batch_id,points,reason,created_at) VALUES(?,?,?,?,?,?)",
                (receipt["plan_id"], receipt["id"], receipt["batch_id"], verdict.deduction_points, verdict.deduction_reason, iso()),
            )

        if verdict.supplement_hours is not None and plan:
            deadline = iso(utcnow() + timedelta(hours=verdict.supplement_hours))
            conn.execute(
                "INSERT INTO supplement_deadlines(plan_id,operator_id,batch_id,reason,deadline_at,created_at) VALUES(?,?,?,?,?,?)",
                (receipt["plan_id"], plan["operator_id"], receipt["batch_id"], verdict.reason, deadline, iso()),
            )

        if verdict.notify_kind and plan:
            self._notify(conn, receipt["plan_id"], verdict.notify_kind, verdict.notify_message or verdict.reason, receipt["id"])

        if verdict.status == CONFLICT:
            # The whole plan's actuals are now in dispute: bring prior conclusions
            # for the plan into the same pending adjudication with both versions.
            conn.execute(
                "UPDATE recon_conclusions SET status=?, versions_json=?, updated_at=? WHERE plan_id=? AND id!=?",
                (CONFLICT, versions_json, iso(), receipt["plan_id"], conclusion_id),
            )

        return conclusion_id

    # -- batch conclusion (optimistic, concurrent-safe) ----------------------
    def conclude_batch(self, batch_id: int, actor: str, role: str, body: dict[str, Any]) -> dict[str, Any]:
        self._require_role(role, {"airspace_reviewer", "commander", "auditor"}, "结束监管批次")
        with self._lock:
            return self._conclude_batch_locked(batch_id, actor, role, body)

    def _conclude_batch_locked(self, batch_id: int, actor: str, role: str, body: dict[str, Any]) -> dict[str, Any]:
        note = str(body.get("note", "")).strip()

        # Late replay of the conclusion: a second auditor sees the current verdict.
        batch = self._batch_row(batch_id)
        if batch["status"] == BATCH_CONCLUDED:
            return {"batch": self._batch_dict(batch_id), "idempotent": True, "concluded": True}

        # Process every still-unprocessed receipt; each is its own transaction so
        # a failure only leaves the remaining processed=0 entries for a retry.
        errors: list[dict[str, Any]] = []
        pending = self.store.conn.execute(
            "SELECT * FROM receipts WHERE batch_id=? AND processed=0 ORDER BY id", (batch_id,)).fetchall()
        for receipt in pending:
            try:
                self._process_receipt(receipt["id"])
            except Exception as exc:  # noqa: BLE001 - keep the batch usable, surface the failure
                errors.append({"receipt_id": receipt["id"], "offline_id": receipt["offline_id"], "error": str(exc)})

        # Finalize the batch. Re-read under the write lock so a concurrent auditor
        # that committed first wins and this one observes the current conclusion.
        self.store.conn.execute("BEGIN IMMEDIATE")
        try:
            batch = self._batch_row(batch_id)
            if batch["status"] == BATCH_CONCLUDED:
                self.store.conn.execute("COMMIT")
                return {"batch": self._batch_dict(batch_id), "idempotent": True, "concluded": True}

            conclusions = self.store.conn.execute(
                "SELECT status FROM recon_conclusions WHERE batch_id=?", (batch_id,)).fetchall()
            statuses = {row["status"] for row in conclusions}
            if errors or CONFLICT in statuses or PENDING in statuses:
                conclusion = "pending_review"
            elif INVALIDATED in statuses or MISMATCH in statuses:
                conclusion = "inconsistent"
            elif statuses and statuses <= {VERIFIED}:
                conclusion = "consistent"
            else:
                conclusion = "pending_review"

            if errors:
                # Keep the batch open so the unprocessed entries can be retried.
                self.store.conn.execute(
                    "UPDATE recon_batches SET note=? WHERE id=?",
                    (f"部分条目处理失败，待重试：{len(errors)} 条", batch_id))
                self.store.conn.execute("COMMIT")
                return {"batch": self._batch_dict(batch_id), "concluded": False, "errors": errors}

            self.store.conn.execute(
                """UPDATE recon_batches SET status='concluded',conclusion=?,version=version+1,
                                            concluded_by=?,concluded_at=?,note=? WHERE id=?""",
                (conclusion, actor, iso(), note, batch_id),
            )
            self.store.conn.execute("COMMIT")
        except Exception:
            self.store.conn.execute("ROLLBACK")
            raise

        return {"batch": self._batch_dict(batch_id), "concluded": True, "errors": []}

    # -- coordination desk ----------------------------------------------------
    def desk(self, role: str, operator: str) -> dict[str, Any]:
        conn = self.store.conn
        if role == "viewer":
            # Public role only sees verified (already-approved) flights.
            pending: list[dict[str, Any]] = []
            verified = self._desk_rows(
                conn,
                "SELECT c.*, p.callsign, p.operator_id, p.region FROM recon_conclusions c JOIN flight_plans p ON p.id=c.plan_id "
                "WHERE c.status=? ORDER BY c.updated_at DESC",
                (VERIFIED,), role, operator,
            )
            deadlines: list[dict[str, Any]] = []
        else:
            pending = self._desk_rows(
                conn,
                "SELECT c.*, p.callsign, p.operator_id, p.region FROM recon_conclusions c JOIN flight_plans p ON p.id=c.plan_id "
                "WHERE c.status IN (?, ?, ?) ORDER BY c.updated_at DESC",
                (PENDING, CONFLICT, INVALIDATED), role, operator,
            )
            verified = self._desk_rows(
                conn,
                "SELECT c.*, p.callsign, p.operator_id, p.region FROM recon_conclusions c JOIN flight_plans p ON p.id=c.plan_id "
                "WHERE c.status=? ORDER BY c.updated_at DESC",
                (VERIFIED,), role, operator,
            )
            deadlines = self._desk_rows(
                conn,
                "SELECT d.*, p.callsign FROM supplement_deadlines d JOIN flight_plans p ON p.id=d.plan_id "
                "WHERE d.status='open' ORDER BY d.deadline_at ASC",
                (), role, operator,
            )
        now = utcnow()
        for row in deadlines:
            row["hours_remaining"] = round((util.parse_time(row["deadline_at"]) - now).total_seconds() / 3600, 1)
        return {
            "server_time": iso(),
            "role": role,
            "pending_review": pending,
            "verified": verified,
            "supplement_deadlines": deadlines,
            "summary": {"pending_review": len(pending), "verified": len(verified), "supplement_deadlines": len(deadlines)},
        }

    def _desk_rows(self, conn: sqlite3.Connection, sql: str, params: tuple, role: str, operator: str) -> list[dict[str, Any]]:
        rows = [dict(r) for r in conn.execute(sql, params)]
        if role == "operator":
            rows = [r for r in rows if r.get("operator_id") == operator]
        elif role == "viewer":
            rows = [r for r in rows if r.get("status") == VERIFIED]
        return rows

    # -- auditor adjudication of conflicting versions -------------------------
    def adjudicate(self, conclusion_id: int, actor: str, role: str, body: dict[str, Any]) -> dict[str, Any]:
        self._require_role(role, {"auditor", "commander"}, "裁决冲突")
        decision = str(body.get("decision", "")).strip()
        note = str(body.get("note", "")).strip()
        if decision not in {"accept", "reject", "need_more"}:
            raise util.ReconError(400, "invalid_decision", "decision 必须是 accept/reject/need_more")
        status = {"accept": VERIFIED, "reject": MISMATCH, "need_more": PENDING}[decision]
        with self._lock:
            with self.store.tx():
                row = self.store.conn.execute("SELECT * FROM recon_conclusions WHERE id=?", (conclusion_id,)).fetchone()
                if not row:
                    raise util.ReconError(404, "conclusion_not_found", "对账结论不存在")
                self.store.conn.execute(
                    """UPDATE recon_conclusions SET status=?,adjudicated_by=?,adjudicated_at=?,adjudication=?,
                                                    version=version+1,updated_at=? WHERE id=?""",
                    (status, actor, iso(), note, iso(), conclusion_id),
                )
            updated = self.store.conn.execute("SELECT * FROM recon_conclusions WHERE id=?", (conclusion_id,)).fetchone()
        return {"conclusion": dict(updated), "idempotent": False}
