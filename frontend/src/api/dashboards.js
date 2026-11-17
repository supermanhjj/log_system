// 仪表盘配置与图表取数（nginx /api/dashboards/ → dashboard-service:8000/）
//
// 配置存 PG，图表数据按 widget 维度配置实时聚合 OpenSearch(logs-*)：
// 列表/详情/新建/保存/删除走 /dashboards*；取数走 POST /query；字典走 /dashboards/meta。
import axios from 'axios'

const client = axios.create({ baseURL: '/api/dashboards', timeout: 30000 })

// —— 配置 CRUD ——
export function listDashboards() {
  return client.get('/dashboards').then((r) => r.data.items)
}

export function getDashboard(id) {
  return client.get(`/dashboards/${id}`).then((r) => r.data)
}

export function createDashboard(payload) {
  return client.post('/dashboards', payload).then((r) => r.data)
}

export function updateDashboard(id, payload) {
  return client.put(`/dashboards/${id}`, payload).then((r) => r.data)
}

export function deleteDashboard(id) {
  return client.delete(`/dashboards/${id}`).then((r) => r.data)
}

// —— 字典：图表类型 / 维度 / 时间范围 ——
export function getDashboardMeta() {
  return client.get('/dashboards/meta').then((r) => r.data)
}

// —— 按配置聚合图表数据 ——
// payload: { range_in, tenant, widgets:[...], force? }
export function queryWidgets(payload) {
  return client.post('/query', payload).then((r) => r.data)
}
