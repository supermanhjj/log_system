import { defineStore } from 'pinia'
import { listTenants } from '../api/system'
import { TENANT_KEY } from '../api/http'

// 全局应用状态：当前租户（驱动多租户隔离）、加载态
//
// 租户清单不再写死在前端：以服务端 tenants 表为唯一真源，登录后由
// loadTenants() 拉取。新增/停用租户只改管理页面，前端无需重新发版。
// 注意：前端这层只决定「下拉里能看到什么」；真正的隔离由后端从登录态强制解析。
export const useAppStore = defineStore('app', {
  state: () => ({
    tenant: localStorage.getItem(TENANT_KEY) || 'i6000',
    tenants: [{ value: 'i6000', label: 'I6000 系统' }],
    tenantsLoaded: false,
    loading: false,
  }),
  actions: {
    setTenant(t) {
      this.tenant = t
      // 持久化：刷新后保持选择，也供 api/http.js 的拦截器读取
      localStorage.setItem(TENANT_KEY, t)
    },
    setLoading(v) {
      this.loading = v
    },
    async loadTenants({ includeAll = false } = {}) {
      try {
        const data = await listTenants()
        const items = (data.items || [])
          .filter((t) => t.enabled)
          // 顶栏只列真实业务系统租户。unknown 已随「归属必须清晰」下线（无法归属的
          // 日志在入口直接拒收），这里保留过滤是为了防御历史库/灰度环境的残留值。
          .filter((t) => t.code !== 'unknown')
          .map((t) => ({ value: t.code, label: t.name }))
        if (includeAll) items.push({ value: 'all', label: '全部租户' })
        if (items.length) {
          this.tenants = items
          this.tenantsLoaded = true
          // 当前租户已不存在（被停用/删除）时回落到第一项，避免查询打到无效索引
          if (!items.some((t) => t.value === this.tenant)) this.setTenant(items[0].value)
        }
      } catch {
        // 拉取失败保留现有列表，不阻塞页面（后端仍会做强制隔离）
      }
      return this.tenants
    },
  },
})
