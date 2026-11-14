// dashboard-service（nginx 路由 /api/dashboards/ → dashboard-service:8000/）
//
// 运行总览全部指标由后端对 OpenSearch 日志索引（logs-*）实时聚合得出，
// 不再使用任何前端硬编码 / 模拟数据。后端端点：GET /overview?range=&tenant=
import axios from 'axios'

const client = axios.create({ baseURL: '/api/dashboards', timeout: 30000 })

export const RANGE_OPTIONS = ['今日', '近 1 小时', '近 24 小时', '近 7 天', '近 30 天']

export async function getOverview(rangeLabel = '今日', tenant = 'default', force = false) {
  const { data } = await client.get('/overview', {
    params: { range_in: rangeLabel, tenant, force: force || undefined },
  })
  return data
}
