// alarm-service（nginx 路由 /api/alarms/ → alarm-service:8000/）
//
// 告警中心全部数据均来自真实后端：
//  - 告警规则：PostgreSQL 持久化（CRUD + 启用开关）
//  - 告警事件：由后端对 OpenSearch 日志索引（logs-*）按规则实时判定得出，无前端硬编码
import axios from 'axios'

const client = axios.create({ baseURL: '/api/alarms', timeout: 30000 })

// 真实业务数据源（与日志采集端 SOURCES 对齐），供「关联数据源」下拉
export const REAL_SOURCES = [
  'order-service',
  'user-service',
  'payment-gateway',
  'inventory-svc',
  'api-gateway',
]

export const LEVELS = ['严重', '警告', '提示']
export const STATUS_OPTIONS = ['告警中', '已恢复', '维护中']
export const NOTIFY_OPTIONS = [
  { value: 'wecom', label: '企业微信' },
  { value: 'sms', label: '短信' },
  { value: 'email', label: '邮件' },
]

// 由规则结构化字段生成「判定条件」可读文案，避免后端拼接、保持前后端一致
export function conditionText(r) {
  const parts = []
  if (r.matchLevel) parts.push(`level:${r.matchLevel}`)
  if (r.matchStatusGte != null) parts.push(`status≥${r.matchStatusGte}`)
  if (r.keyword) parts.push(`包含"${r.keyword}"`)
  if (r.matchSource) parts.push(`source:${r.matchSource}`)
  const cond = parts.length ? parts.join(' 且 ') + '，' : '任意日志，'
  return `${cond}${r.windowMinutes} 分钟内 ≥ ${r.threshold} 条即触发`
}

export function notifyLabels(notify = []) {
  return notify.map((n) => (NOTIFY_OPTIONS.find((o) => o.value === n) || { label: n }).label).join(' + ')
}

// ------------------------------- 告警事件 -------------------------------
export async function listEvents(params = {}) {
  const { data } = await client.get('/events', { params })
  return data
}

export async function closeEvents(ids = [], reason = '', handler = '') {
  const { data } = await client.post('/events/close', { ids, reason, handler })
  return data
}

// 由后端「分析当前规则配置 + 索引真实字段」后生成的关联日志 PPL（自适应，前端不拼模板）
export async function getRelatedQuery(eventId, tenant = 'default') {
  const { data } = await client.get(`/events/${encodeURIComponent(eventId)}/related-query`, {
    params: { tenant },
  })
  return data
}

export async function closeEvent(eventId, reason = '', handler = '') {
  const { data } = await client.post(`/events/${encodeURIComponent(eventId)}/close`, { reason, handler })
  return data
}

export async function maintainEvent(eventId, reason = '', handler = '') {
  const { data } = await client.post(`/events/${encodeURIComponent(eventId)}/maintain`, { reason, handler })
  return data
}

// ------------------------------- 告警规则 -------------------------------
export async function listRules(params = {}) {
  const { data } = await client.get('/rules', { params })
  return data
}

export async function getRule(id) {
  const { data } = await client.get(`/rules/${id}`)
  return data
}

export async function createRule(payload) {
  const { data } = await client.post('/rules', payload)
  return data
}

export async function updateRule(id, payload) {
  const { data } = await client.put(`/rules/${id}`, payload)
  return data
}

export async function deleteRule(id) {
  const { data } = await client.delete(`/rules/${id}`)
  return data
}

export async function toggleRule(id, enabled) {
  const { data } = await client.post(`/rules/${id}/toggle`, { enabled })
  return data
}
