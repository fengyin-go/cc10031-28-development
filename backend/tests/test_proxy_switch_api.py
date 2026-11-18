"""代理切换 / 处理项蛇 HTTP 接口测试：路由分派、403 权限、工作台过滤。"""
from __future__ import annotations

import os
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.main import app
from app.services.processing_snake import processing_snake
from app.services.proxy_switch import proxy_switch


@pytest.fixture()
def client():
    with TestClient(app) as c:
        # startup 事件会预置演示计划，进入上下文后再清理，保证用例独立
        proxy_switch._plans.clear()
        proxy_switch._seq = 0
        proxy_switch._receipt_seq = 0
        processing_snake._records.clear()
        processing_snake._links.clear()
        processing_snake._todos.clear()
        processing_snake._item_seq = 0
        processing_snake._link_seq = 0
        processing_snake._todo_seq = 0
        processing_snake._result_seq = 0
        yield c


def test_health_and_routing(client):
    resp = client.get("/api/health")
    assert resp.status_code == 200
    # 固定路径未被 /api/{module} 动态路由抢占
    resp = client.get("/api/proxy-switch/plans")
    assert resp.status_code == 200
    assert resp.json() == {"items": [], "total": 0}


def test_full_switch_approval_flow(client):
    owner, other = "订单采集流程", "评论采集流程"

    # 1. 反封锁触发：只标记需要切换
    resp = client.post("/api/anti-block/2/trigger-block")
    assert resp.status_code == 200
    marker = resp.json()["entry"]["proxy_switch_marker"]
    assert marker["needed"] is True and marker["plan_id"] is None

    # 2. 解析封锁事件（只读）
    resp = client.post("/api/proxy-switch/parse-block", json={
        "anti_block_id": 2,
        "affected_node_ids": [3, 4],
        "switch_mode": "主备一起切换",
    })
    assert resp.status_code == 200
    event = resp.json()["event"]
    assert {n["node_id"] for n in event["affected_nodes"]} == {3, 4}

    # 3. 导入处理项蛇（重复导入只认第一条）
    r1 = client.post("/api/processing-snake/import", json={
        "item_key": "BLK-2", "source_module": "anti_block",
        "data": {"site": "资讯采集站点B"}, "business_summary": {"site": "资讯采集站点B"},
    })
    assert r1.json()["created"] is True
    r2 = client.post("/api/processing-snake/import", json={
        "item_key": "BLK-2", "source_module": "anti_block", "data": {"site": "篡改值"},
    })
    assert r2.json()["created"] is False
    assert r2.json()["record"]["data"]["site"] == "资讯采集站点B"

    # 4. 生成计划：计划与流程归属同时写入
    resp = client.post("/api/proxy-switch/plans", json={
        "anti_block_id": 2, "owner_flow": owner,
        "affected_node_ids": [3, 4], "switch_mode": "主备一起切换",
        "snake_item_key": "BLK-2",
    })
    assert resp.status_code == 200
    plan = resp.json()["plan"]
    plan_id = plan["id"]
    assert plan["owner_flow"] == owner and plan["snake_version"] >= 2

    # 5. 同节点重复计划被拒（400），跨流程确认被拒（403）
    conflict = client.post("/api/proxy-switch/plans", json={
        "anti_block_id": 2, "owner_flow": other, "affected_node_ids": [3],
    })
    assert conflict.status_code == 400
    forbidden = client.post(f"/api/proxy-switch/plans/{plan_id}/confirm", json={
        "operator_flow": other,
    })
    assert forbidden.status_code == 403

    # 6. 工作台过滤：按流程 / 节点 / 地域
    assert client.get("/api/proxy-switch/plans", params={"owner_flow": owner}).json()["total"] == 1
    assert client.get("/api/proxy-switch/plans", params={"node_id": 3}).json()["total"] == 1
    assert client.get("/api/proxy-switch/plans", params={"region": "华南"}).json()["total"] == 1
    assert client.get("/api/proxy-switch/plans", params={"region": "华北"}).json()["total"] == 0
    # 其他流程只读查看节点归属
    ownership = client.get("/api/proxy-switch/nodes").json()["items"]
    node3 = next(n for n in ownership if n["node_id"] == 3)
    assert node3["owner_flow"] == owner and node3["occupied"] is True

    # 7. 所属流程确认：回执 + 防护策略按蛇版本更新
    ok = client.post(f"/api/proxy-switch/plans/{plan_id}/confirm", json={
        "operator_flow": owner,
    })
    assert ok.status_code == 200
    body = ok.json()
    assert body["receipt"]["receipt_no"].startswith("RCP-")
    assert body["plan"]["status"] == "已确认"
    strategy = client.get("/api/anti_block/2").json()
    assert strategy["proxy_switch_marker"]["state"] == "已切换"

    # 8. 回执与处理项蛇链路、待办、对账
    receipts = client.get("/api/proxy-switch/receipts").json()
    assert receipts["total"] == 1
    todos = client.get("/api/processing-snake/todos", params={"item_key": "BLK-2"}).json()
    assert all(t["status"] == "已处理" for t in todos["items"])
    reconcile = client.get("/api/processing-snake/reconcile").json()
    assert reconcile["ok"] is True
    chain = client.get("/api/processing-snake/chain", params={"item_key": "BLK-2"}).json()
    modules = [item["module"] for item in chain["items"]]
    assert "proxy_switch" in modules and "anti_block" in modules


def test_revoke_flow_and_validation_errors(client):
    client.post("/api/anti-block/2/trigger-block")
    resp = client.post("/api/proxy-switch/plans", json={
        "anti_block_id": 2, "owner_flow": "流程X",
        "affected_node_ids": [5], "switch_mode": "先切主并观察备",
    })
    plan_id = resp.json()["plan"]["id"]

    # 缺少所属流程
    bad = client.post("/api/proxy-switch/plans", json={"anti_block_id": 2, "owner_flow": ""})
    assert bad.status_code == 400

    # 撤销后节点释放，且终态计划不可再操作
    revoked = client.post(f"/api/proxy-switch/plans/{plan_id}/revoke", json={
        "operator_flow": "流程X", "reason": "节点池调整",
    })
    assert revoked.status_code == 200
    again = client.post(f"/api/proxy-switch/plans/{plan_id}/confirm", json={
        "operator_flow": "流程X",
    })
    assert again.status_code == 400
    assert client.get("/api/proxy-switch/nodes").json()["items"][4]["occupied"] is False


def test_unknown_resources(client):
    assert client.post("/api/proxy-switch/parse-block", json={
        "anti_block_id": 999,
    }).status_code == 404
    assert client.get("/api/processing-snake/records/NOPE").status_code == 404
    assert client.get("/api/proxy-switch/plans/999").status_code == 404
