// auth-service（nginx 路由 /api/auth/ → auth-service:8000/）
// 登录态只存 Token 与本机"记住账号"，密码永不落地；权限每次由 /me 现取。
import axios from 'axios'

export const TOKEN_KEY = 'ls_token'
export const ACCOUNT_KEY = 'ls_account'

const client = axios.create({ baseURL: '/api/auth', timeout: 20000 })

client.interceptors.request.use((cfg) => {
  const token = localStorage.getItem(TOKEN_KEY)
  if (token) cfg.headers.Authorization = `Bearer ${token}`
  return cfg
})

export async function listTenants() {
  const { data } = await client.get('/tenants')
  return data
}

export async function login(payload) {
  const { data } = await client.post('/login', payload)
  return data
}

export async function me() {
  const { data } = await client.get('/me')
  return data
}

export async function logout() {
  const { data } = await client.post('/logout')
  return data
}

export async function changePassword(payload) {
  const { data } = await client.put('/password', payload)
  return data
}
