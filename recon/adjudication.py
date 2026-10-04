"""判定：回执分类、冲突检测与扣分规则。纯函数，不触碰存储与网络。"""
from __future__ import annotations

from datetime import datetime, timedelta

TOLERANCE = timedelta(minutes=5)          # 实际起降相对批准窗口的容差
CONFLICT_EPSILON = timedelta(seconds=60)  # 两条回执的实际起降差超过该值即视为冲突版本
POINTS = {"deviation": 2, "contested": 4, "no_show": 3, "invalidated": 1}  # 违规扣分按发现类型定档


def classify_flight(expected_start: datetime, expected_end: datetime,
                    actual_takeoff: datetime, actual_landing: datetime) -> dict:
    """把一条实际飞行回执与批次锁定的批准窗口比对，得出 matched / deviation。"""
    reasons: list[str] = []
    if actual_landing <= actual_takeoff: reasons.append("landing_not_after_takeoff")
    if actual_takeoff < expected_start - TOLERANCE: reasons.append("takeoff_before_window")
    if actual_takeoff > expected_end + TOLERANCE: reasons.append("takeoff_after_window")
    if actual_landing > expected_end + TOLERANCE: reasons.append("landing_after_window")
    return {"verdict": "matched" if not reasons else "deviation", "reasons": reasons}


def flights_conflict(a_takeoff: datetime, a_landing: datetime,
                     b_takeoff: datetime, b_landing: datetime) -> bool:
    """两条回执的实际起降是否矛盾（同一计划不应有两版不同的实际起降）。"""
    return abs(a_takeoff - b_takeoff) > CONFLICT_EPSILON or abs(a_landing - b_landing) > CONFLICT_EPSILON


def points_for(finding: str | None) -> int:
    return POINTS.get(finding or "", 1)
