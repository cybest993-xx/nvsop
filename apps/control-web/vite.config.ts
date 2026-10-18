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
    // 按新库实测：最大 chunk 是设备页 156 kB（2026-10 迁移后实测），其余在 145 kB 以下。
    // 阈值留出余量，让 reporter 对真正的异常增长报警。
    chunkSizeWarningLimit: 200,
  },
  test: {
    environment: 'jsdom',
    // Naive UI 的样式是运行时注入的 CSS-in-JS，测试不导入任何 CSS 文件；跳过样式处理。
    css: false,
    include: ['tests/integration/**/*.spec.ts'],
  },
})
