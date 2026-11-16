// system-service（nginx 路由 /api/system/ → system-service:8000/）
// 平台管理：用户 / 角色权限 / 系统参数 / 审计日志。所有请求需带登录 Token。
import axios from 'axios'
import { TOKEN_KEY } from './auth'

const client = axios.create({ baseURL: '/api/system', timeout: 30000 })

client.interceptors.request.use((cfg) => {
  const token = localStorage.getItem(TOKEN_KEY)
  if (token) cfg.headers.Authorization = `Bearer ${token}`
  return cfg
})

function toQuery(params) {
  const q = {}
  Object.entries(params || {}).forEach(([k, v]) => {
    if (v !== '' && v !== null && v !== undefined) q[k] = v
  })
  return q
}

export async function getMeta() {
  const { data } = await client.get('/meta')
  return data
}

export async function listUsers(params) {
  const { data } = await client.get('/users', { params: toQuery(params) })
  return data
}

export async function createUser(payload) {
  const { data } = await client.post('/users', payload)
  return data
}

export async function updateUser(id, payload) {
  const { data } = await client.put(`/users/${id}`, payload)
  return data
}

export async function toggleUser(id, enabled) {
  const { data } = await client.put(`/users/${id}/enabled`, { enabled })
  return data
}

export async function resetUserPassword(id) {
  const { data } = await client.post(`/users/${id}/reset-password`)
  return data
}

export async function batchEnabled(ids, enabled) {
  const { data } = await client.post('/users/batch/enabled', { ids, enabled })
  return data
}

export async function batchResetPassword(ids) {
  const { data } = await client.post('/users/batch/reset-password', { ids })
  return data
}

export async function listRoles() {
  const { data } = await client.get('/roles')
  return data
}

export async function updateRole(key, payload) {
  const { data } = await client.put(`/roles/${key}`, payload)
  return data
}

export async function getParams() {
  const { data } = await client.get('/params')
  return data
}

export async function putParams(items) {
  const { data } = await client.put('/params', { items })
  return data
}

export async function listAudit(params) {
  const { data } = await client.get('/audit', { params: toQuery(params) })
  return data
}

// CSV 导出：后端已带 BOM，前端只负责落盘
async function saveCsv(url, params, filename) {
  const res = await client.get(url, { params: toQuery(params), responseType: 'blob' })
  const blob = new Blob([res.data], { type: 'text/csv;charset=utf-8' })
  const link = document.createElement('a')
  link.href = URL.createObjectURL(blob)
  link.download = filename
  document.body.appendChild(link)
  link.click()
  document.body.removeChild(link)
  URL.revokeObjectURL(link.href)
}

export function exportUsersCsv(params) {
  return saveCsv('/users/export.csv', params, 'users.csv')
}

export function exportAuditCsv(params) {
  return saveCsv('/audit/export.csv', params, 'audit-log.csv')
}
