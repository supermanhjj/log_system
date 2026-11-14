// ingest-service（nginx 路由 /api/data/ → ingest-service:8000/）
//
// 数据源列表 = OpenSearch 真实日志来源自动发现 + PostgreSQL 登记配置合并，
// 全部指标由后端实时聚合；前端不含任何静态样例数据。
import axios from 'axios'

const client = axios.create({ baseURL: '/api/data', timeout: 30000 })

export const INGEST_TYPES = ['Agent', 'Syslog', 'HTTP', 'Kafka', '自定义']
export const STATUS_OPTIONS = ['运行中', '告警中', '异常', '离线', '维护中', '待接入']
export const ROLE_LABELS = {
  source: '来源字段',
  time: '时间字段',
  level: '级别字段',
  host: '主机字段',
  tenant: '租户字段',
  message: '正文字段',
}

export async function getMeta(tenant = 'default') {
  const { data } = await client.get('/meta', { params: { tenant } })
  return data
}

export async function listSources(params = {}) {
  const { data } = await client.get('/sources', { params })
  return data
}

export async function getSource(key, tenant = 'default') {
  const { data } = await client.get(`/sources/${encodeURIComponent(key)}`, { params: { tenant } })
  return data
}

export async function createSource(payload) {
  const { data } = await client.post('/sources', payload)
  return data
}

export async function updateSource(key, payload) {
  const { data } = await client.put(`/sources/${encodeURIComponent(key)}`, payload)
  return data
}

export async function deleteSource(key, tenant = 'default') {
  const { data } = await client.delete(`/sources/${encodeURIComponent(key)}`, { params: { tenant } })
  return data
}

export async function batchStatus(keys, status, tenant = 'default') {
  const { data } = await client.post('/sources/batch-status', { keys, status, tenant })
  return data
}

export async function testConnectivity(payload) {
  const { data } = await client.post('/sources/test', payload)
  return data
}

export async function exportConfig(tenant = 'default') {
  const resp = await client.get('/export', { params: { tenant }, responseType: 'blob' })
  const disposition = resp.headers['content-disposition'] || ''
  const m = /filename="?([^"]+)"?/.exec(disposition)
  const name = m ? m[1] : `data-sources-${tenant}.json`
  const url = URL.createObjectURL(resp.data)
  const a = document.createElement('a')
  a.href = url
  a.download = name
  a.click()
  URL.revokeObjectURL(url)
  return name
}

export async function getSettings() {
  const { data } = await client.get('/settings')
  return data
}

export async function putSettings(settings) {
  const { data } = await client.put('/settings', { settings })
  return data
}
