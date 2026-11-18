# RPA流程自动化中心

面向企业数据采集场景，提供RPA流程编排、元素定位、执行调度和结果校验的自动化运营平台

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/WorkbenchView.vue|tsx  全部模块共享工作台
│   ├── src/data/modules.ts              18个业务模块配置
│   ├── src/api/              统一请求封装
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/resource.py  通用查询/导出/登记/动作接口
│   ├── app/services/<key>.py    每个模块独立领域逻辑
│   ├── app/services/base.py     共享服务骨架
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm ci
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 流程定义 | `bot_definition` | RPA流程 | 流程名称、目标系统、触发方式 |
| 步骤编排 | `step_editor` | 流程步骤 | 步骤序号、步骤名称、操作类型 |
| 元素定位 | `element_selector` | 定位规则 | 规则名称、选择器类型、定位表达式 |
| 触发配置 | `trigger_config` | 触发器 | 触发器名称、触发类型、调度表达式 |
| 执行记录 | `execution_log` | 执行日志 | 执行编号、所属流程、开始时间 |
| 数据提取 | `data_extract` | 提取规则 | 规则名称、源页面结构、提取字段 |
| 凭据管理 | `credential_store` | 系统凭据 | 凭据名称、目标系统、认证方式 |
| 环境配置 | `env_profile` | 执行环境 | 环境名称、浏览器版本、操作系统 |
| 反封锁 | `anti_block` | 防护策略 | 策略名称、目标站点、请求间隔 |
| 结果校验 | `result_verify` | 校验规则 | 规则名称、校验字段、校验条件 |
| 批次追踪 | `batch_tracker` | 采集批次 | 批次编号、触发流程、采集时间 |
| 页面建模 | `page_model` | 页面模型 | 模型名称、目标站点、页面结构 |
| 数据补全 | `data_enrich` | 补全规则 | 规则名称、目标字段、补全来源 |
| 代理路由 | `proxy_router` | 代理节点 | 节点名称、IP地址、出口地域 |
| 频控策略 | `rate_limiter` | 频控规则 | 规则名称、目标站点、每秒上限 |
| 输出格式 | `output_format` | 格式模板 | 模板名称、输出类型、字段映射 |
| 异常日志 | `anomaly_log` | 异常事件 | 事件编号、关联流程、异常类型 |
| 调度计划 | `scheduler` | 采集计划 | 计划名称、绑定流程、cron表达式 |
| 运行概览 | `report_dashboard` | 运行数据 | 统计日期、总执行数、成功数 |
| 任务队列 | `task_queue` | 排队任务 | 任务编号、流程名称、提交时间 |

## 约定

- 全部模块共享`frontend/src/views/WorkbenchView.vue|tsx`和一套资源路由。
- 每个模块仍保留独立`backend/app/services/<模块>.py`；纯前端保留
  `frontend/src/domain/<模块>.ts`，用于承载真实业务差异并维持36题位容量。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## 代理切换数据能力（反封锁联动）

反封锁触发后只标记「需要代理切换」，不直接改节点池；可审批的代理切换计划由
独立的跨模块能力生成与管理，代码在 `backend/app/services/proxy_switch.py`，
链路底稿在 `backend/app/services/processing_snake.py`，专用路由在
`backend/app/routers/proxy_switch.py`，前端入口为「代理切换工作台」
（路由 `/proxy-workbench`）。

核心口径：

- **三源只读消费**：代理节点 `proxy_router`、频控规则 `rate_limiter`、
  新到达的防护策略 `anti_block`；解析封锁事件影响的地域与节点主备归属。
- **计划与流程归属同时写入**：`owner_flow` 是所属采集流程，只有它能确认/撤销，
  其他流程调写操作返回 403，只能只读查看节点归属和回执。
- **同一节点同一时间只能有一个活动（待确认）计划**：创建前整体校验，
  任一节点冲突则整单不落地；终态（已确认/已撤销）后节点占用自动释放。
- **两种主备口径**：`主备一起切换` 或 `先切主并观察备`，两种口径下同一节点
  都只会出现在至多一个活动计划里。
- **处理项蛇（受控只读）**：处理项导入即定稿、重复导入只认第一条；链路串联带
  版本号并联动待办；切换计划、结果明细按蛇版本落印；提供业务记录与结果明细对账。
  对外只暴露查询口径，无通用更新/删除接口。
- **确认留痕**：所属流程确认时按处理项蛇版本更新防护策略 `proxy_switch_marker`
  并生成 `RCP-` 开头的切换回执，反封锁模块据此消费计划。

主要接口：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/anti-block/{id}/trigger-block` | 封锁触发，仅标记需要切换 |
| POST | `/api/proxy-switch/parse-block` | 解析封锁事件（只读，不落库） |
| POST | `/api/proxy-switch/plans` | 生成带流程归属的切换计划 |
| GET | `/api/proxy-switch/plans` | 按流程/节点/地域/状态查计划 |
| POST | `/api/proxy-switch/plans/{id}/confirm` | 所属流程确认，出回执并更新策略 |
| POST | `/api/proxy-switch/plans/{id}/revoke` | 所属流程撤销，释放节点占用 |
| GET | `/api/proxy-switch/nodes` | 节点归属占用视图（跨流程只读） |
| GET | `/api/proxy-switch/receipts` | 切换回执查询 |
| POST | `/api/processing-snake/import` | 处理项导入（重复导入只认第一条） |
| GET | `/api/processing-snake/records|chain|todos` | 处理项蛇只读查询 |
| GET | `/api/processing-snake/reconcile` | 业务记录与结果明细对账 |

领域：数据工程
子方向：数据采集、爬虫与RPA
