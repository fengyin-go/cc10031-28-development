"""代理切换与处理项蛇专用路由。

资源路由 /api/{module} 只做通用 CRUD，代理切换的审批权限、
处理项蛇的受控只读口径放在这里，路由层只做参数与异常翻译，
业务判断全部在领域服务内。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.services.processing_snake import processing_snake
from app.services.proxy_switch import (
    MODE_MAIN_FIRST,
    SWITCH_MODES,
    proxy_switch,
)
from app.services.anti_block import AntiBlockService

router = APIRouter(prefix="/api", tags=["代理切换与处理项蛇"])


# ---------------------------------------------------------------------------
# 请求体
# ---------------------------------------------------------------------------

class ParseBlockPayload(BaseModel):
    anti_block_id: int
    switch_mode: str = MODE_MAIN_FIRST
    affected_node_ids: list[int] | None = None
    regions: list[str] | None = None


class CreatePlanPayload(BaseModel):
    anti_block_id: int
    owner_flow: str = Field(..., description="所属采集流程，与计划同时写入")
    switch_mode: str = MODE_MAIN_FIRST
    affected_node_ids: list[int] | None = None
    regions: list[str] | None = None
    snake_item_key: str | None = None
    source: str = "anti_block"


class OperatorPayload(BaseModel):
    operator_flow: str = Field(..., description="执行操作的采集流程，用于归属校验")
    reason: str | None = None


class SnakeImportPayload(BaseModel):
    item_key: str
    source_module: str
    data: dict[str, Any] = Field(default_factory=dict)
    business_summary: dict[str, Any] = Field(default_factory=dict)


def _translate(exc: Exception) -> HTTPException:
    if isinstance(exc, PermissionError):
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, KeyError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, ValueError):
        return HTTPException(status_code=400, detail=str(exc))
    raise exc


# ---------------------------------------------------------------------------
# 封锁触发：反封锁只标记需要切换
# ---------------------------------------------------------------------------

@router.post("/anti-block/{entry_id}/trigger-block")
def trigger_block(entry_id: int) -> dict[str, Any]:
    entry, message = AntiBlockService().trigger_block(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=message)
    return {"ok": True, "message": message, "entry": entry}


# ---------------------------------------------------------------------------
# 封锁事件解析（只读，不落库）
# ---------------------------------------------------------------------------

@router.post("/proxy-switch/parse-block")
def parse_block_event(payload: ParseBlockPayload) -> dict[str, Any]:
    if payload.switch_mode not in SWITCH_MODES:
        raise HTTPException(status_code=400, detail=f"不支持的切换口径：{payload.switch_mode}")
    try:
        event = proxy_switch.parse_block_event(
            payload.anti_block_id,
            affected_node_ids=payload.affected_node_ids,
            regions=payload.regions,
            switch_mode=payload.switch_mode,
        )
    except (KeyError, ValueError) as exc:
        raise _translate(exc)
    return {"ok": True, "event": event}


# ---------------------------------------------------------------------------
# 代理切换计划：生成、确认、撤销
# ---------------------------------------------------------------------------

@router.get("/proxy-switch/plans")
def list_switch_plans(
    owner_flow: str | None = Query(default=None),
    node_id: int | None = Query(default=None),
    region: str | None = Query(default=None),
    active_only: bool = Query(default=False),
    status: str | None = Query(default=None),
) -> dict[str, Any]:
    items = proxy_switch.list_plans(
        owner_flow=owner_flow, node_id=node_id, region=region,
        active_only=active_only, status=status,
    )
    return {"items": items, "total": len(items)}


@router.get("/proxy-switch/plans/{plan_id}")
def get_switch_plan(plan_id: int) -> dict[str, Any]:
    plan = proxy_switch.get_plan(plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail=f"代理切换计划 {plan_id} 不存在")
    return plan


@router.post("/proxy-switch/plans")
def create_switch_plan(payload: CreatePlanPayload) -> dict[str, Any]:
    try:
        plan = proxy_switch.create_plan(
            payload.anti_block_id,
            payload.owner_flow,
            switch_mode=payload.switch_mode,
            affected_node_ids=payload.affected_node_ids,
            regions=payload.regions,
            snake_item_key=payload.snake_item_key,
            source=payload.source,
        )
    except (KeyError, ValueError) as exc:
        raise _translate(exc)
    return {"ok": True, "message": "代理切换计划已生成，等待所属采集流程确认", "plan": plan}


@router.post("/proxy-switch/plans/{plan_id}/confirm")
def confirm_switch_plan(plan_id: int, payload: OperatorPayload) -> dict[str, Any]:
    try:
        plan = proxy_switch.confirm_plan(plan_id, payload.operator_flow)
    except (KeyError, ValueError, PermissionError) as exc:
        raise _translate(exc)
    return {
        "ok": True,
        "message": "节点切换已确认，防护策略已按处理项蛇版本更新并生成回执",
        "plan": plan,
        "receipt": plan["receipt"],
    }


@router.post("/proxy-switch/plans/{plan_id}/revoke")
def revoke_switch_plan(plan_id: int, payload: OperatorPayload) -> dict[str, Any]:
    try:
        plan = proxy_switch.revoke_plan(plan_id, payload.operator_flow, payload.reason or "")
    except (KeyError, ValueError, PermissionError) as exc:
        raise _translate(exc)
    return {"ok": True, "message": "切换计划已撤销，节点活动占用已释放", "plan": plan}


@router.get("/proxy-switch/nodes")
def node_ownership() -> dict[str, Any]:
    items = proxy_switch.node_ownership_view()
    return {"items": items, "total": len(items)}


@router.get("/proxy-switch/receipts")
def switch_receipts(owner_flow: str | None = Query(default=None)) -> dict[str, Any]:
    items = proxy_switch.list_receipts(owner_flow=owner_flow)
    return {"items": items, "total": len(items)}


@router.post("/proxy-switch/admin/reset")
def reset_switch_workbench() -> dict[str, Any]:
    """开发/联调清场：清空切换计划与处理项蛇内存状态（非业务审批动作）。"""
    proxy_switch._plans.clear()
    proxy_switch._seq = 0
    proxy_switch._receipt_seq = 0
    snake = processing_snake
    snake._records.clear()
    snake._links.clear()
    snake._todos.clear()
    snake._item_seq = 0
    snake._link_seq = 0
    snake._todo_seq = 0
    snake._result_seq = 0
    return {"ok": True, "message": "代理切换工作台与处理项蛇内存状态已清空"}


# ---------------------------------------------------------------------------
# 处理项蛇：受控只读查询 + 唯一的导入入口（重复导入只认第一条）
# ---------------------------------------------------------------------------

@router.post("/processing-snake/import")
def import_snake_record(payload: SnakeImportPayload) -> dict[str, Any]:
    record, created = processing_snake.import_record(
        payload.item_key, payload.source_module, payload.data, payload.business_summary,
    )
    return {
        "ok": True,
        "message": "处理项已导入" if created else "重复导入，已忽略并返回首条记录",
        "created": created,
        "record": record,
    }


@router.get("/processing-snake/records")
def list_snake_records() -> dict[str, Any]:
    items = processing_snake.list_records()
    return {"items": items, "total": len(items)}


@router.get("/processing-snake/records/{item_key}")
def get_snake_record(item_key: str) -> dict[str, Any]:
    try:
        return processing_snake.get_record(item_key)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.get("/processing-snake/chain")
def snake_chain(item_key: str | None = Query(default=None)) -> dict[str, Any]:
    items = processing_snake.list_chain(item_key)
    return {"items": items, "total": len(items)}


@router.get("/processing-snake/todos")
def snake_todos(item_key: str | None = Query(default=None)) -> dict[str, Any]:
    items = processing_snake.list_todos(item_key)
    return {"items": items, "total": len(items)}


@router.get("/processing-snake/reconcile")
def snake_reconcile() -> dict[str, Any]:
    return processing_snake.reconcile()
