"""代理切换数据能力 — 把反封锁的「需要切换」标记落成可审批的代理切换计划。

数据来源（只读消费三个模块）：
- proxy_router 代理节点数据：节点地域、在线状态、主备归属
- rate_limiter 频控规则数据：目标站点的频控口径快照
- anti_block 新到达的防护策略数据：封锁事件、目标站点、IP代理池

关键约束：
1. 反封锁模块只标记「需要切换」，不直接动节点；本服务生成带流程归属的计划。
2. 计划与流程归属同时写入：owner_flow 是所属采集流程，只有它能确认/撤销，
   其他流程只读查看节点归属和回执（PermissionError 拦截写操作）。
3. 同一节点同一时间只能有一个活动（待确认）计划；多节点计划在创建前整体校验，
   任一节点冲突则整单不落地。
4. 主节点和备用节点同时被封锁时支持两种口径：主备一起切换 / 先切主并观察备；
   两种口径下同一节点都只会出现在至多一个活动计划里。
5. 确认时按处理项蛇版本更新防护策略并生成切换回执，全程留痕可核对。
"""
from __future__ import annotations

import copy
import threading
from datetime import datetime, timezone
from typing import Any

from app.services.processing_snake import processing_snake
from app.store import store

# 计划状态
PLAN_PENDING = "待确认"
PLAN_CONFIRMED = "已确认"
PLAN_REVOKED = "已撤销"
ACTIVE_STATUSES = {PLAN_PENDING}
TERMINAL_STATUSES = {PLAN_CONFIRMED, PLAN_REVOKED}

# 切换口径
MODE_TOGETHER = "主备一起切换"
MODE_MAIN_FIRST = "先切主并观察备"
SWITCH_MODES = {MODE_TOGETHER, MODE_MAIN_FIRST}

# 节点角色
ROLE_MAIN = "主节点"
ROLE_BACKUP = "备用节点"

ACTION_SWITCH = "切换"
ACTION_OBSERVE = "观察"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ProxySwitchService:
    """代理切换计划领域服务：不继承 BaseModuleService，是跨模块消费/审批服务。"""

    SERVICE_NAME = "proxy_switch"

    def __init__(self, snake: ProcessingSnakeService | None = None) -> None:
        self._lock = threading.RLock()
        self._seq = 0
        self._receipt_seq = 0
        self._plans: list[dict[str, Any]] = []
        # 默认挂全局处理项蛇单例，也允许注入独立实例做隔离测试
        self._snake = snake if snake is not None else processing_snake

    # ------------------------------------------------------------------
    # 三源数据读取
    # ------------------------------------------------------------------

    def read_proxy_nodes(self) -> list[dict[str, Any]]:
        """读取代理节点数据。"""
        return [
            {
                "node_id": row["id"],
                "node_name": row.get("节点名称"),
                "ip": row.get("IP地址"),
                "region": row.get("出口地域"),
                "protocol": row.get("协议类型"),
                "bandwidth": row.get("带宽上限"),
                "latency": row.get("延迟"),
                "status": row.get("status"),
                "usable": row.get("可用状态"),
            }
            for row in store.rows("proxy_router")
        ]

    def read_rate_limiter(self, site: str) -> list[dict[str, Any]]:
        """读取目标站点命中的频控规则数据。"""
        rules = store.rows("rate_limiter")
        return [
            {
                "rule_id": row["id"],
                "rule_name": row.get("规则名称"),
                "site": row.get("目标站点"),
                "per_second": row.get("每秒上限"),
                "per_day": row.get("每日上限"),
                "concurrency": row.get("并发上限"),
                "cooldown": row.get("冷却时间"),
                "priority": row.get("优先级"),
                "status": row.get("status"),
            }
            for row in rules
            if site and site in str(row.get("目标站点", ""))
        ]

    def read_protection(self, anti_block_id: int) -> dict[str, Any]:
        """读取新到达代理防护联动的防护策略数据。"""
        strategy = store.find("anti_block", anti_block_id)
        if strategy is None:
            raise KeyError(f"防护策略 {anti_block_id} 不存在")
        return {
            "anti_block_id": strategy["id"],
            "strategy_name": strategy.get("策略名称"),
            "site": strategy.get("目标站点"),
            "interval": strategy.get("请求间隔"),
            "ip_pool": strategy.get("IP代理池"),
            "ua_rotation": strategy.get("UA轮换"),
            "captcha": strategy.get("验证码处理"),
            "cookie_policy": strategy.get("Cookie策略"),
            "status": strategy.get("status"),
            "switch_marker": strategy.get("proxy_switch_marker"),
        }

    # ------------------------------------------------------------------
    # 封锁事件解析：地域 + 节点归属 + 主备角色
    # ------------------------------------------------------------------

    @staticmethod
    def _regions_from_pool(ip_pool: Any) -> list[str]:
        """从防护策略的 IP代理池 文本解析地域清单。"""
        if not ip_pool:
            return []
        text = str(ip_pool)
        for sep in ("，", "、", ";", "；", "\n", "\t", " "):
            text = text.replace(sep, ",")
        return [part.strip() for part in text.split(",") if part.strip()]

    def _resolve_affected_nodes(
        self,
        node_ids: list[int] | None,
        regions: list[str] | None,
        pool_regions: list[str],
    ) -> list[dict[str, Any]]:
        nodes = self.read_proxy_nodes()
        if node_ids:
            found = [n for n in nodes if n["node_id"] in set(node_ids)]
            missing = set(node_ids) - {n["node_id"] for n in found}
            if missing:
                raise ValueError(f"代理节点不存在：{sorted(missing)}")
            return found

        region_pool = [r for r in (regions or []) + pool_regions if r]
        if region_pool:
            matched = [
                n for n in nodes
                if any(r == n["region"] or r in str(n["region"]) for r in region_pool)
            ]
            if matched:
                return matched

        # 兜底：封锁首先体现在异常节点上
        unhealthy = [n for n in nodes if n["status"] in ("离线", "限速中", "已禁用")]
        if unhealthy:
            return unhealthy
        raise ValueError("封锁事件未解析到任何受影响代理节点")

    @staticmethod
    def _assign_roles(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """按地域推断主备归属：同地域内在线优先、节点编号小的为主，其次为备。"""
        result: list[dict[str, Any]] = []
        regions: dict[str, list[dict[str, Any]]] = {}
        for node in nodes:
            regions.setdefault(str(node.get("region") or "未知地域"), []).append(node)
        for region, members in regions.items():
            ordered = sorted(members, key=lambda n: (n["status"] != "在线", n["node_id"]))
            for index, node in enumerate(ordered):
                enriched = dict(node)
                enriched["region"] = region
                enriched["role"] = ROLE_MAIN if index == 0 else ROLE_BACKUP
                result.append(enriched)
        return sorted(result, key=lambda n: (str(n["region"]), n["node_id"]))

    def parse_block_event(
        self,
        anti_block_id: int,
        *,
        affected_node_ids: list[int] | None = None,
        regions: list[str] | None = None,
        switch_mode: str = MODE_MAIN_FIRST,
    ) -> dict[str, Any]:
        """解析封锁事件影响的地域与节点归属，返回可用于生成计划的事件数据（不落库）。"""
        if switch_mode not in SWITCH_MODES:
            raise ValueError(f"不支持的切换口径：{switch_mode}")
        protection = self.read_protection(anti_block_id)
        if protection["status"] != "触发封锁":
            raise ValueError(
                f"防护策略 {anti_block_id} 当前状态为「{protection['status']}」，未触发封锁，无需切换"
            )
        site = str(protection["site"])
        pool_regions = self._regions_from_pool(protection["ip_pool"])
        nodes = self._resolve_affected_nodes(affected_node_ids, regions, pool_regions)
        nodes = self._assign_roles(nodes)

        for node in nodes:
            if switch_mode == MODE_TOGETHER or node["role"] == ROLE_MAIN:
                node["action"] = ACTION_SWITCH
            else:
                node["action"] = ACTION_OBSERVE

        return {
            "anti_block_id": anti_block_id,
            "strategy_name": protection["strategy_name"],
            "site": site,
            "regions": sorted({n["region"] for n in nodes}),
            "pool_regions": pool_regions,
            "switch_mode": switch_mode,
            "affected_nodes": nodes,
            "target_node_ids": [n["node_id"] for n in nodes if n["action"] == ACTION_SWITCH],
            "observe_node_ids": [n["node_id"] for n in nodes if n["action"] == ACTION_OBSERVE],
            "rate_limiter_snapshot": self.read_rate_limiter(site),
            "protection_snapshot": protection,
        }

    # ------------------------------------------------------------------
    # 活动计划约束：同一节点同一时间只能有一个活动计划
    # ------------------------------------------------------------------

    def active_plan_of_node(self, node_id: int) -> dict[str, Any] | None:
        for plan in self._plans:
            if plan["status"] not in ACTIVE_STATUSES:
                continue
            if any(n["node_id"] == node_id for n in plan["affected_nodes"]):
                return plan
        return None

    def _assert_nodes_free(self, node_ids: list[int]) -> None:
        conflicts: list[dict[str, Any]] = []
        for node_id in node_ids:
            holder = self.active_plan_of_node(node_id)
            if holder is not None:
                conflicts.append({"node_id": node_id, "plan_id": holder["id"], "plan_no": holder["plan_no"]})
        if conflicts:
            raise ValueError(f"节点已存在活动切换计划，同一节点同一时间只能有一个活动计划：{conflicts}")

    # ------------------------------------------------------------------
    # 计划生成：计划与流程归属同时写入
    # ------------------------------------------------------------------

    def create_plan(
        self,
        anti_block_id: int,
        owner_flow: str,
        *,
        switch_mode: str = MODE_MAIN_FIRST,
        affected_node_ids: list[int] | None = None,
        regions: list[str] | None = None,
        snake_item_key: str | None = None,
        source: str = "anti_block",
    ) -> dict[str, Any]:
        if not str(owner_flow or "").strip():
            raise ValueError("代理切换计划必须同时写入所属采集流程（owner_flow）")
        event = self.parse_block_event(
            anti_block_id,
            affected_node_ids=affected_node_ids,
            regions=regions,
            switch_mode=switch_mode,
        )
        with self._lock:
            # 先整体校验冲突，任一节点被占用则整单不落地
            self._assert_nodes_free(
                [n["node_id"] for n in event["affected_nodes"]]
            )

            snake_version: int | None = None
            todo_id: int | None = None
            if snake_item_key is not None:
                snake_record = self._snake.get_record(snake_item_key)
                link = self._snake.append_chain(
                    snake_item_key,
                    self.SERVICE_NAME,
                    "生成代理切换计划",
                    payload={
                        "anti_block_id": anti_block_id,
                        "site": event["site"],
                        "switch_mode": switch_mode,
                        "target_node_ids": event["target_node_ids"],
                    },
                    todo_title=f"确认 {event['site']} 代理切换计划",
                )
                todo_id = link.get("todo_id")
                snake_version = snake_record["version"] + 1

            self._seq += 1
            now = _now()
            plan = {
                "id": self._seq,
                "plan_no": f"SW-{1000 + self._seq}",
                "status": PLAN_PENDING,
                "active": True,
                "owner_flow": owner_flow.strip(),
                "source": source,
                "anti_block_id": anti_block_id,
                "strategy_name": event["strategy_name"],
                "site": event["site"],
                "affected_regions": event["regions"],
                "switch_mode": switch_mode,
                "affected_nodes": event["affected_nodes"],
                "target_node_ids": event["target_node_ids"],
                "observe_node_ids": event["observe_node_ids"],
                "rate_limiter_snapshot": event["rate_limiter_snapshot"],
                "protection_snapshot": event["protection_snapshot"],
                "block_event": {
                    "anti_block_id": anti_block_id,
                    "site": event["site"],
                    "regions": event["regions"],
                    "pool_regions": event["pool_regions"],
                },
                "snake_item_key": snake_item_key,
                "snake_version": snake_version,
                "snake_todo_id": todo_id,
                "receipt": None,
                "created_at": now,
                "updated_at": now,
            }

            if snake_item_key is not None:
                self._snake.attach_proxy_switch_plan(snake_item_key, plan)

            self._plans.append(plan)
            store.channel.publish(
                topic="rpa.proxy_switch_planned",
                source=self.SERVICE_NAME,
                payload={
                    "plan_id": plan["id"], "plan_no": plan["plan_no"],
                    "owner_flow": plan["owner_flow"], "site": plan["site"],
                    "target_node_ids": plan["target_node_ids"],
                    "snake_version": snake_version,
                },
            )
            return copy.deepcopy(plan)

    # ------------------------------------------------------------------
    # 审批：只有所属采集流程可以确认/撤销，其他流程只读
    # ------------------------------------------------------------------

    def _require_active_plan(self, plan_id: int) -> dict[str, Any]:
        for plan in self._plans:
            if plan["id"] == plan_id:
                if plan["status"] not in ACTIVE_STATUSES:
                    raise ValueError(f"计划 {plan['plan_no']} 已处于终态「{plan['status']}」，不可再操作")
                return plan
        raise KeyError(f"代理切换计划 {plan_id} 不存在")

    @staticmethod
    def _assert_owner(plan: dict[str, Any], operator_flow: str) -> None:
        if operator_flow != plan["owner_flow"]:
            raise PermissionError(
                f"计划 {plan['plan_no']} 归属采集流程「{plan['owner_flow']}」，"
                f"「{operator_flow}」只有只读权限，不能确认或撤销"
            )

    def _close_snake_todo(self, plan: dict[str, Any]) -> None:
        if plan.get("snake_todo_id") is None:
            return
        for todo in self._snake.list_todos(plan["snake_item_key"]):
            if todo["todo_id"] == plan["snake_todo_id"] and todo["status"] == "待处理":
                self._snake.handle_todo(todo["todo_id"], plan["owner_flow"])

    def _update_protection_strategy(self, plan: dict[str, Any], state: str) -> None:
        """按所属流程更新防护策略：反封锁只标记需要切换，处理结果回写标记。"""
        strategy = store.find("anti_block", plan["anti_block_id"])
        if strategy is None:
            return
        strategy["proxy_switch_marker"] = {
            "needed": True,
            "state": state,
            "plan_id": plan["id"],
            "plan_no": plan["plan_no"],
            "owner_flow": plan["owner_flow"],
            "snake_version": plan.get("snake_version"),
            "switched_node_ids": plan["target_node_ids"],
            "observed_node_ids": plan["observe_node_ids"],
            "updated_at": _now(),
        }
        store.channel.publish(
            topic="rpa.protection_strategy_updated",
            source=plan["owner_flow"],
            payload={
                "anti_block_id": plan["anti_block_id"],
                "plan_id": plan["id"],
                "state": state,
                "operator_flow": plan["owner_flow"],
            },
        )

    def confirm_plan(self, plan_id: int, operator_flow: str) -> dict[str, Any]:
        """所属采集流程确认切换：更新防护策略并生成切换回执。"""
        with self._lock:
            plan = self._require_active_plan(plan_id)
            self._assert_owner(plan, operator_flow)

            plan["status"] = PLAN_CONFIRMED
            plan["active"] = False
            plan["updated_at"] = _now()
            self._update_protection_strategy(plan, "已切换")

            self._receipt_seq += 1
            receipt = {
                "receipt_no": f"RCP-{2000 + self._receipt_seq}",
                "plan_id": plan["id"],
                "plan_no": plan["plan_no"],
                "operator_flow": operator_flow,
                "owner_flow": plan["owner_flow"],
                "anti_block_id": plan["anti_block_id"],
                "site": plan["site"],
                "switch_mode": plan["switch_mode"],
                "switched_nodes": [
                    {"node_id": n["node_id"], "node_name": n["node_name"], "region": n["region"], "role": n["role"]}
                    for n in plan["affected_nodes"] if n["action"] == ACTION_SWITCH
                ],
                "observed_nodes": [
                    {"node_id": n["node_id"], "node_name": n["node_name"], "region": n["region"], "role": n["role"]}
                    for n in plan["affected_nodes"] if n["action"] == ACTION_OBSERVE
                ],
                "protection_updated": True,
                "snake_item_key": plan["snake_item_key"],
                "snake_version": plan.get("snake_version"),
                "created_at": _now(),
            }
            plan["receipt"] = receipt

            if plan["snake_item_key"] is not None:
                self._close_snake_todo(plan)
                self._snake.append_chain(
                    plan["snake_item_key"], "anti_block",
                    "确认代理切换并更新防护策略",
                    payload={"plan_id": plan["id"], "receipt_no": receipt["receipt_no"]},
                )
                snake_record = self._snake.get_record(plan["snake_item_key"])
                expect = {"site": plan["site"]} if "site" in snake_record["business_summary"] else {}
                self._snake.append_result_detail(
                    plan["snake_item_key"], "anti_block",
                    detail={"receipt_no": receipt["receipt_no"], "switched": receipt["switched_nodes"]},
                    expect=expect,
                )

            store.channel.publish(
                topic="rpa.proxy_switch_confirmed",
                source=operator_flow,
                payload={"plan_id": plan["id"], "receipt_no": receipt["receipt_no"]},
            )
            return copy.deepcopy(plan)

    def revoke_plan(self, plan_id: int, operator_flow: str, reason: str = "") -> dict[str, Any]:
        """所属采集流程撤销计划：节点活动占用同步释放。"""
        with self._lock:
            plan = self._require_active_plan(plan_id)
            self._assert_owner(plan, operator_flow)

            plan["status"] = PLAN_REVOKED
            plan["active"] = False
            plan["revoke_reason"] = reason.strip() or "所属流程主动撤销"
            plan["updated_at"] = _now()
            self._update_protection_strategy(plan, "已撤销")

            if plan["snake_item_key"] is not None:
                self._close_snake_todo(plan)
                self._snake.append_chain(
                    plan["snake_item_key"], self.SERVICE_NAME,
                    "撤销代理切换计划",
                    payload={"plan_id": plan["id"], "reason": plan["revoke_reason"]},
                )

            store.channel.publish(
                topic="rpa.proxy_switch_revoked",
                source=operator_flow,
                payload={"plan_id": plan["id"], "reason": plan["revoke_reason"]},
            )
            return copy.deepcopy(plan)

    # ------------------------------------------------------------------
    # 工作台查询：按流程、节点、地域过滤；跨流程只读可见
    # ------------------------------------------------------------------

    def list_plans(
        self,
        *,
        owner_flow: str | None = None,
        node_id: int | None = None,
        region: str | None = None,
        active_only: bool = False,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        with self._lock:
            plans = list(self._plans)
        if owner_flow:
            plans = [p for p in plans if p["owner_flow"] == owner_flow]
        if node_id is not None:
            plans = [
                p for p in plans
                if any(n["node_id"] == node_id for n in p["affected_nodes"])
            ]
        if region:
            plans = [
                p for p in plans
                if any(region == n["region"] or region in str(n["region"]) for n in p["affected_nodes"])
            ]
        if active_only:
            plans = [p for p in plans if p["status"] in ACTIVE_STATUSES]
        if status:
            plans = [p for p in plans if p["status"] == status]
        return [copy.deepcopy(p) for p in plans]

    def get_plan(self, plan_id: int) -> dict[str, Any] | None:
        for plan in self._plans:
            if plan["id"] == plan_id:
                return copy.deepcopy(plan)
        return None

    def list_receipts(self, owner_flow: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            receipts = [p["receipt"] for p in self._plans if p["receipt"]]
        if owner_flow:
            receipts = [r for r in receipts if r["owner_flow"] == owner_flow]
        return [copy.deepcopy(r) for r in receipts]

    def node_ownership_view(self) -> list[dict[str, Any]]:
        """节点归属视图：每个节点当前是否被活动计划占用、归属哪个流程（跨流程只读）。"""
        view = []
        for node in self.read_proxy_nodes():
            holder = self.active_plan_of_node(node["node_id"])
            view.append({
                **node,
                "active_plan_id": holder["id"] if holder else None,
                "active_plan_no": holder["plan_no"] if holder else None,
                "owner_flow": holder["owner_flow"] if holder else None,
                "occupied": holder is not None,
            })
        return view

    def domain_snapshot(self) -> dict[str, Any]:
        with self._lock:
            plans = list(self._plans)
        return {
            "module": self.SERVICE_NAME,
            "entity": "代理切换计划",
            "artifact": "封锁驱动的代理切换审批数据",
            "total": len(plans),
            "active": sum(1 for p in plans if p["status"] in ACTIVE_STATUSES),
            "confirmed": sum(1 for p in plans if p["status"] == PLAN_CONFIRMED),
            "revoked": sum(1 for p in plans if p["status"] == PLAN_REVOKED),
            "receipts": sum(1 for p in plans if p["receipt"]),
            "nodes_occupied": sum(
                1 for node in self.read_proxy_nodes()
                if self.active_plan_of_node(node["node_id"]) is not None
            ),
        }


# 全局单例
proxy_switch = ProxySwitchService()


def bootstrap_demo_switch_data() -> dict[str, Any]:
    """启动演示数据：为已触发封锁的防护策略生成一条可审批的活动计划。

    - 受重入保护：已有计划或解析失败时安静跳过，测试反复初始化也不重复造数。
    - 同步落一条处理项蛇记录，演示「计划与流程归属同时写入 + 蛇版本落印」。
    """
    if proxy_switch.list_plans():
        return {"seeded": False, "reason": "already_has_plans"}
    blocked = [
        row for row in store.rows("anti_block")
        if row.get("status") == "触发封锁"
    ]
    if not blocked:
        return {"seeded": False, "reason": "no_triggered_block"}

    strategy = blocked[0]
    owner = next(
        (str(row.get("流程名称")) for row in store.rows("bot_definition") if row.get("流程名称")),
        "默认采集流程",
    )
    item_key = f"BLK-{strategy['id']}"
    processing_snake.import_record(
        item_key,
        source_module="anti_block",
        data={"anti_block_id": strategy["id"], "site": strategy.get("目标站点")},
        business_summary={"site": strategy.get("目标站点")},
    )
    try:
        plan = proxy_switch.create_plan(
            int(strategy["id"]),
            owner,
            switch_mode=MODE_MAIN_FIRST,
            snake_item_key=item_key,
            source="bootstrap_demo",
        )
    except ValueError:
        return {"seeded": False, "reason": "node_conflict"}
    return {"seeded": True, "plan_id": plan["id"], "plan_no": plan["plan_no"], "owner_flow": owner}
