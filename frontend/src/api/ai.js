// ai-service（nginx 路由 /api/ai/ → ai-service:8000/）
import axios from 'axios'

const client = axios.create({ baseURL: '/api/ai', timeout: 30000 })

export const AI_KINDS = [
  { key: 'commercial', label: '商用 API' },
  { key: 'local', label: '本地自定义' },
  { key: 'cli', label: 'WorkBuddy CLI' },
]

export const GRAMMAR_CATEGORIES = [
  { key: 'command', label: '命令白名单' },
  { key: 'field_alias', label: '字段别名' },
  { key: 'term_mapping', label: '业务词映射' },
  { key: 'time_rule', label: '时间规则' },
  { key: 'example', label: 'NL→PPL 示例' },
  { key: 'deny', label: '禁止项' },
]

export async function getAIMeta() {
  const { data } = await client.get('/meta')
  return data
}

export async function listProviders(kind = '') {
  const { data } = await client.get('/providers', { params: kind ? { kind } : {} })
  return data
}

export async function createProvider(payload) {
  const { data } = await client.post('/providers', payload)
  return data
}

export async function updateProvider(id, payload) {
  const { data } = await client.put(`/providers/${id}`, payload)
  return data
}

export async function deleteProvider(id) {
  const { data } = await client.delete(`/providers/${id}`)
  return data
}

export async function setDefaultProvider(id) {
  const { data } = await client.post(`/providers/${id}/default`)
  return data
}

export async function testProvider(id, payload) {
  const { data } = await client.post(`/providers/${id}/test`, payload)
  return data
}

export async function listGrammar(params = {}) {
  const { data } = await client.get('/grammar', { params })
  return data
}

export async function createGrammar(payload) {
  const { data } = await client.post('/grammar', payload)
  return data
}

export async function updateGrammar(id, payload) {
  const { data } = await client.put(`/grammar/${id}`, payload)
  return data
}

export async function deleteGrammar(id) {
  const { data } = await client.delete(`/grammar/${id}`)
  return data
}

export async function importGrammar(payload) {
  const { data } = await client.post('/grammar/import', payload)
  return data
}

export async function exportGrammar() {
  const resp = await client.get('/grammar/export', { responseType: 'blob' })
  const url = URL.createObjectURL(new Blob([resp.data], { type: 'application/json' }))
  const a = document.createElement('a')
  a.href = url
  a.download = 'ai-grammar-rules.json'
  a.click()
  URL.revokeObjectURL(url)
}

export async function syncFields(tenant = 'default') {
  const { data } = await client.post('/grammar/sync-fields', null, { params: { tenant } })
  return data
}

export async function previewGrammar(question) {
  const { data } = await client.post('/grammar/preview', { question })
  return data
}

export async function listCapabilities() {
  const { data } = await client.get('/capabilities')
  return data
}

export async function updateCapability(key, payload) {
  const { data } = await client.put(`/capabilities/${encodeURIComponent(key)}`, payload)
  return data
}
