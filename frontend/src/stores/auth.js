// 登录态：Token + 当前用户 + 权限点。
// 权限不缓存在侧边栏，统一走 has(resource, action)；后端每次请求现查权限表，
// 所以管理员改完角色权限，用户下一次点击即生效（无需重新登录）。
import { defineStore } from 'pinia'
import { ACCOUNT_KEY, TOKEN_KEY, changePassword, login, logout, me } from '../api/auth'

export const useAuthStore = defineStore('auth', {
  state: () => ({
    token: localStorage.getItem(TOKEN_KEY) || '',
    user: null,
    permissions: [],
    passwordRule: '',
    loaded: false,
  }),
  getters: {
    isLoggedIn: (s) => !!s.token && !!s.user,
    displayName: (s) => s.user?.name || '',
    roleName: (s) => s.user?.roleName || '',
    tenant: (s) => s.user?.tenant || '',
    tenantName: (s) => s.user?.tenantName || '',
  },
  actions: {
    has(resource, action = 'view') {
      return this.permissions.includes(`${resource}:${action}`)
    },
    hasAny(resource, actions = []) {
      return actions.some((a) => this.has(resource, a))
    },
    setSession(data) {
      this.token = data.token || ''
      if (this.token) localStorage.setItem(TOKEN_KEY, this.token)
      this.user = data.user || null
      this.permissions = data.permissions || []
      this.loaded = true
    },
    async login({ account, password, tenant, remember }) {
      const data = await login({ account, password, tenant, remember: !!remember })
      this.setSession(data)
      if (remember) localStorage.setItem(ACCOUNT_KEY, account)
      else localStorage.removeItem(ACCOUNT_KEY)
      return data
    },
    async fetchMe() {
      const data = await me()
      this.user = data.user
      this.permissions = data.permissions || []
      this.passwordRule = data.passwordRule || ''
      this.loaded = true
      return data
    },
    async logout() {
      try {
        await logout()
      } catch {
        // 登出失败不阻塞本地清理：Token 到期或被停用时正是这种场景
      }
      this.clear()
    },
    async changePassword(payload) {
      return changePassword(payload)
    },
    clear() {
      this.token = ''
      this.user = null
      this.permissions = []
      this.loaded = false
      localStorage.removeItem(TOKEN_KEY)
    },
    rememberedAccount() {
      return localStorage.getItem(ACCOUNT_KEY) || ''
    },
  },
})
