// 统一请求拦截：注入登录 Token，并只在「租户域接口」上补当前租户
//
// 为什么不能所有请求都补 tenant：
//   平台管理类接口（/api/system/*）里的 tenant 是**筛选条件**，不是隔离边界。
//   若把顶栏选的 "all"（全部租户）塞给 /system/users，后端会按 t.code='all'
//   去匹配真实租户编码，结果恒为 0 条 —— 表现就是「用户管理页空白，刷新后又有了」
//   （刷新时路由守卫用 JWT 里的真实租户覆盖了 all）。所以这里按作用域区分。
//
// 租户域接口仍由服务端按 JWT 强制解析（各服务 authz.py 的 resolve_tenant）：
// 前端传的只是「查看意图」，改 localStorage 也只能拿到 403，不会越权。
import { TOKEN_KEY } from './auth'

// 当前租户持久化键：刷新页面后保持用户选择，避免回落到 default 造成误解
export const TENANT_KEY = 'ruilog.currentTenant'

// 真正做到「按租户隔离数据」的服务前缀。只有这些接口才自动补 tenant。
const TENANT_SCOPED_PREFIXES = ['/api/search', '/api/dashboards', '/api/alarms', '/api/data']

export function currentTenant() {
  return localStorage.getItem(TENANT_KEY) || 'default'
}

function isTenantScoped(cfg) {
  const base = cfg.baseURL || ''
  if (TENANT_SCOPED_PREFIXES.some((p) => base === p || base.startsWith(p + '/'))) return true
  const url = String(cfg.url || '')
  return TENANT_SCOPED_PREFIXES.some((p) => url.startsWith(p))
}

/**
 * axios 请求拦截器实现体。
 *  - 始终注入 Authorization
 *  - 仅租户域接口补租户；调用方显式传了 tenant（含空串表示全部）则不覆盖
 */
export function attachAuth(cfg) {
  const token = localStorage.getItem(TOKEN_KEY)
  if (token) cfg.headers.Authorization = `Bearer ${token}`
  if (!isTenantScoped(cfg)) return cfg
  cfg.params = { ...(cfg.params || {}) }
  if (!Object.prototype.hasOwnProperty.call(cfg.params, 'tenant')) {
    cfg.params.tenant = currentTenant()
  }
  return cfg
}
