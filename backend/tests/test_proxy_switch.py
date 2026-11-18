"""代理切换数据能力 + 处理项蛇 测试。

覆盖需求：
- 读取代理节点/频控规则/防护策略三源数据，解析封锁事件的地域与节点归属
- 计划与流程归属同时写入；同一节点同一时间只能有一个活动计划
- 只有所属采集流程能确认/撤销，其他流程只读（403）
- 主备同时封锁的两种口径，同一节点都只落在一个活动计划里
- 处理项蛇：串联链路+待办、业务记录与结果明细对账、重复导入只认第一条、受控只读
- 确认时按处理项蛇版本更新防护策略并生成回执
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.processing_snake import (
    ProcessingSnakeService,
    processing_snake,
)
from app.services.proxy_switch import (
    MODE_MAIN_FIRST,
    MODE_TOGETHER,
    PLAN_CONFIRMED,
    PLAN_PENDING,
    PLAN_REVOKED,
    ProxySwitchService,
    bootstrap_demo_switch_data,
    proxy_switch,
)
from app.store import store


TRIGGERED_ANTI_BLOCK = 3
OWNER = "订单采集流程"
OTHER = "评论采集流程"


@pytest.fixture(autouse=True)
def isolated_services():
    """每个用例使用全新的服务实例，避免单例状态串扰。"""
    snake = ProcessingSnakeService()
    service = ProxySwitchService(snake)
    yield snake, service
    # 恢复全局单例的干净状态
    proxy_switch._plans.clear()
    proxy_switch._seq = 0
    proxy_switch._receipt_seq = 0


# ---------------------------------------------------------------------------
# 一、三源数据读取与封锁事件解析
# ---------------------------------------------------------------------------

class TestBlockEventParsing:
    def test_read_three_sources(self, isolated_services):
        _, service = isolated_services
        nodes = service.read_proxy_nodes()
        assert len(nodes) >= 3
        assert {"node_id", "region", "status"} <= set(nodes[0])

        rules = service.read_rate_limiter("电商采集站点A")
        assert rules, "应读到站点 A 的频控规则"
        assert all("电商采集站点A" in r["site"] for r in rules)

        protection = service.read_protection(TRIGGERED_ANTI_BLOCK)
        assert protection["status"] == "触发封锁"
        assert protection["switch_marker"] is None or isinstance(protection["switch_marker"], dict)

    def test_parse_resolves_regions_and_node_roles(self, isolated_services):
        _, service = isolated_services
        event = service.parse_block_event(TRIGGERED_ANTI_BLOCK)
        # 策略 IP代理池为「华东,华南」
        assert set(event["regions"]) == {"华东", "华南"}
        roles = {n["role"] for n in event["affected_nodes"]}
        assert "主节点" in roles
        assert "备用节点" in roles
        # 主备角色在同地域内各有归属
        for region in event["regions"]:
            region_nodes = [n for n in event["affected_nodes"] if n["region"] == region]
            assert sum(1 for n in region_nodes if n["role"] == "主节点") == 1

    def test_parse_rejects_non_triggered_strategy(self, isolated_services):
        _, service = isolated_services
        with pytest.raises(ValueError):
            service.parse_block_event(2)  # 已启用，未封锁

    def test_explicit_node_ids(self, isolated_services):
        _, service = isolated_services
        event = service.parse_block_event(
            TRIGGERED_ANTI_BLOCK, affected_node_ids=[1, 2], switch_mode=MODE_TOGETHER
        )
        assert {n["node_id"] for n in event["affected_nodes"]} == {1, 2}


# ---------------------------------------------------------------------------
# 二、计划生成：归属同时写入 + 节点级唯一活动计划
# ---------------------------------------------------------------------------

class TestPlanCreation:
    def test_owner_flow_required(self, isolated_services):
        _, service = isolated_services
        with pytest.raises(ValueError):
            service.create_plan(TRIGGERED_ANTI_BLOCK, "  ")

    def test_plan_and_owner_written_together(self, isolated_services):
        _, service = isolated_services
        plan = service.create_plan(TRIGGERED_ANTI_BLOCK, OWNER)
        assert plan["owner_flow"] == OWNER
        assert plan["status"] == PLAN_PENDING
        assert plan["active"] is True
        assert plan["target_node_ids"], "应包含需要切换的节点"
        assert plan["block_event"]["site"] == "电商采集站点A"

    def test_same_node_cannot_have_two_active_plans(self, isolated_services):
        _, service = isolated_services
        first = service.create_plan(TRIGGERED_ANTI_BLOCK, OWNER)
        occupied = first["target_node_ids"][0]
        # 另一个流程对同一节点再提计划：必须被拒绝
        with pytest.raises(ValueError, match="同一节点同一时间只能有一个活动计划"):
            service.create_plan(
                TRIGGERED_ANTI_BLOCK, OTHER, affected_node_ids=[occupied]
            )
        # 冲突时整单不落地：仍然只有一条计划
        assert len(service.list_plans()) == 1

    def test_released_after_terminal_status(self, isolated_services):
        _, service = isolated_services
        plan = service.create_plan(TRIGGERED_ANTI_BLOCK, OWNER,
                                   affected_node_ids=[1, 2], switch_mode=MODE_TOGETHER)
        service.confirm_plan(plan["id"], OWNER)
        # 终态后节点不再被活动计划占用，可再建计划（针对仍封锁的策略换节点）
        assert service.active_plan_of_node(1) is None
        second = service.create_plan(
            TRIGGERED_ANTI_BLOCK, OTHER, affected_node_ids=[1]
        )
        assert second["owner_flow"] == OTHER


# ---------------------------------------------------------------------------
# 三、主备同时封锁的两种口径
# ---------------------------------------------------------------------------

class TestMainBackupModes:
    def test_together_switches_both(self, isolated_services):
        _, service = isolated_services
        plan = service.create_plan(
            TRIGGERED_ANTI_BLOCK, OWNER,
            affected_node_ids=[1, 2], switch_mode=MODE_TOGETHER,
        )
        assert sorted(plan["target_node_ids"]) == [1, 2]
        assert plan["observe_node_ids"] == []
        assert all(n["action"] == "切换" for n in plan["affected_nodes"])

    def test_main_first_observes_backup(self, isolated_services):
        _, service = isolated_services
        plan = service.create_plan(
            TRIGGERED_ANTI_BLOCK, OWNER,
            affected_node_ids=[1, 2], switch_mode=MODE_MAIN_FIRST,
        )
        # 主节点切换、备用节点观察；同一节点仍只出现在这一个活动计划里
        assert plan["target_node_ids"] == [1]
        assert plan["observe_node_ids"] == [2]
        assert service.active_plan_of_node(2)["id"] == plan["id"]

    def test_both_modes_keep_single_active_plan_per_node(self, isolated_services):
        _, service = isolated_services
        plan_together = service.create_plan(
            TRIGGERED_ANTI_BLOCK, OWNER,
            affected_node_ids=[5, 6], switch_mode=MODE_TOGETHER,
        )
        for node_id in (5, 6):
            assert service.active_plan_of_node(node_id)["id"] == plan_together["id"]
        with pytest.raises(ValueError):
            service.create_plan(
                TRIGGERED_ANTI_BLOCK, OTHER,
                affected_node_ids=[6], switch_mode=MODE_MAIN_FIRST,
            )


# ---------------------------------------------------------------------------
# 四、流程归属权限：只有所属流程能确认/撤销
# ---------------------------------------------------------------------------

class TestOwnershipPermission:
    def test_other_flow_cannot_confirm(self, isolated_services):
        _, service = isolated_services
        plan = service.create_plan(TRIGGERED_ANTI_BLOCK, OWNER,
                                   affected_node_ids=[1], switch_mode=MODE_TOGETHER)
        with pytest.raises(PermissionError):
            service.confirm_plan(plan["id"], OTHER)
        assert service.get_plan(plan["id"])["status"] == PLAN_PENDING

    def test_other_flow_cannot_revoke(self, isolated_services):
        _, service = isolated_services
        plan = service.create_plan(TRIGGERED_ANTI_BLOCK, OWNER,
                                   affected_node_ids=[1], switch_mode=MODE_TOGETHER)
        with pytest.raises(PermissionError):
            service.revoke_plan(plan["id"], OTHER)

    def test_other_flow_can_read(self, isolated_services):
        _, service = isolated_services
        plan = service.create_plan(TRIGGERED_ANTI_BLOCK, OWNER,
                                   affected_node_ids=[1], switch_mode=MODE_TOGETHER)
        visible = service.list_plans(owner_flow=OTHER)
        assert visible == []  # 按所属流程过滤看不到别人的计划
        all_plans = service.list_plans()
        assert len(all_plans) == 1  # 但全量只读视图可见
        ownership = service.node_ownership_view()
        node1 = next(n for n in ownership if n["node_id"] == 1)
        assert node1["owner_flow"] == OWNER and node1["occupied"] is True

    def test_owner_confirm_generates_receipt_and_updates_strategy(self, isolated_services):
        _, service = isolated_services
        plan = service.create_plan(
            TRIGGERED_ANTI_BLOCK, OWNER,
            affected_node_ids=[1, 2], switch_mode=MODE_MAIN_FIRST,
        )
        updated = service.confirm_plan(plan["id"], OWNER)
        assert updated["status"] == PLAN_CONFIRMED
        receipt = updated["receipt"]
        assert receipt["receipt_no"].startswith("RCP-")
        assert receipt["operator_flow"] == OWNER
        assert [n["node_id"] for n in receipt["switched_nodes"]] == [1]
        assert [n["node_id"] for n in receipt["observed_nodes"]] == [2]
        assert receipt["protection_updated"] is True

        # 防护策略被按归属流程更新为切换标记
        strategy = store.find("anti_block", TRIGGERED_ANTI_BLOCK)
        marker = strategy["proxy_switch_marker"]
        assert marker["needed"] is True
        assert marker["state"] == "已切换"
        assert marker["owner_flow"] == OWNER
        assert marker["plan_id"] == plan["id"]

        assert len(service.list_receipts()) == 1

    def test_owner_revoke_releases_nodes(self, isolated_services):
        _, service = isolated_services
        plan = service.create_plan(TRIGGERED_ANTI_BLOCK, OWNER,
                                   affected_node_ids=[1], switch_mode=MODE_TOGETHER)
        revoked = service.revoke_plan(plan["id"], OWNER, reason="备用节点池不足")
        assert revoked["status"] == PLAN_REVOKED
        assert service.active_plan_of_node(1) is None
        assert store.find("anti_block", TRIGGERED_ANTI_BLOCK)["proxy_switch_marker"]["state"] == "已撤销"

    def test_terminal_plan_cannot_operate_again(self, isolated_services):
        _, service = isolated_services
        plan = service.create_plan(TRIGGERED_ANTI_BLOCK, OWNER,
                                   affected_node_ids=[1], switch_mode=MODE_TOGETHER)
        service.revoke_plan(plan["id"], OWNER)
        with pytest.raises(ValueError):
            service.confirm_plan(plan["id"], OWNER)


# ---------------------------------------------------------------------------
# 五、处理项蛇：去重、链路版本、待办联动、对账、受控只读
# ---------------------------------------------------------------------------

class TestProcessingSnake:
    def test_duplicate_import_keeps_first(self, isolated_services):
        snake, _ = isolated_services
        first, created = snake.import_record("BLK-9", "anti_block", {"v": 1})
        assert created is True
        again, created_again = snake.import_record("BLK-9", "anti_block", {"v": 2})
        assert created_again is False
        assert again["data"] == {"v": 1}  # 只认第一条
        assert len(snake.list_records()) == 1

    def test_chain_versions_and_todo_linkage(self, isolated_services):
        snake, _ = isolated_services
        snake.import_record("BLK-9", "anti_block", {})
        assert snake.current_version("BLK-9") == 1
        link = snake.append_chain(
            "BLK-9", "proxy_switch", "生成代理切换计划",
            payload={"plan": "x"}, todo_title="确认切换",
        )
        assert snake.current_version("BLK-9") == 2
        assert link["version"] == 2
        pending = snake.pending_todos("BLK-9")
        assert len(pending) == 1
        snake.handle_todo(pending[0]["todo_id"], OWNER)
        assert snake.pending_todos("BLK-9") == []
        chain = snake.list_chain("BLK-9")
        assert [c["module"] for c in chain] == ["anti_block", "proxy_switch"]

    def test_plan_stamped_with_snake_version(self, isolated_services):
        snake, service = isolated_services
        snake.import_record("BLK-9", "anti_block", {"site": "电商采集站点A"},
                            business_summary={"site": "电商采集站点A"})
        plan = service.create_plan(
            TRIGGERED_ANTI_BLOCK, OWNER,
            affected_node_ids=[1], switch_mode=MODE_TOGETHER,
            snake_item_key="BLK-9",
        )
        # 计划按处理项蛇版本落印
        assert plan["snake_version"] == snake.current_version("BLK-9")
        assert snake.get_proxy_switch_plan("BLK-9")["id"] == plan["id"]

        confirmed = service.confirm_plan(plan["id"], OWNER)
        assert confirmed["receipt"]["snake_version"] == plan["snake_version"]
        # 确认后待办自动关闭
        assert snake.pending_todos("BLK-9") == []

    def test_reconcile_business_and_result_details(self, isolated_services):
        snake, _ = isolated_services
        snake.import_record("BLK-9", "anti_block", {},
                            business_summary={"site": "电商采集站点A"})
        snake.append_result_detail(
            "BLK-9", "anti_block", {"ok": True},
            expect={"site": "电商采集站点A"},
        )
        report = snake.reconcile()
        assert report["ok"] is True and report["mismatches"] == []

        # 制造口径不一致
        bad = ProcessingSnakeService()
        bad.import_record("X", "m", {}, business_summary={"site": "A"})
        bad.append_result_detail("X", "m", {}, expect={"site": "B"})
        bad_report = bad.reconcile()
        assert bad_report["ok"] is False
        assert "不一致" in bad_report["mismatches"][0]["reason"]

    def test_readonly_surface_has_no_mutation_api(self, isolated_services):
        snake, _ = isolated_services
        # 受控只读：对外查询方法只返回拷贝，外部改动不污染底稿
        snake.import_record("BLK-9", "anti_block", {"v": 1})
        view = snake.get_record("BLK-9")
        view["data"]["v"] = 999
        assert snake.get_record("BLK-9")["data"]["v"] == 1
        assert not hasattr(snake, "update_record")
        assert not hasattr(snake, "delete_record")


# ---------------------------------------------------------------------------
# 六、反封锁只标记需要切换
# ---------------------------------------------------------------------------

class TestAntiBlockMarkerOnly:
    def test_trigger_block_only_marks(self, isolated_services):
        from app.services.anti_block import AntiBlockService

        entry, message = AntiBlockService().trigger_block(2)
        assert entry["status"] == "触发封锁"
        marker = entry["proxy_switch_marker"]
        assert marker["needed"] is True
        assert marker["plan_id"] is None  # 反封锁不建计划、不动节点
        assert "仅标记" in message
        # 节点池没有被反封锁直接改动
        assert store.find("proxy_router", 1)["status"] == "在线"


# ---------------------------------------------------------------------------
# 七、启动演示数据幂等
# ---------------------------------------------------------------------------

class TestBootstrap:
    def test_bootstrap_idempotent(self, isolated_services):
        result = bootstrap_demo_switch_data()
        assert result["seeded"] is True
        again = bootstrap_demo_switch_data()
        assert again["seeded"] is False
        plans = proxy_switch.list_plans()
        assert len(plans) == 1
        assert plans[0]["snake_item_key"] is not None
