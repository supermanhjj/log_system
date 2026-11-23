// system-service 自监测管理：组件健康 / 采集链路诊断 / 全链路拓扑
import axios from 'axios'
import { attachAuth } from './http'

const client = axios.create({ baseURL: '/api/system/monitor', timeout: 45000 })
client.interceptors.request.use(attachAuth)

function toQuery(params) {
  const q = {}
  Object.entries(params || {}).forEach(([k, v]) => {
    if (v !== '' && v !== null && v !== undefined) q[k] = v
  })
  return q
}

export async function getMonitorHealth() {
  const { data } = await client.get('/health')
  return data
}

export async function getMonitorTopology(tenant) {
  const { data } = await client.get('/topology', { params: toQuery({ tenant }) })
  return data
}

export async function listMonitorCollectors(tenant) {
  const { data } = await client.get('/collectors', { params: toQuery({ tenant }) })
  return data
}

export async function runMonitorDiagnose(payload) {
  const { data } = await client.post('/diagnose', payload)
  return data
}
