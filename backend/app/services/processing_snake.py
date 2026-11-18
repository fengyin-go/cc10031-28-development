"""处理项蛇 — 串联处理链路的受控只读业务记录。

设计定位（业务口径）：
- 处理项蛇是跨模块处理链路的「业务记录底稿」：每个处理项一个 item_key，
  导入即定稿，重复导入只认第一条记录；后续环节只能追加链路、待办和结果明细，
  不能回头改写业务记录（受控只读：对外只暴露查询口径，不提供通用更新/删除）。
- 每次链路环节推进产生一个新版本号，代理切换计划、结果明细都按当时的
  处理项蛇版本落印，保证反封锁模块取用时版本可核对。
- 待办与链路联动：环节追加时可同步挂出待办，环节处理完待办随之关闭。
- 业务记录和结果明细对得上：结果明细必须引用已存在的处理项与版本，
  reconcile 会按 business_summary 与结果明细的期望键做对账。
- 兼容既有业务口径：处理项蛇不替代各模块自己的表和服务，只在旁边
  追加一条可追溯的链路底稿，现有 CRUD 与贯通链路不受影响。
"""
from __future__ import annotations

import copy
import threading
from datetime import datetime, timezone
from typing import Any


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ProcessingSnakeService:
    """处理项蛇：导入去重、链路串联、待办联动、结果对账的受控只读记录器。"""

    SERVICE_NAME = "processing_snake"

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._item_seq = 0
        self._link_seq = 0
        self._todo_seq = 0
        self._result_seq = 0
        self._records: dict[str, dict[str, Any]] = {}
        self._links: list[dict[str, Any]] = []
        self._todos: list[dict[str, Any]] = []

    # ------------------------------------------------------------------
    # 导入：重复导入只认第一条
    # ------------------------------------------------------------------

    def import_record(
        self,
        item_key: str,
        source_module: str,
        data: dict[str, Any],
        business_summary: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """导入一个处理项。相同 item_key 重复导入时直接返回首条记录。

        返回 (记录, 是否首次导入)。
        """
        with self._lock:
            existing = self._records.get(item_key)
            if existing is not None:
                return dict(existing), False
            self._item_seq += 1
            now = _now()
            record = {
                "item_id": self._item_seq,
                "item_key": item_key,
                "version": 1,
                "source_module": source_module,
                "data": dict(data),
                "business_summary": dict(business_summary or {}),
                "result_details": [],
                "proxy_switch_plan": None,
                "imported_at": now,
                "updated_at": now,
            }
            self._records[item_key] = record
            self._links.append(self._make_link(record, source_module, "处理项导入", {
                "item_key": item_key,
                "source_module": source_module,
            }))
            return dict(record), True

    def _require_record(self, item_key: str) -> dict[str, Any]:
        record = self._records.get(item_key)
        if record is None:
            raise KeyError(f"处理项 {item_key} 尚未导入处理项蛇")
        return record

    # ------------------------------------------------------------------
    # 链路：串联处理链路，每推进一个环节版本号 +1
    # ------------------------------------------------------------------

    def _make_link(
        self, record: dict[str, Any], module: str, action: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        self._link_seq += 1
        return {
            "link_id": self._link_seq,
            "item_id": record["item_id"],
            "item_key": record["item_key"],
            "version": record["version"],
            "module": module,
            "action": action,
            "status": "已流转",
            "payload": dict(payload),
            "created_at": _now(),
        }

    def append_chain(
        self,
        item_key: str,
        module: str,
        action: str,
        payload: dict[str, Any] | None = None,
        todo_title: str | None = None,
    ) -> dict[str, Any]:
        """追加一个处理链路环节并推进版本；可同时挂出联动待办。"""
        with self._lock:
            record = self._require_record(item_key)
            record["version"] += 1
            record["updated_at"] = _now()
            link = self._make_link(record, module, action, payload or {})
            self._links.append(link)
            if todo_title:
                self._todo_seq += 1
                todo = {
                    "todo_id": self._todo_seq,
                    "item_id": record["item_id"],
                    "item_key": item_key,
                    "version": record["version"],
                    "module": module,
                    "title": todo_title,
                    "status": "待处理",
                    "handled_by": None,
                    "created_at": _now(),
                    "handled_at": None,
                }
                self._todos.append(todo)
                link["todo_id"] = todo["todo_id"]
            return dict(link)

    # ------------------------------------------------------------------
    # 待办：与链路联动更新
    # ------------------------------------------------------------------

    def handle_todo(self, todo_id: int, handled_by: str) -> dict[str, Any]:
        with self._lock:
            for todo in self._todos:
                if todo["todo_id"] == todo_id:
                    if todo["status"] == "已处理":
                        return dict(todo)
                    todo["status"] = "已处理"
                    todo["handled_by"] = handled_by
                    todo["handled_at"] = _now()
                    return dict(todo)
            raise KeyError(f"待办 {todo_id} 不存在")

    def pending_todos(self, item_key: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            return [
                dict(t) for t in self._todos
                if t["status"] == "待处理" and (item_key is None or t["item_key"] == item_key)
            ]

    # ------------------------------------------------------------------
    # 代理切换计划挂接 / 结果明细：全部按处理项蛇版本落印
    # ------------------------------------------------------------------

    def attach_proxy_switch_plan(self, item_key: str, plan: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            record = self._require_record(item_key)
            stamped = copy.deepcopy(plan)
            stamped["snake_version"] = record["version"]
            record["proxy_switch_plan"] = stamped
            record["updated_at"] = _now()
            return copy.deepcopy(stamped)

    def get_proxy_switch_plan(self, item_key: str) -> dict[str, Any] | None:
        with self._lock:
            record = self._require_record(item_key)
            return copy.deepcopy(record["proxy_switch_plan"]) if record["proxy_switch_plan"] else None

    def current_version(self, item_key: str) -> int:
        with self._lock:
            return self._require_record(item_key)["version"]

    def append_result_detail(
        self,
        item_key: str,
        module: str,
        detail: dict[str, Any],
        expect: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """追加结果明细。明细携带处理项与版本，便于和业务记录对账。"""
        with self._lock:
            record = self._require_record(item_key)
            self._result_seq += 1
            result = {
                "result_id": self._result_seq,
                "item_id": record["item_id"],
                "item_key": item_key,
                "version": record["version"],
                "module": module,
                "detail": dict(detail),
                "expect": dict(expect or {}),
                "created_at": _now(),
            }
            record["result_details"].append(result)
            record["updated_at"] = _now()
            return dict(result)

    def reconcile(self) -> dict[str, Any]:
        """对账：结果明细必须能回溯到业务记录，且期望键与业务摘要一致。"""
        with self._lock:
            mismatches: list[dict[str, Any]] = []
            for key, record in self._records.items():
                for result in record["result_details"]:
                    if result["item_id"] != record["item_id"]:
                        mismatches.append({
                            "item_key": key, "result_id": result["result_id"],
                            "reason": "结果明细引用的处理项不存在",
                        })
                    for expect_key, expect_value in result["expect"].items():
                        actual = record["business_summary"].get(expect_key)
                        if actual != expect_value:
                            mismatches.append({
                                "item_key": key, "result_id": result["result_id"],
                                "reason": f"业务口径 {expect_key} 不一致：摘要={actual}，结果={expect_value}",
                            })
            return {
                "records": len(self._records),
                "result_details": sum(len(r["result_details"]) for r in self._records.values()),
                "mismatches": mismatches,
                "ok": not mismatches,
            }

    # ------------------------------------------------------------------
    # 受控只读查询口径：只给查看，不给改动
    # ------------------------------------------------------------------

    def get_record(self, item_key: str) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._require_record(item_key))

    def list_records(self) -> list[dict[str, Any]]:
        with self._lock:
            return [copy.deepcopy(r) for r in self._records.values()]

    def list_chain(self, item_key: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            return [
                copy.deepcopy(link) for link in self._links
                if item_key is None or link["item_key"] == item_key
            ]

    def list_todos(self, item_key: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            return [
                copy.deepcopy(todo) for todo in self._todos
                if item_key is None or todo["item_key"] == item_key
            ]

    def domain_snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "module": self.SERVICE_NAME,
                "entity": "处理项蛇",
                "artifact": "处理链路受控只读底稿",
                "records": len(self._records),
                "chain_links": len(self._links),
                "todos_total": len(self._todos),
                "todos_pending": sum(1 for t in self._todos if t["status"] == "待处理"),
                "result_details": sum(len(r["result_details"]) for r in self._records.values()),
                "plans_attached": sum(1 for r in self._records.values() if r["proxy_switch_plan"]),
                "reconcile_ok": self.reconcile()["ok"],
            }


# 全局单例：与 runtime_session / callback_hook 同样的跨模块共享方式
processing_snake = ProcessingSnakeService()
