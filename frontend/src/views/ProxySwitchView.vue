<template>
  <section class="page proxy-workbench">
    <header class="page-head">
      <div>
        <h2>代理切换工作台</h2>
        <p class="page-desc">
          反封锁只标记需要切换；切换计划与所属采集流程同时写入，同一节点同一时间只有一个活动计划。
          所属流程可确认或撤销，其他流程只读查看节点归属与回执。
        </p>
      </div>
    </header>

    <div class="stat-row">
      <article class="stat-card">
        <span class="stat-label">计划总数</span>
        <strong class="stat-value">{{ plans.length }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">待确认</span>
        <strong class="stat-value">{{ countByStatus('待确认') }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">已确认</span>
        <strong class="stat-value">{{ countByStatus('已确认') }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">节点被占用</span>
        <strong class="stat-value">{{ ownership.filter((n) => n.occupied).length }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label class="filter-item">
        <span>当前流程</span>
        <input v-model="operatorFlow" data-testid="filter-operator" placeholder="所属采集流程" />
      </label>
      <label class="filter-item">
        <span>按流程筛选</span>
        <input v-model="filters.owner_flow" data-testid="filter-flow" placeholder="流程归属" />
      </label>
      <label class="filter-item">
        <span>节点ID</span>
        <input v-model="filters.node_id" data-testid="filter-node" placeholder="如 1" />
      </label>
      <label class="filter-item">
        <span>地域</span>
        <input v-model="filters.region" data-testid="filter-region" placeholder="如 华东" />
      </label>
      <label class="filter-item">
        <span>状态</span>
        <select v-model="filters.status" data-testid="filter-status">
          <option value="">全部</option>
          <option value="待确认">待确认</option>
          <option value="已确认">已确认</option>
          <option value="已撤销">已撤销</option>
        </select>
      </label>
      <button class="btn" data-testid="query" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <p v-if="errorMessage" class="error-text" data-testid="error-banner">{{ errorMessage }}</p>

    <h3 class="section-title">代理切换计划</h3>
    <table class="data-table" data-testid="plan-table">
      <thead>
        <tr>
          <th>计划编号</th>
          <th>所属流程</th>
          <th>目标站点</th>
          <th>影响地域</th>
          <th>受影响节点</th>
          <th>切换口径</th>
          <th>蛇版本</th>
          <th>状态</th>
          <th>回执</th>
          <th>操作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="plan in plans" :key="plan.id" :data-testid="`plan-row-${plan.id}`" :data-status="plan.status">
          <td>{{ plan.plan_no }}</td>
          <td>{{ plan.owner_flow }}</td>
          <td>{{ plan.site }}</td>
          <td>{{ plan.affected_regions.join('、') }}</td>
          <td>
            <span v-for="node in plan.affected_nodes" :key="node.node_id" class="node-chip" :data-role="node.role">
              {{ node.node_name }}（{{ node.role }}·{{ node.action }}）
            </span>
          </td>
          <td>{{ plan.switch_mode }}</td>
          <td>{{ plan.snake_version ?? '—' }}</td>
          <td>
            <span class="status-badge" :data-status="plan.status">{{ plan.status }}</span>
          </td>
          <td>{{ plan.receipt ? String(plan.receipt.receipt_no) : '—' }}</td>
          <td class="row-actions">
            <template v-if="plan.status === '待确认'">
              <button
                v-if="isOwner(plan)"
                class="link"
                type="button"
                :data-testid="`confirm-${plan.id}`"
                @click="onConfirm(plan)"
              >
                确认切换
              </button>
              <button
                v-if="isOwner(plan)"
                class="link danger"
                type="button"
                :data-testid="`revoke-${plan.id}`"
                @click="onRevoke(plan)"
              >
                撤销
              </button>
              <span v-else class="readonly-hint" :data-testid="`readonly-${plan.id}`">
                只读：归属{{ plan.owner_flow }}
              </span>
            </template>
            <span v-else class="readonly-hint">已终态</span>
          </td>
        </tr>
        <tr v-if="!plans.length">
          <td colspan="10" class="empty-state">暂无切换计划，可在下方对触发封锁的防护策略生成计划</td>
        </tr>
      </tbody>
    </table>

    <h3 class="section-title">节点归属（跨流程只读）</h3>
    <table class="data-table" data-testid="ownership-table">
      <thead>
        <tr>
          <th>节点ID</th>
          <th>节点名称</th>
          <th>出口地域</th>
          <th>节点状态</th>
          <th>是否占用</th>
          <th>活动计划</th>
          <th>归属流程</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="node in ownership" :key="node.node_id" :data-testid="`node-row-${node.node_id}`">
          <td>{{ node.node_id }}</td>
          <td>{{ node.node_name }}</td>
          <td>{{ node.region }}</td>
          <td>{{ node.status }}</td>
          <td>
            <span class="status-badge" :data-status="node.occupied ? '待确认' : ''">
              {{ node.occupied ? '活动计划占用中' : '空闲' }}
            </span>
          </td>
          <td>{{ node.active_plan_no ?? '—' }}</td>
          <td>{{ node.owner_flow ?? '—' }}</td>
        </tr>
      </tbody>
    </table>

    <h3 class="section-title">生成切换计划</h3>
    <form class="modal create-panel" @submit.prevent="onCreate">
      <label class="create-field">
        <span>封锁策略ID（anti_block）</span>
        <input v-model.number="createForm.anti_block_id" data-testid="create-anti-block" type="number" required />
      </label>
      <label class="create-field">
        <span>所属采集流程</span>
        <input v-model="createForm.owner_flow" data-testid="create-owner" required />
      </label>
      <label class="create-field">
        <span>切换口径</span>
        <select v-model="createForm.switch_mode" data-testid="create-mode">
          <option value="先切主并观察备">先切主并观察备</option>
          <option value="主备一起切换">主备一起切换</option>
        </select>
      </label>
      <label class="create-field">
        <span>指定节点ID（逗号分隔，留空按地域解析）</span>
        <input v-model="createForm.nodeIdsText" data-testid="create-nodes" placeholder="如 1,2" />
      </label>
      <label class="create-field">
        <span>处理项蛇 item_key（可选）</span>
        <input v-model="createForm.snake_item_key" data-testid="create-snake" placeholder="如 BLK-3" />
      </label>
      <button class="btn primary" data-testid="submit-create" type="submit" :disabled="submitting">
        {{ submitting ? '提交中…' : '生成计划' }}
      </button>
    </form>

    <h3 class="section-title">处理项蛇（受控只读）与对账</h3>
    <p class="page-desc">
      对账状态：
      <span class="status-badge" :data-status="reconcile?.ok ? '已确认' : '已撤销'">
        {{ reconcile?.ok ? '业务记录与结果明细一致' : '存在口径不一致' }}
      </span>
      ｜ 记录 {{ reconcile?.records ?? 0 }} 条，结果明细 {{ reconcile?.result_details ?? 0 }} 条
    </p>
    <table class="data-table" data-testid="snake-table">
      <thead>
        <tr>
          <th>item_key</th>
          <th>来源模块</th>
          <th>当前版本</th>
          <th>业务摘要</th>
          <th>挂接计划</th>
          <th>结果明细数</th>
          <th>导入时间</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="record in snakeRecords" :key="record.item_key" :data-testid="`snake-row-${record.item_key}`">
          <td>{{ record.item_key }}</td>
          <td>{{ record.source_module }}</td>
          <td>v{{ record.version }}</td>
          <td>{{ JSON.stringify(record.business_summary) }}</td>
          <td>{{ record.proxy_switch_plan ? String(record.proxy_switch_plan.plan_no) : '—' }}</td>
          <td>{{ record.result_details.length }}</td>
          <td>{{ formatTime(record.imported_at) }}</td>
        </tr>
        <tr v-if="!snakeRecords.length">
          <td colspan="7" class="empty-state">暂无处理项蛇记录</td>
        </tr>
      </tbody>
    </table>
  </section>
</template>

<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'

import {
  confirmSwitchPlan,
  createSwitchPlan,
  getSnakeReconcile,
  listNodeOwnership,
  listSnakeRecords,
  listSwitchPlans,
  revokeSwitchPlan,
} from '@/api/proxy-switch'
import type { NodeOwnership, SnakeRecord, SwitchPlan } from '@/api/proxy-switch'

const plans = ref<SwitchPlan[]>([])
const ownership = ref<NodeOwnership[]>([])
const snakeRecords = ref<SnakeRecord[]>([])
const reconcile = ref<Awaited<ReturnType<typeof getSnakeReconcile>> | null>(null)
const errorMessage = ref('')
const submitting = ref(false)
const operatorFlow = ref('订单采集流程')
const filters = reactive<{ owner_flow: string; node_id: string; region: string; status: string }>({
  owner_flow: '',
  node_id: '',
  region: '',
  status: '',
})
const createForm = reactive({
  anti_block_id: 3,
  owner_flow: '订单采集流程',
  switch_mode: '先切主并观察备' as '先切主并观察备' | '主备一起切换',
  nodeIdsText: '',
  snake_item_key: '',
})

function countByStatus(status: string): number {
  return plans.value.filter((plan) => plan.status === status).length
}

function isOwner(plan: SwitchPlan): boolean {
  return operatorFlow.value.trim() === plan.owner_flow
}

function formatTime(value: string): string {
  return value ? value.replace('T', ' ').slice(0, 19) : '—'
}

function resetFilters() {
  filters.owner_flow = ''
  filters.node_id = ''
  filters.region = ''
  filters.status = ''
  void reload()
}

async function reload() {
  errorMessage.value = ''
  try {
    const query: Record<string, string> = {}
    if (filters.owner_flow.trim()) query.owner_flow = filters.owner_flow.trim()
    if (filters.node_id.trim()) query.node_id = filters.node_id.trim()
    if (filters.region.trim()) query.region = filters.region.trim()
    if (filters.status) query.status = filters.status
    const [planPayload, nodePayload, snakePayload, reconcilePayload] = await Promise.all([
      listSwitchPlans(query),
      listNodeOwnership(),
      listSnakeRecords(),
      getSnakeReconcile(),
    ])
    plans.value = planPayload.items
    ownership.value = nodePayload.items
    snakeRecords.value = snakePayload.items
    reconcile.value = reconcilePayload
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '工作台数据读取失败'
  }
}

async function onCreate() {
  submitting.value = true
  errorMessage.value = ''
  try {
    const nodeIds = createForm.nodeIdsText
      .split(',')
      .map((part) => part.trim())
      .filter(Boolean)
      .map((part) => Number(part))
    await createSwitchPlan({
      anti_block_id: createForm.anti_block_id,
      owner_flow: createForm.owner_flow.trim(),
      switch_mode: createForm.switch_mode,
      affected_node_ids: nodeIds.length ? nodeIds : undefined,
      snake_item_key: createForm.snake_item_key.trim() || undefined,
    })
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '计划生成失败'
  } finally {
    submitting.value = false
  }
}

async function onConfirm(plan: SwitchPlan) {
  errorMessage.value = ''
  try {
    const result = await confirmSwitchPlan(plan.id, operatorFlow.value.trim())
    window.alert(`已确认切换，回执编号：${String(result.receipt.receipt_no)}`)
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '确认失败'
  }
}

async function onRevoke(plan: SwitchPlan) {
  errorMessage.value = ''
  const reason = window.prompt('请输入撤销原因', '所属流程主动撤销')
  if (reason === null) return
  try {
    await revokeSwitchPlan(plan.id, operatorFlow.value.trim(), reason)
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '撤销失败'
  }
}

onMounted(reload)
</script>

<style scoped>
.proxy-workbench .section-title {
  margin: 24px 0 12px;
  font-size: 16px;
}
.node-chip {
  display: inline-block;
  margin: 2px 4px 2px 0;
  padding: 2px 8px;
  border-radius: 10px;
  background: #eef2ff;
  font-size: 12px;
  white-space: nowrap;
}
.node-chip[data-role='主节点'] {
  background: #fff1f0;
}
.status-badge {
  display: inline-block;
  padding: 2px 10px;
  border-radius: 10px;
  background: #f0f0f0;
  font-size: 12px;
}
.status-badge[data-status='待确认'] {
  background: #fff7e6;
  color: #d46b08;
}
.status-badge[data-status='已确认'] {
  background: #f6ffed;
  color: #389e0d;
}
.status-badge[data-status='已撤销'] {
  background: #fff1f0;
  color: #cf1322;
}
.readonly-hint {
  color: #999;
  font-size: 12px;
}
.link.danger {
  color: #cf1322;
}
.create-panel {
  display: grid;
  grid-template-columns: repeat(2, minmax(220px, 1fr));
  gap: 12px 16px;
  padding: 16px;
  border: 1px solid #e8e8e8;
  border-radius: 8px;
}
.create-panel .btn {
  grid-column: 1 / -1;
  justify-self: start;
}
</style>
