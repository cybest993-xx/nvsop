import { defineConfig } from '@playwright/test'

const brandedProjects = process.env.PLAYWRIGHT_BRANDED === '1'
const fixedEnvironmentURL = process.env.NVSOP_BASE_URL
const developmentEnvironment = process.env.NVSOP_DEV === '1'
const reportFile = process.env.PLAYWRIGHT_JSON_OUTPUT_FILE

export default defineConfig({
  testDir: './tests/e2e',
  fullyParallel: true,
  workers: developmentEnvironment ? 2 : undefined,
  forbidOnly: Boolean(process.env.CI),
  retries: process.env.CI ? 1 : 0,
  reporter: reportFile ? [['line'], ['json', { outputFile: reportFile }]] : 'line',
  outputDir: process.env.PLAYWRIGHT_OUTPUT_DIR ?? 'test-results',
  use: {
    baseURL: fixedEnvironmentURL ?? 'http://127.0.0.1:4173',
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
  webServer: fixedEnvironmentURL
    ? undefined
    : {
        command: 'pnpm run dev',
        url: 'http://127.0.0.1:4173',
        reuseExistingServer: !process.env.CI,
      },
})
