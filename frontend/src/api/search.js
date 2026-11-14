import axios from 'axios'

// search-service（nginx 路由 /api/search/ → search-service:8000/）
const client = axios.create({ baseURL: '/api/search', timeout: 20000 })

export async function searchQuery(payload) {
  const { data } = await client.post('/query', payload)
  return data
}

export async function searchHistogram(payload) {
  const { data } = await client.post('/histogram', payload)
  return data
}

export async function searchPql(payload) {
  const { data } = await client.post('/pql', payload)
  return data
}

export async function searchContext(payload) {
  const { data } = await client.post('/context', payload)
  return data
}
