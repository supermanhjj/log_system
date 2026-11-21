// agent-service（nginx 路由 /api/agents/ → agent-service:8000/）
//
// Agent 主机来自真实心跳自动注册，状态由 Redis 心跳 + 超时判定，采集速率由
// OpenSearch 真实日志反推；重启/升级为真实指令队列，不含任何模拟进度。
import axios from 'axios'
import { attachAuth, currentTenant } from './http'

const client = axios.create({ baseURL: '/api/agents', timeout: 30000 })

// Token + 当前租户：租户由服务端从登录态强制解析，越权由后端 403（前端只传意图）
client.interceptors.request.use(attachAuth)

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

export async function installConfig(hostname, tenant = currentTenant()) {
  const resp = await client.get('/install-config', {
    params: { hostname, tenant }, responseType: 'blob',
  })
  return downloadBlob(resp, `fluent-bit-${hostname}.conf`)
}

function downloadBlob(resp, fallback) {
  const disposition = resp.headers['content-disposition'] || ''
  const m = /filename="?([^"]+)"?/.exec(disposition)
  const name = m ? m[1] : fallback
  const url = URL.createObjectURL(resp.data)
  const a = document.createElement('a')
  a.href = url
  a.download = name
  a.click()
  URL.revokeObjectURL(url)
  return name
}

// 下载接入包（tar.gz）：可同时签发凭证、登记主机台账。
// 凭证明文只出现在响应头里，且只有「本次签发」才会返回。
export async function installPackage(payload, tenant = currentTenant()) {
  const resp = await client.post('/install-package', payload, {
    params: { tenant }, responseType: 'blob', timeout: 60000,
  })
  return {
    name: downloadBlob(resp, `ruilog-agent-${payload.hostname}.tar.gz`),
    code: resp.headers['x-collector-code'] || '',
    key: resp.headers['x-collector-key'] || '',
    ledger: resp.headers['x-host-ledger'] || 'skipped',
    variant: resp.headers['x-package-variant'] || payload.variant,
  }
}

export async function listCollectorCredentials() {
  const { data } = await client.get('/collector-credentials')
  return data
}

export async function revokeCollectorCredential(code) {
  const { data } = await client.delete(`/collector-credentials/${encodeURIComponent(code)}`)
  return data
}
