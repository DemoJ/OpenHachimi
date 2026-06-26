import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// 后端默认监听 127.0.0.1:8765，dev 模式把 API 路径代理过去，
// 这样前端 fetch 写相对路径即可，build 后也无需改动。
const BACKEND = 'http://127.0.0.1:8765'

const apiPaths = [
  '/health',
  '/state',
  '/roles',
  '/config',
  '/chat',
  '/new',
  '/role',
  '/stop',
  '/commands',
  '/session',
  '/sessions',
  '/artifacts',
  '/schedules',
]

export default defineConfig({
  plugins: [vue()],
  base: '/ui/',
  build: {
    outDir: '../openhachimi_agent/webui_dist',
    emptyOutDir: true,
  },
  server: {
    port: 5173,
    proxy: Object.fromEntries(
      apiPaths.map((p) => [p, { target: BACKEND, changeOrigin: true }]),
    ),
  },
})
