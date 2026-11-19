// 日志空间管理（真实数据，无演示值）
// nginx /api/system/storage/* → system-service
// 后端取数：OpenSearch _cat/indices（体积/文档数/health/创建时间）、
//            _cluster/stats（节点卷容量）、date_histogram（趋势）、delete_by_query（清理），
//            PostgreSQL storage_config / tenant_quotas / storage_snapshots。
import axios from 'axios'
import { TOKEN_KEY } from './auth'

const client = axios.create({ baseURL: '/api/system/storage', timeout: 30000 })

client.interceptors.request.use((cfg) => {
  const token = localStorage.getItem(TOKEN_KEY)
  if (token) cfg.headers.Authorization = `Bearer ${token}`
  return cfg
})

export const STORAGE_RANGE_OPTIONS = ['近 7 天', '近 30 天', '近 90 天']

/** 容量概览：物理卷口径或配额口径（由配置里的 quotaGB 决定），趋势来自快照/日志分布 */
export async function getCapacity(rangeLabel = '近 7 天', tenant = '') {
  const { data } = await client.get('/capacity', { params: { range: rangeLabel, tenant } })
  return data
}

export async function getStorageConfig() {
  const { data } = await client.get('/config')
  return data
}

export async function putStorageConfig(payload) {
  const { data } = await client.put('/config', payload)
  return data
}

/** 保留期变更前的只读影响预估：查真实将要删除的索引/文档数 */
export async function getRetentionImpact(days) {
  const { data } = await client.get('/retention-impact', {
    params: { days },
    timeout: 90000, // ClickHouse 精确 count 可能扫数千万行
  })
  return data
}

export async function listIndexes(tenant = '') {
  const { data } = await client.get('/indexes', { params: { tenant } })
  return data
}

/** mode: purge 按保留期删过期文档（默认）｜drop 删除整个冷索引（仅冷索引允许） */
export async function cleanIndex(name, mode = 'purge') {
  const { data } = await client.post(`/indexes/${encodeURIComponent(name)}/clean`, { mode })
  return data
}

export async function listTenantQuotas() {
  const { data } = await client.get('/quotas')
  return data
}

export async function putTenantQuota(tenant, quotaGB) {
  const { data } = await client.put(`/quotas/${encodeURIComponent(tenant)}`, { quotaGB })
  return data
}
