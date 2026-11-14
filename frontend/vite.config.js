import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// 开发期：/api 反代到 nginx(:8080) → 各后端微服务；/osd 反代到 Dashboards。
// 生产期：nginx 直接托管 dist 并把 /api 转给微服务（见 docker-compose.prod.yml）。
export default defineConfig({
  base: '/',
  plugins: [vue()],
  server: {
    port: 5173,
    proxy: {
      '/api': { target: 'http://localhost:8080', changeOrigin: true },
      '/osd': { target: 'http://localhost:8080', changeOrigin: true, ws: true },
    },
  },
  build: {
    outDir: 'dist',
  },
})
