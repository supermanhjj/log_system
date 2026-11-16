import { defineStore } from 'pinia'

// 全局应用状态：当前租户（驱动多租户隔离）、加载态
export const useAppStore = defineStore('app', {
  state: () => ({
    tenant: 'default', // default / acme / gp / all
    // 标签与平台管理「租户」表、登录页下拉保持一致（值不变，仅显示名对齐）
    tenants: [
      { value: 'default', label: '总部生产' },
      { value: 'acme', label: '省侧运维' },
      { value: 'gp', label: '测试租户' },
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
