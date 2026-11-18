"""业务路由汇总：专用路由先注册，通用模块资源路由兜底。"""
from __future__ import annotations

from app.routers import proxy_switch, resource

# proxy_switch 含 /api/proxy-switch/* 与 /api/processing-snake/* 固定路径，
# 必须排在 /api/{module} 动态资源路由之前，否则会被当作未知模块拦截。
ROUTERS = [proxy_switch, resource]
