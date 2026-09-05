import { defineConfig } from '@playwright/test'

const brandedProjects = process.env.PLAYWRIGHT_BRANDED === '1'

export default defineConfig({
  testDir: './tests/e2e',
  fullyParallel: true,
  forbidOnly: Boolean(process.env.CI),
  retries: process.env.CI ? 1 : 0,
  reporter: 'line',
  use: {
    baseURL: 'http://127.0.0.1:4173',
    trace: 'retain-on-failure',
  },
  projects: brandedProjects
    ? [
        {
          name: 'chrome-1366x768',
          use: { channel: 'chrome', viewport: { width: 1366, height: 768 } },
        },
        {
          name: 'edge-1920x1080',
          use: { channel: 'msedge', viewport: { width: 1920, height: 1080 } },
        },
      ]
    : [
        {
          name: 'chromium-1366x768',
          use: { browserName: 'chromium', viewport: { width: 1366, height: 768 } },
        },
        {
          name: 'chromium-1920x1080',
          use: { browserName: 'chromium', viewport: { width: 1920, height: 1080 } },
        },
      ],
  webServer: {
    command: 'pnpm run dev',
    url: 'http://127.0.0.1:4173',
    reuseExistingServer: !process.env.CI,
  },
})
