/// <reference types="vitest/config" />
import { fileURLToPath, URL } from 'node:url'

import vue from '@vitejs/plugin-vue'
import { defineConfig } from 'vite'

// The center backend during development. In the deployment there is no proxy: Nginx serves the
// built assets and routes `/api/v1` to FastAPI on one origin (§六), which is what lets the
// session cookie be `SameSite=strict`. The proxy exists so `vite dev` presents that same single
// origin — without it the browser would treat the API as cross-site and drop the cookie.
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
  test: {
    environment: 'jsdom',
    // Component tests import Element Plus, whose styles are side-effect imports.
    css: false,
    include: ['tests/integration/**/*.spec.ts'],
  },
})
