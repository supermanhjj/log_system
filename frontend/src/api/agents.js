// agent-service（nginx 路由 /api/agents/ → agent-service:8000/）
//
// Agent 主机来自真实心跳自动注册，状态由 Redis 心跳 + 超时判定，采集速率由
// OpenSearch 真实日志反推；重启/升级为真实指令队列，不含任何模拟进度。
import axios from 'axios'

const client = axios.create({ baseURL: '/api/agents', timeout: 30000 })

export const AGENT_STATUS_OPTIONS = ['运行中', '告警中', '离线', '维护中', '异常']

export async function listAgents(params = {}) {
  const { data } = await client.get('/agents', { params })
  return data
}

export async function getAgent(hostname) {
  const { data } = await client.get(`/agents/${encodeURIComponent(hostname)}`)
  return data
}

export async function updateAgent(hostname, payload) {
  const { data } = await client.put(`/agents/${encodeURIComponent(hostname)}`, payload)
  return data
}

export async function sendCommand(hostname, type, payload = {}, note = '') {
  const { data } = await client.post(`/agents/${encodeURIComponent(hostname)}/command`,
    { type, payload, note })
  return data
}

export async function setAgentStatus(hostname, status) {
  const { data } = await client.post(`/agents/${encodeURIComponent(hostname)}/status`, { status })
  return data
}

export async function listUpgradeTasks(limit = 10) {
  const { data } = await client.get('/upgrade-tasks', { params: { limit } })
  return data
}

export async function createUpgradeTask(payload) {
  const { data } = await client.post('/upgrade-tasks', payload)
  return data
}

export async function cancelUpgradeTask(id) {
  const { data } = await client.post(`/upgrade-tasks/${id}/cancel`)
  return data
}

export async function installConfig(hostname, tenant = 'default') {
  const resp = await client.get('/install-config', {
    params: { hostname, tenant }, responseType: 'blob',
  })
  const disposition = resp.headers['content-disposition'] || ''
  const m = /filename="?([^"]+)"?/.exec(disposition)
  const name = m ? m[1] : `fluent-bit-${hostname}.conf`
  const url = URL.createObjectURL(resp.data)
  const a = document.createElement('a')
  a.href = url
  a.download = name
  a.click()
  URL.revokeObjectURL(url)
  return name
}
