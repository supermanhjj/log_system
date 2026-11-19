// 服务端解析管线管理（仅平台管理员写）
// nginx /api/pipelines/* → ingest-service
import axios from 'axios'
import { TOKEN_KEY } from './auth'

const client = axios.create({ baseURL: '/api/pipelines', timeout: 30000 })

client.interceptors.request.use((cfg) => {
  const token = localStorage.getItem(TOKEN_KEY)
  if (token) cfg.headers.Authorization = `Bearer ${token}`
  return cfg
})

export const getPipelineStatus = async () => (await client.get('/status')).data
export const listParsers = async () => (await client.get('/parsers')).data
export const saveParser = async (payload) => (await client.post('/parsers', payload)).data
export const deleteParser = async (code) => (await client.delete(`/parsers/${encodeURIComponent(code)}`)).data
export const listRules = async () => (await client.get('/rules')).data
export const saveRule = async (payload) => (await client.post('/rules', payload)).data
export const deleteRule = async (code) => (await client.delete(`/rules/${encodeURIComponent(code)}`)).data

/** 贴一段真实日志试解析，未保存也能试 */
export const previewParse = async (payload) => (await client.post('/preview', payload)).data

/** 历史重放；dryRun=true 只看结果不写库 */
export const replayLogs = async (payload) =>
  (await client.post('/replay', payload, { timeout: 120000 })).data

/** 采集器凭证与租户白名单（key 只在新建时提交，数据库只存哈希） */
export const listCollectors = async () => (await client.get('/collectors')).data
export const saveCollector = async (payload) => (await client.post('/collectors', payload)).data

/** 租户伪造 / 越界审计记录 */
export const securityAudit = async (limit = 100) =>
  (await client.get('/security-audit', { params: { limit } })).data

export const PARSER_TYPES = ['grok', 'json', 'regex', 'delimiter', 'kv']
