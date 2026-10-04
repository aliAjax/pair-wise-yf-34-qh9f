"""Judgment layer: pure reconciliation decision logic.

Given a receipt and the current state of a plan, its approval and the
airspace restrictions, the judge returns a :class:`Verdict` describing what
the system should conclude. It never writes to the database; the service
layer applies the side effects. This keeps "判定" independent from "存储".
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from . import util
from .util import boxes_overlap, iso, parse_time, route_bbox, times_overlap

# Conclusion statuses -------------------------------------------------------
VERIFIED = "verified"            # 已核销: actual flight matches a valid approval
INVALIDATED = "invalidated"      # 批准失效: plan or restriction changed after approval
MISMATCH = "mismatch"            # 不一致: actual flight deviates from the approved plan
CONFLICT = "conflict"            # 冲突: two versions of actuals for one plan, pending adjudication
PENDING = "pending"              # 待复核: no approvable revision on record

# Coarse deduction points for an actual flight that contradicts the approval.
MISMATCH_POINTS = 2
# Operators get this many hours to supplement evidence for an invalidated/mismatch.
SUPPLEMENT_HOURS = 72


@dataclass
class Verdict:
    status: str
    reason: str
    approval_id: int | None = None
    deduction_points: int | None = None
    deduction_reason: str | None = None
    supplement_hours: int | None = None
    notify_kind: str | None = None
    notify_message: str | None = None
    versions: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reason": self.reason,
            "approval_id": self.approval_id,
            "deduction_points": self.deduction_points,
            "deduction_reason": self.deduction_reason,
            "supplement_hours": self.supplement_hours,
            "notify_kind": self.notify_kind,
            "notify_message": self.notify_message,
            "versions": self.versions,
        }


def _receipt_summary(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "receipt_id": row["id"],
        "offline_id": row["offline_id"],
        "actual_takeoff_at": row["actual_takeoff_at"],
        "actual_landing_at": row["actual_landing_at"],
        "result": row["result"],
        "received_at": row["received_at"],
    }


def _conflicts_with(a: sqlite3.Row, b: sqlite3.Row) -> bool:
    """Two receipts conflict when they report different actuals for the same plan."""
    if a["result"] != b["result"]:
        return True
    for column in ("actual_takeoff_at", "actual_landing_at"):
        av, bv = a[column], b[column]
        if av and bv and av != bv:
            return True
    return False


class ReconJudge:
    """Decides a verdict for one receipt against the current record."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def judge(self, receipt: sqlite3.Row) -> Verdict:
        plan = self.conn.execute("SELECT * FROM flight_plans WHERE id=?", (receipt["plan_id"],)).fetchone()
        if not plan:
            return Verdict(PENDING, "回执对应的飞行计划不存在")

        # 1. Approval must exist for the exact revision the receipt landed on.
        approval = self.conn.execute(
            "SELECT * FROM approvals WHERE plan_id=? AND plan_revision=? AND decision='approved' ORDER BY id DESC LIMIT 1",
            (plan["id"], receipt["plan_revision"]),
        ).fetchone()
        if not approval:
            return Verdict(
                PENDING,
                f"回执对应计划版本 v{receipt['plan_revision']} 没有有效批准记录",
                notify_kind="recon_pending",
                notify_message=f"飞行计划 {plan['callsign']} 回执待复核：对应版本无批准记录",
            )

        # 2. Plan changed after the approved revision -> original approval invalid.
        if plan["revision"] != receipt["plan_revision"]:
            return Verdict(
                INVALIDATED,
                f"计划已从 v{receipt['plan_revision']} 变更为 v{plan['revision']}，原批准失效",
                approval_id=approval["id"],
                supplement_hours=SUPPLEMENT_HOURS,
                notify_kind="approval_invalidated",
                notify_message=f"飞行计划 {plan['callsign']} 已变更，原批准失效，请重新申请或补证",
            )

        # 3. A restriction created/updated after the approval now overlaps the plan.
        restriction = self._restriction_changed_since(plan, approval)
        if restriction:
            return Verdict(
                INVALIDATED,
                f"空域限制「{restriction['name']}」在批准后新增或变更，原批准失效",
                approval_id=approval["id"],
                supplement_hours=SUPPLEMENT_HOURS,
                notify_kind="approval_invalidated",
                notify_message=f"飞行计划 {plan['callsign']} 批准后空域限制变化，原批准失效，请重新申请或补证",
            )

        # 4. Conflicting actuals for the same plan -> keep two versions for adjudication.
        conflict = self._conflicting_receipt(plan, receipt)
        if conflict:
            return Verdict(
                CONFLICT,
                f"同一计划存在冲突的实际起降（回执 {conflict['offline_id']} 与 {receipt['offline_id']}），留两版待裁决",
                approval_id=approval["id"],
                notify_kind="recon_conflict",
                notify_message=f"飞行计划 {plan['callsign']} 实际起降冲突，已保留两版待审核员裁决",
                versions=[_receipt_summary(conflict), _receipt_summary(receipt)],
            )

        # 5. Otherwise the actual flight must match the approved window/result.
        deviation = self._actual_deviation(plan, receipt)
        if deviation:
            return Verdict(
                MISMATCH,
                deviation,
                approval_id=approval["id"],
                deduction_points=MISMATCH_POINTS,
                deduction_reason=deviation,
                supplement_hours=SUPPLEMENT_HOURS,
                notify_kind="recon_mismatch",
                notify_message=f"飞行计划 {plan['callsign']} 实际飞行与批准结论不一致：{deviation}",
            )

        return Verdict(
            VERIFIED,
            "实际起降与批准结论一致，已核销",
            approval_id=approval["id"],
        )

    def _restriction_changed_since(self, plan: sqlite3.Row, approval: sqlite3.Row) -> sqlite3.Row | None:
        recorded = self._recorded_restrictions(approval)
        current = self._current_restrictions(plan)
        if recorded is not None:
            # Deterministic: a restriction is new or its revision bumped since approval.
            for rid, restriction in current.items():
                if rid not in recorded or recorded[rid] != restriction["revision"]:
                    return restriction
            return None
        # Fallback for approvals written before restriction snapshots were recorded.
        approval_time = parse_time(approval["created_at"])
        for restriction in current.values():
            created_after = parse_time(restriction["created_at"]) > approval_time
            updated_after = bool(restriction["revision"] > 1 and restriction["updated_at"] and parse_time(restriction["updated_at"]) > approval_time)
            if created_after or updated_after:
                return restriction
        return None

    def _recorded_restrictions(self, approval: sqlite3.Row) -> dict[str, int] | None:
        raw = approval["restrictions_json"] if "restrictions_json" in approval.keys() else None
        if not raw:
            return None
        try:
            return {str(k): int(v) for k, v in json.loads(raw).items()}
        except (ValueError, TypeError):
            return None

    def _current_restrictions(self, plan: sqlite3.Row) -> dict[str, sqlite3.Row]:
        route = json.loads(plan["route_json"])
        bbox = route_bbox(route)
        start, end = parse_time(plan["starts_at"]), parse_time(plan["ends_at"])
        current: dict[str, sqlite3.Row] = {}
        for restriction in self.conn.execute("SELECT * FROM restrictions WHERE status='active'"):
            rbox = (restriction["min_lon"], restriction["min_lat"], restriction["max_lon"], restriction["max_lat"])
            if not boxes_overlap(bbox, rbox):
                continue
            if not times_overlap(start, end, parse_time(restriction["starts_at"]), parse_time(restriction["ends_at"])):
                continue
            altitude_overlap = plan["max_altitude"] > restriction["min_altitude"] and restriction["max_altitude"] > 0
            if not altitude_overlap:
                continue
            current[str(restriction["id"])] = restriction
        return current

    def _conflicting_receipt(self, plan: sqlite3.Row, receipt: sqlite3.Row) -> sqlite3.Row | None:
        others = self.conn.execute(
            "SELECT * FROM receipts WHERE plan_id=? AND id!=? ORDER BY id",
            (plan["id"], receipt["id"]),
        )
        for other in others:
            if _conflicts_with(receipt, other):
                return other
        return None

    @staticmethod
    def _actual_deviation(plan: sqlite3.Row, receipt: sqlite3.Row) -> str | None:
        result = receipt["result"]
        if result == "cancelled":
            return "实际飞行取消，与已批准结论不一致"
        if result == "diverted":
            return "实际飞行备降，与已批准结论不一致"
        if result != "completed":
            return f"实际飞行结果 {result} 无法与批准结论核对"
        start, end = parse_time(plan["starts_at"]), parse_time(plan["ends_at"])
        if receipt["actual_takeoff_at"] and parse_time(receipt["actual_takeoff_at"]) < start:
            return "实际起飞时间早于批准窗口"
        if receipt["actual_landing_at"] and parse_time(receipt["actual_landing_at"]) > end:
            return "实际降落时间晚于批准窗口"
        return None
