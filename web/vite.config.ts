import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// 浏览器只请求 Vite 的 /api；Vite 再转发给本机 FastAPI，保留原有路径。
export default defineConfig({
  plugins: [react()],
  server: {
    host: '127.0.0.1',
    port: Number(process.env.JOBPILOT_WEB_PORT ?? 5174),
    strictPort: true,
    proxy: {
      '/api': `http://127.0.0.1:${process.env.UVICORN_PORT ?? 8000}`,
    },
  },
})
