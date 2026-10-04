"""接口入口：/api/recon/* 路由分发，只做路径解析与参数转换，业务编排都在 service.py。"""
from __future__ import annotations

from typing import Any

from common import ApiError


def handle_get(service: Any, path: str, actor: str, role: str, operator: str) -> tuple[int, Any]:
    if path == "/api/recon/console": return 200, service.console(role, operator)
    if path == "/api/recon/batches": return 200, service.list_batches(role, operator)
    parts = [p for p in path.split("/") if p]
    if len(parts) == 4 and parts[:3] == ["api", "recon", "batches"] and parts[3].isdigit():
        return 200, service.batch_detail(role, operator, int(parts[3]))
    if len(parts) == 4 and parts[:3] == ["api", "recon", "items"] and parts[3].isdigit():
        return 200, service.item_detail(role, operator, int(parts[3]))
    raise ApiError(404, "not_found", "接口不存在")


def handle_post(service: Any, path: str, actor: str, role: str, operator: str, body: dict) -> tuple[int, Any]:
    if path == "/api/recon/batches": return 201, service.create_batch(actor, role, body)
    if path == "/api/recon/sweep": return 200, service.sweep(actor, role)
    parts = [p for p in path.split("/") if p]
    if len(parts) == 5 and parts[:3] == ["api", "recon", "batches"] and parts[3].isdigit():
        if parts[4] == "receipts": return 200, service.ingest_receipts(actor, role, operator, int(parts[3]), body)
        if parts[4] == "adjudicate": return 200, service.adjudicate(actor, role, int(parts[3]), body)
    raise ApiError(404, "not_found", "接口不存在")
