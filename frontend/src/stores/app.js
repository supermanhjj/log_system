import { defineStore } from 'pinia'

// 全局应用状态：当前租户（驱动多租户隔离）、加载态
export const useAppStore = defineStore('app', {
  state: () => ({
    tenant: 'default', // default / acme / gp / all
    tenants: [
      { value: 'default', label: 'default（默认租户）' },
      { value: 'acme', label: 'acme' },
      { value: 'gp', label: 'gp' },
      { value: 'all', label: '全部租户' },
    ],
    loading: false,
  }),
  actions: {
    setTenant(t) {
      this.tenant = t
    },
    setLoading(v) {
      this.loading = v
    },
  },
})
