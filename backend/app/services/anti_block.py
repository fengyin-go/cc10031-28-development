"""反封锁领域服务 — 防护策略管理，输出 domain_snapshot 供跨模块消费。

数据工程核心：数据清洗与转换链路处理 — 跨模块数据产物
"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class AntiBlockService(BaseModuleService):
    MODULE = "anti_block"
    ENTITY = "防护策略"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["策略名称", "目标站点", "请求间隔", "IP代理池", "UA轮换", "验证码处理", "Cookie策略"]
    REQUIRED_FIELDS = ["策略名称", "目标站点", "请求间隔"]
    STATUS_ORDER = ["草稿", "已启用", "触发封锁", "已停用"]
    ACTION_RULES = {"启用策略": "草稿", "停用策略": "已启用"}
    NEGATIVE_ACTIONS = ["停用策略"]

    def trigger_block(self, entry_id: int) -> tuple[dict[str, Any] | None, str]:
        """封锁触发：只把策略标记为「触发封锁 + 需要代理切换」，不直接动节点。

        反封锁模块不生成切换计划、不改节点池；计划由代理切换能力按
        所属采集流程生成并审批，处理结果再回写 proxy_switch_marker。
        """
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"
        entry["status"] = "触发封锁"
        entry["pending"] = True
        entry["abnormal"] = True
        entry["proxy_switch_marker"] = {
            "needed": True,
            "state": "待生成切换计划",
            "plan_id": None,
            "plan_no": None,
            "owner_flow": None,
            "snake_version": None,
            "updated_at": None,
        }
        return entry, "封锁已触发，仅标记需要代理切换，等待生成可审批切换计划"

    def get_switch_marker(self, entry_id: int) -> dict[str, Any] | None:
        """读取策略上的代理切换标记（只读）。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None
        return entry.get("proxy_switch_marker")

    def get_site_protection(self, site: str) -> dict[str, Any]:
        """查询指定站点的反封锁策略覆盖情况。"""
        rows = store.rows(self.MODULE)
        active = [
            {
                "id": r["id"], "name": r.get("策略名称"),
                "interval": r.get("请求间隔"), "ip_pool": r.get("IP代理池"),
                "ua_rotation": r.get("UA轮换"), "captcha": r.get("验证码处理"),
            }
            for r in rows
            if r.get("status") == "已启用" and (site in r.get("目标站点", "") or site == r.get("目标站点"))
        ]
        return {
            "site": site,
            "active_protections": len(active),
            "strategies": active,
            "blocked": sum(1 for r in rows if r.get("status") == "触发封锁"),
        }

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        rows = store.rows(self.MODULE)
        return {
            **base,
            "active_strategies": sum(1 for r in rows if r.get("status") == "已启用"),
            "blocked_count": sum(1 for r in rows if r.get("status") == "触发封锁"),
        }