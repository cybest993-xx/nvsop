/// <reference types="vitest/config" />
import { fileURLToPath, URL } from 'node:url'

import vue from '@vitejs/plugin-vue'
import { defineConfig } from 'vite'

// 开发期间连接中心后台。部署时没有代理：Nginx 提供构建产物，并在同一来源下将 `/api/v1`
// 转发到 FastAPI（§六），从而允许会话 cookie 使用 `SameSite=strict`。代理只为让
// `vite dev` 呈现同一来源；否则浏览器会把 API 视为跨站请求并丢弃 cookie。
const BACKEND = process.env.SOP_BACKEND_ORIGIN ?? 'http://127.0.0.1:8000'

export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    proxy: {
      '/api': { target: BACKEND, changeOrigin: false },
    },
  },
  build: {
    // Element Plus 的共享 chunk 约为 700 kB；保留阈值可让 reporter 继续暴露更大的异常增长。
    chunkSizeWarningLimit: 800,
  },
  test: {
    environment: 'jsdom',
    // 组件测试会导入 Element Plus，其样式属于副作用导入；测试不需要处理 CSS。
    css: false,
    include: ['tests/integration/**/*.spec.ts'],
  },
})
