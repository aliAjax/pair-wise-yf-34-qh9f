"""编排：角色校验、幂等回执、裁决与级联失效。判定规则见 adjudication.py，SQL 见 store.py。"""
from __future__ import annotations

import json
import sqlite3
from datetime import timedelta
from typing import Any

from common import ApiError, boxes_overlap, iso, parse_time, route_bbox, times_overlap, utcnow
from recon import adjudication
from recon.store import ReconStore

REVIEW_ROLES = {"auditor", "commander", "airspace_reviewer"}
# 条目状态机：pending 待回执 → matched 自动核销(reconciled) / deviation 偏差 / contested 冲突待裁决；
# 计划或限制变化 → invalidated 待补证 → evidence_submitted 已补证；逾期未回执 → no_show；终态 reconciled 已核销。
PENDING_REVIEW_STATUSES = {"deviation", "contested", "evidence_submitted", "no_show"}


class ReconService:
    def __init__(self, repo: Any):
        self.repo = repo
        self.store = ReconStore(repo)

    # ---------------- 监管批次 ----------------
    def create_batch(self, actor: str, role: str, body: dict[str, Any]) -> dict[str, Any]:
        if role not in {"auditor", "commander"}: raise ApiError(403, "batch_forbidden", "只有监管审核员或指挥官可以创建抽查批次")
        code, title = str(body.get("code", "")).strip(), str(body.get("title", "")).strip()
        plan_ids = body.get("plan_ids")
        if not code or not title or not isinstance(plan_ids, list) or not plan_ids:
            raise ApiError(400, "invalid_batch", "批次号、标题和非空 plan_ids 必填")
        deadline = iso(parse_time(body["receipt_deadline"])) if body.get("receipt_deadline") else iso(utcnow() + timedelta(days=7))
        evidence_hours = body.get("evidence_hours", 72)
        if not isinstance(evidence_hours, int) or evidence_hours <= 0: raise ApiError(400, "invalid_batch", "evidence_hours 必须是正整数")
        with self.repo.tx() as conn:
            if self.store.batch_by_code(conn, code): raise ApiError(409, "batch_exists", "监管批次号已存在")
            batch_id = self.store.insert_batch(conn, code, title, actor, deadline, evidence_hours)
            results, seen = [], set()
            for plan_id in plan_ids:
                if plan_id in seen: continue
                seen.add(plan_id)
                plan = conn.execute("SELECT * FROM flight_plans WHERE id=?", (plan_id,)).fetchone() if isinstance(plan_id, int) else None
                if not plan:
                    results.append({"plan_id": plan_id, "result": "error", "error": "plan_not_found"}); continue
                if plan["status"] != "approved":
                    results.append({"plan_id": plan_id, "result": "error", "error": "plan_not_approved", "status": plan["status"]}); continue
                approval = conn.execute("SELECT * FROM approvals WHERE plan_id=? AND plan_revision=? AND decision='approved' ORDER BY id DESC LIMIT 1",
                                        (plan_id, plan["revision"])).fetchone()
                if not approval:
                    results.append({"plan_id": plan_id, "result": "error", "error": "approval_not_found"}); continue
                item_id = self.store.insert_item(conn, batch_id, plan, approval)
                results.append({"plan_id": plan_id, "result": "created", "item_id": item_id, "plan_revision": plan["revision"]})
            self.repo.audit(conn, None, actor, role, "recon_batch_created",
                            {"batch_id": batch_id, "code": code, "created": sum(1 for r in results if r["result"] == "created")})
            batch = dict(self.store.batch_by_id(conn, batch_id))
        return {"batch": batch, "results": results}

    def list_batches(self, role: str, operator: str) -> dict[str, Any]:
        if role == "operator": rows = self.store.list_batches(self.store.conn, operator)
        elif role in REVIEW_ROLES: rows = self.store.list_batches(self.store.conn)
        else: raise ApiError(403, "console_forbidden", "当前角色不能查看抽查批次")
        return {"batches": [dict(r) for r in rows]}

    def batch_detail(self, role: str, operator: str, batch_id: int) -> dict[str, Any]:
        batch = self.store.batch_by_id(self.store.conn, batch_id)
        if not batch: raise ApiError(404, "batch_not_found", "抽查批次不存在")
        if role == "operator": items = self.store.items_for_batch(self.store.conn, batch_id, operator)
        elif role in REVIEW_ROLES: items = self.store.items_for_batch(self.store.conn, batch_id)
        else: raise ApiError(403, "console_forbidden", "当前角色不能查看抽查批次")
        return {"batch": dict(batch), "items": [dict(r) for r in items]}

    # ---------------- 回执（离线、晚到、可重放） ----------------
    def ingest_receipts(self, actor: str, role: str, operator: str, batch_id: int, body: dict[str, Any]) -> dict[str, Any]:
        if role != "operator": raise ApiError(403, "receipt_forbidden", "只有运营方可以回传回执")
        entries = body.get("entries")
        if not isinstance(entries, list) or not entries: raise ApiError(400, "entries_required", "entries 必须是非空数组")
        if not self.store.batch_by_id(self.store.conn, batch_id): raise ApiError(404, "batch_not_found", "抽查批次不存在")
        results = []
        for entry in entries:
            if not isinstance(entry, dict):
                results.append({"result": "error", "error": "invalid_entry", "message": "回执条目必须是对象"}); continue
            try:
                with self.repo.tx() as conn:  # 每条回执独立事务：写入失败后只重试没有处理的条目
                    results.append(self._ingest_one(conn, batch_id, actor, operator, entry))
            except ApiError as exc:
                results.append({"offline_id": entry.get("offline_id"), "result": "error", "error": exc.code, "message": exc.message})
        return {"batch_id": batch_id, "results": results}

    def _ingest_one(self, conn: sqlite3.Connection, batch_id: int, actor: str, operator: str, entry: dict[str, Any]) -> dict[str, Any]:
        offline_id = str(entry.get("offline_id", "")).strip()
        if not offline_id: raise ApiError(400, "offline_id_required", "回执必须携带 offline_id")
        prior = self.store.receipt_by_offline_id(conn, offline_id)
        if prior:  # 重放只记一次，直接返回首次落库的结果
            return {"offline_id": offline_id, "result": "duplicate", "receipt_id": prior["id"], "item_id": prior["item_id"]}
        kind = str(entry.get("kind", "flight")).strip()
        if kind not in {"flight", "supplement"}: raise ApiError(400, "invalid_kind", "kind 必须是 flight 或 supplement")
        plan_id = entry.get("plan_id")
        if not isinstance(plan_id, int): raise ApiError(400, "invalid_entry", "plan_id 必填")
        item = self.store.item_by_batch_plan(conn, batch_id, plan_id)  # 回执按监管批次落到批次锁定的批准版本
        if not item: raise ApiError(404, "item_not_found", "该计划不在此监管批次中")
        if item["operator_id"] != operator: raise ApiError(403, "forbidden_operator", "只能回传本运营方计划的回执")
        takeoff = landing = None
        if kind == "flight":
            takeoff, landing = parse_time(entry.get("actual_takeoff")), parse_time(entry.get("actual_landing"))
        reported = entry.get("plan_revision")
        mismatch = 1 if isinstance(reported, int) and reported != item["plan_revision"] else 0
        receipt_id = self.store.insert_receipt(conn, item, kind, offline_id, actor, reported, mismatch,
                                               iso(takeoff) if takeoff else None, iso(landing) if landing else None,
                                               str(entry.get("note", "")).strip())
        self.repo.audit(conn, plan_id, actor, "operator", "receipt_recorded",
                        {"offline_id": offline_id, "item_id": item["id"], "kind": kind, "revision_mismatch": bool(mismatch)})
        if kind == "flight": self._apply_flight_receipt(conn, item, receipt_id, takeoff, landing)
        elif item["status"] == "invalidated": self.store.mark_evidence_submitted(conn, item["id"])
        fresh = self.store.item_by_id(conn, item["id"])
        return {"offline_id": offline_id, "result": "recorded", "receipt_id": receipt_id, "item_id": item["id"],
                "revision_mismatch": bool(mismatch), "item_status": fresh["status"]}

    def _apply_flight_receipt(self, conn: sqlite3.Connection, item: sqlite3.Row, receipt_id: int, takeoff, landing) -> None:
        status = item["status"]
        if status == "reconciled" and item["concluded_by"] != "system":
            self.store.mark_post_conclusion(conn, receipt_id)  # 人工结论不回翻，晚到回执仅标记留存
            return
        if status in {"contested", "invalidated", "evidence_submitted"}:
            return  # 冲突中等待裁决；批准失效走补证流程，飞行回执仅留存
        for other in self.store.receipts_for_item(conn, item["id"]):
            if other["id"] == receipt_id or other["kind"] != "flight" or other["standing"] != "candidate": continue
            if adjudication.flights_conflict(parse_time(other["actual_takeoff"]), parse_time(other["actual_landing"]), takeoff, landing):
                self.store.mark_contested(conn, item["id"])  # 冲突的实际起降两版都保留，等审核员裁决
                return
        verdict = adjudication.classify_flight(parse_time(item["expected_start"]), parse_time(item["expected_end"]), takeoff, landing)
        if verdict["verdict"] == "matched": self.store.auto_reconcile(conn, item["id"])
        else: self.store.mark_deviation(conn, item["id"], verdict["reasons"])

    # ---------------- 裁决（先写先得） ----------------
    def adjudicate(self, actor: str, role: str, batch_id: int, body: dict[str, Any]) -> dict[str, Any]:
        if role != "auditor": raise ApiError(403, "adjudicate_forbidden", "只有监管审核员可以提交裁决")
        sheet_offline_id = str(body.get("sheet_offline_id", "")).strip()
        entries = body.get("entries")
        if not sheet_offline_id or not isinstance(entries, list) or not entries:
            raise ApiError(400, "invalid_sheet", "sheet_offline_id 和非空 entries 必填")
        prior = self.store.sheet_by_offline_id(self.store.conn, sheet_offline_id)
        if prior: return {"batch_id": batch_id, "idempotent": True, "results": json.loads(prior["result_json"])}
        try:
            with self.repo.tx() as conn:
                batch = self.store.batch_by_id(conn, batch_id)
                if not batch: raise ApiError(404, "batch_not_found", "抽查批次不存在")
                results = []
                for entry in entries:
                    if not isinstance(entry, dict):
                        results.append({"result": "error", "error": "invalid_entry"}); continue
                    results.append(self._conclude_one(conn, batch, actor, sheet_offline_id, entry))
                self.store.insert_sheet(conn, batch_id, actor, sheet_offline_id, json.dumps(results, ensure_ascii=False))
                return {"batch_id": batch_id, "idempotent": False, "results": results}
        except sqlite3.IntegrityError:  # 同一裁决单并发重放：返回首次落库的结果
            prior = self.store.sheet_by_offline_id(self.store.conn, sheet_offline_id)
            if prior: return {"batch_id": batch_id, "idempotent": True, "results": json.loads(prior["result_json"])}
            raise

    def _conclude_one(self, conn: sqlite3.Connection, batch: sqlite3.Row, actor: str,
                      sheet_offline_id: str, entry: dict[str, Any]) -> dict[str, Any]:
        item_id = entry.get("item_id")
        item = self.store.item_by_id(conn, item_id) if isinstance(item_id, int) else None
        if not item or item["batch_id"] != batch["id"]:
            return {"item_id": item_id, "result": "error", "error": "item_not_found"}
        if item["status"] == "reconciled":  # 先写入的一方生效，后到的人看到当前结论
            return {"item_id": item_id, "result": "conflict", "current": self._conclusion_of(item)}
        verdict = entry.get("verdict")
        if verdict not in {"cleared", "violation"}:
            return {"item_id": item_id, "result": "error", "error": "invalid_verdict", "message": "verdict 必须是 cleared 或 violation"}
        accept_id = entry.get("accept_receipt_id")
        if item["status"] == "contested":
            candidates = {r["id"] for r in self.store.receipts_for_item(conn, item["id"]) if r["kind"] == "flight"}
            if accept_id not in candidates:
                return {"item_id": item_id, "result": "error", "error": "accept_receipt_required", "candidates": sorted(candidates)}
        note = str(entry.get("note", "")).strip()
        self.store.conclude_item(conn, item["id"], verdict, note, actor, accept_id if isinstance(accept_id, int) else None, sheet_offline_id)
        if isinstance(accept_id, int): self.store.settle_receipt_standings(conn, item["id"], accept_id)
        deduction = 0
        if verdict == "violation":
            deduction = adjudication.points_for(item["finding"])
            self.store.insert_deduction(conn, item["id"], item["operator_id"], deduction,
                                        f"批次 {batch['code']} 核销违规（{item['finding']}）", f"item:{item['id']}:conclusion")
            self.store.notify_dedupe(conn, item["plan_id"], "deduction",
                                     f"计划 {item['callsign']} 在批次 {batch['code']} 核销记违规，扣 {deduction} 分", f"item:{item['id']}:deduction")
        else:
            self.store.notify_dedupe(conn, item["plan_id"], "concluded",
                                     f"计划 {item['callsign']} 在批次 {batch['code']} 已核销，无违规", f"item:{item['id']}:concluded")
        self.repo.audit(conn, item["plan_id"], actor, "auditor", "item_concluded",
                        {"item_id": item["id"], "verdict": verdict, "accept_receipt_id": accept_id, "deduction": deduction})
        return {"item_id": item_id, "result": "concluded",
                "conclusion": self._conclusion_of(self.store.item_by_id(conn, item["id"])), "deduction": deduction}

    @staticmethod
    def _conclusion_of(item: sqlite3.Row) -> dict[str, Any]:
        return {"conclusion_kind": item["conclusion_kind"], "conclusion_note": item["conclusion_note"],
                "concluded_by": item["concluded_by"], "concluded_at": item["concluded_at"],
                "accepted_receipt_id": item["accepted_receipt_id"], "sheet_offline_id": item["sheet_offline_id"]}

    # ---------------- 协调台 ----------------
    def console(self, role: str, operator: str) -> dict[str, Any]:
        if role == "operator": rows = self.store.console_items(self.store.conn, operator)
        elif role in REVIEW_ROLES: rows = self.store.console_items(self.store.conn)
        else: raise ApiError(403, "console_forbidden", "当前角色不能查看协调台")
        now = iso()
        buckets: dict[str, list[dict[str, Any]]] = {"pending_review": [], "reconciled": [], "evidence_deadlines": [], "awaiting_receipts": []}
        for row in rows:
            item, status = dict(row), row["status"]
            if status == "invalidated":
                item["overdue"] = bool(item["evidence_deadline"] and item["evidence_deadline"] < now)
                buckets["evidence_deadlines"].append(item)
            elif status == "reconciled": buckets["reconciled"].append(item)
            elif status == "pending": buckets["awaiting_receipts"].append(item)
            else: buckets["pending_review"].append(item)
        return {"role": role, "server_time": now, "summary": {k: len(v) for k, v in buckets.items()}, **buckets}

    def item_detail(self, role: str, operator: str, item_id: int) -> dict[str, Any]:
        item = self.store.item_by_id(self.store.conn, item_id)
        if not item: raise ApiError(404, "item_not_found", "对账条目不存在")
        if role == "operator" and item["operator_id"] != operator: raise ApiError(403, "forbidden_operator", "不能查看其他运营方的条目")
        if role != "operator" and role not in REVIEW_ROLES: raise ApiError(403, "console_forbidden", "当前角色不能查看对账条目")
        return {"item": dict(item), "batch": dict(self.store.batch_by_id(self.store.conn, item["batch_id"])),
                "receipts": [dict(r) for r in self.store.receipts_for_item(self.store.conn, item_id)],
                "deductions": [dict(r) for r in self.store.deductions_for_item(self.store.conn, item_id)]}

    # ---------------- 逾期处理（可重复执行，不重复扣分或发通知） ----------------
    def sweep(self, actor: str, role: str) -> dict[str, Any]:
        if role not in {"auditor", "commander"}: raise ApiError(403, "sweep_forbidden", "只有监管审核员或指挥官可以执行逾期处理")
        now = iso()
        with self.repo.tx() as conn:
            no_show = self.store.pending_past_receipt_deadline(conn, now)
            for item in no_show:
                self.store.mark_no_show(conn, item["id"])
                self.store.notify_dedupe(conn, item["plan_id"], "no_show",
                                         f"批次 {item['batch_code']}：计划 {item['callsign']} 逾期未回传回执，记为未执行待复核",
                                         f"item:{item['id']}:no_show")
                self.repo.audit(conn, item["plan_id"], actor, role, "receipt_overdue", {"item_id": item["id"]})
            overdue = self.store.invalidated_past_evidence_deadline(conn, now)
            for item in overdue:
                self.store.notify_dedupe(conn, item["plan_id"], "evidence_overdue",
                                         f"批次 {item['batch_code']}：计划 {item['callsign']} 补证期限已过，仍未补证",
                                         f"item:{item['id']}:evidence_overdue")
        return {"no_show": len(no_show), "evidence_reminders": len(overdue)}

    # ---------------- 级联钩子（在 app 主流程的事务里调用） ----------------
    def on_plan_changed(self, conn: sqlite3.Connection, plan: sqlite3.Row, cause: str) -> None:
        self._invalidate_items(conn, plan["id"], plan["revision"], cause)

    def invalidate_for_restriction(self, conn: sqlite3.Connection, restriction: sqlite3.Row) -> None:
        if restriction["status"] != "active": return
        rbox = (restriction["min_lon"], restriction["min_lat"], restriction["max_lon"], restriction["max_lat"])
        r_start, r_end = parse_time(restriction["starts_at"]), parse_time(restriction["ends_at"])
        for plan in conn.execute("SELECT * FROM flight_plans WHERE status='approved'").fetchall():
            if not boxes_overlap(route_bbox(json.loads(plan["route_json"])), rbox): continue
            if not times_overlap(parse_time(plan["starts_at"]), parse_time(plan["ends_at"]), r_start, r_end): continue
            if not (plan["max_altitude"] > restriction["min_altitude"] and restriction["max_altitude"] > 0): continue
            conn.execute("UPDATE flight_plans SET status='draft', updated_at=? WHERE id=?", (iso(), plan["id"]))
            approval = conn.execute("SELECT id FROM approvals WHERE plan_id=? AND plan_revision=? AND decision='approved' ORDER BY id DESC LIMIT 1",
                                    (plan["id"], plan["revision"])).fetchone()
            approval_id = approval["id"] if approval else 0
            self.repo.audit(conn, plan["id"], "system", "system", "approval_invalidated",
                            {"cause": "restriction_changed", "restriction_id": restriction["id"], "approval_id": approval_id})
            # 去重键锚定批准实例：同一批准被级联重试只通知一次，重新批准后的再次失效仍会通知
            self.store.notify_dedupe(conn, plan["id"], "approval_invalidated",
                                     f"飞行计划 {plan['callsign']} 因空域限制「{restriction['name']}」变化，原批准失效",
                                     f"approval:{approval_id}:restriction_changed")
            self._invalidate_items(conn, plan["id"], plan["revision"], "restriction_changed")

    def _invalidate_items(self, conn: sqlite3.Connection, plan_id: int, plan_revision: int, cause: str) -> None:
        for item in self.store.open_items_for_plan_revision(conn, plan_id, plan_revision):
            batch = self.store.batch_by_id(conn, item["batch_id"])
            deadline = iso(utcnow() + timedelta(hours=batch["evidence_hours"]))
            self.store.invalidate_item(conn, item["id"], deadline)
            self.store.notify_dedupe(conn, plan_id, "evidence_required",
                                     f"监管批次 {batch['code']}：计划 {item['callsign']} 原批准失效（{cause}），请在 {deadline} 前补证",
                                     f"item:{item['id']}:invalidated")
            self.repo.audit(conn, plan_id, "system", "system", "recon_item_invalidated", {"item_id": item["id"], "cause": cause})
