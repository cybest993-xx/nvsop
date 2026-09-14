import { expect, test } from '@playwright/test'

const SESSION = {
  user_id: '018f0000-0000-7000-8000-00000000e301',
  login_name: 'monitor.operator',
  display_name: '运行操作员',
  expires_at: '2026-09-14T09:00:00Z',
  permissions: ['monitor.report.view'],
}

const SSE_DECISION = [
  'id: host-e301:decision-1',
  'event: decision',
  'data: {"event_id":"host-e301:decision-1","verdict":"indeterminate","reason_codes":["FUTURE_REASON"],"template_version_id":"version-e301","model_ids":["model-e301"]}',
  '',
  '',
].join('\n')

test('SYS-46 — overview shows the raw reason code from the monitor SSE stream', async ({ page }) => {
  await page.route('**/api/v1/auth/session', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(SESSION),
    })
  })
  await page.route('**/api/v1/monitor/stream*', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'text/event-stream',
      headers: { 'Cache-Control': 'no-cache' },
      body: SSE_DECISION,
    })
  })

  await page.goto('/')

  await expect(page.getByRole('heading', { name: '概览' })).toBeVisible()
  await expect(page.getByText('实时上报镜像')).toBeVisible()
  await expect(page.getByText('host-e301:decision-1')).toBeVisible()
  await expect(page.getByText('未知原因码：FUTURE_REASON')).toBeVisible()
  await expect(page.getByText('模板 version-e301')).toBeVisible()
  await expect(page.getByText('模型 model-e301')).toBeVisible()
})
