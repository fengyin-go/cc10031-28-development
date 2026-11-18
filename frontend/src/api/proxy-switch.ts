/** 代理切换数据能力与处理项蛇的接口封装。 */
import { request } from './client'

export type SwitchMode = '主备一起切换' | '先切主并观察备'

export type AffectedNode = {
  node_id: number
  node_name: string
  region: string
  role: '主节点' | '备用节点'
  action: '切换' | '观察'
  status: string
  [key: string]: string | number | boolean | null
}

export type SwitchPlan = {
  id: number
  plan_no: string
  status: '待确认' | '已确认' | '已撤销'
  active: boolean
  owner_flow: string
  source: string
  anti_block_id: number
  strategy_name: string
  site: string
  affected_regions: string[]
  switch_mode: SwitchMode
  affected_nodes: AffectedNode[]
  target_node_ids: number[]
  observe_node_ids: number[]
  snake_item_key: string | null
  snake_version: number | null
  receipt: Record<string, unknown> | null
  created_at: string
  updated_at: string
  [key: string]: unknown
}

export type NodeOwnership = {
  node_id: number
  node_name: string
  region: string
  status: string
  occupied: boolean
  owner_flow: string | null
  active_plan_no: string | null
  active_plan_id: number | null
}

async function readJson<T>(response: Response): Promise<T> {
  if (!response.ok) {
    const body = (await response.json().catch(() => ({}))) as { detail?: string }
    throw new Error(body.detail ?? `接口返回 ${response.status}`)
  }
  return (await response.json()) as T
}

export async function triggerBlock(antiBlockId: number): Promise<unknown> {
  const response = await request(`/api/anti-block/${antiBlockId}/trigger-block`, { method: 'POST' })
  return readJson(response)
}

export async function parseBlockEvent(payload: {
  anti_block_id: number
  switch_mode: SwitchMode
  affected_node_ids?: number[]
  regions?: string[]
}): Promise<{ ok: boolean; event: Record<string, unknown> }> {
  const response = await request('/api/proxy-switch/parse-block', {
    method: 'POST',
    body: JSON.stringify(payload),
  })
  return readJson(response)
}

export async function listSwitchPlans(filters: {
  owner_flow?: string
  node_id?: number
  region?: string
  active_only?: boolean
  status?: string
}): Promise<{ items: SwitchPlan[]; total: number }> {
  const query = new URLSearchParams()
  Object.entries(filters).forEach(([key, value]) => {
    if (value !== undefined && value !== '' && value !== null) query.set(key, String(value))
  })
  const response = await request(`/api/proxy-switch/plans?${query.toString()}`)
  return readJson(response)
}

export async function createSwitchPlan(payload: {
  anti_block_id: number
  owner_flow: string
  switch_mode: SwitchMode
  affected_node_ids?: number[]
  regions?: string[]
  snake_item_key?: string
}): Promise<{ ok: boolean; message: string; plan: SwitchPlan }> {
  const response = await request('/api/proxy-switch/plans', {
    method: 'POST',
    body: JSON.stringify(payload),
  })
  return readJson(response)
}

export async function confirmSwitchPlan(
  planId: number,
  operatorFlow: string,
): Promise<{ ok: boolean; message: string; plan: SwitchPlan; receipt: Record<string, unknown> }> {
  const response = await request(`/api/proxy-switch/plans/${planId}/confirm`, {
    method: 'POST',
    body: JSON.stringify({ operator_flow: operatorFlow }),
  })
  return readJson(response)
}

export async function revokeSwitchPlan(
  planId: number,
  operatorFlow: string,
  reason: string,
): Promise<{ ok: boolean; message: string; plan: SwitchPlan }> {
  const response = await request(`/api/proxy-switch/plans/${planId}/revoke`, {
    method: 'POST',
    body: JSON.stringify({ operator_flow: operatorFlow, reason }),
  })
  return readJson(response)
}

export async function listNodeOwnership(): Promise<{ items: NodeOwnership[]; total: number }> {
  const response = await request('/api/proxy-switch/nodes')
  return readJson(response)
}

export async function listSwitchReceipts(
  ownerFlow?: string,
): Promise<{ items: Record<string, unknown>[]; total: number }> {
  const query = ownerFlow ? `?owner_flow=${encodeURIComponent(ownerFlow)}` : ''
  const response = await request(`/api/proxy-switch/receipts${query}`)
  return readJson(response)
}

export type SnakeRecord = {
  item_id: number
  item_key: string
  version: number
  source_module: string
  data: Record<string, unknown>
  business_summary: Record<string, unknown>
  result_details: Record<string, unknown>[]
  proxy_switch_plan: Record<string, unknown> | null
  imported_at: string
  updated_at: string
}

export async function listSnakeRecords(): Promise<{ items: SnakeRecord[]; total: number }> {
  const response = await request('/api/processing-snake/records')
  return readJson(response)
}

export async function getSnakeReconcile(): Promise<{
  ok: boolean
  records: number
  result_details: number
  mismatches: Record<string, unknown>[]
}> {
  const response = await request('/api/processing-snake/reconcile')
  return readJson(response)
}
