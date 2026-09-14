import { expect, test } from '@playwright/test'

const SESSION = {
  user_id: '018f0000-0000-7000-8000-00000000e301',
  login_name: 'overview.operator',
  display_name: '概览操作员',
  expires_at: '2026-09-14T09:00:00Z',
  permissions: ['device.host.view', 'monitor.view'],
}

const OVERVIEW = {
  device: {
    status: 'future_status',
    data: { inference_hosts: { total: 1, active: 1, deactivated: 0, unknown: 0 } },
  },
  template: { status: 'not_permitted', data: {} },
  dataset: { status: 'unavailable', data: {}, detail: '训练数据摘要暂时不可用' },
  monitor: {
    status: 'available',
    data: {
      recent_decisions: 1,
      recent_health: 0,
      runtime_status: 'reported_observations_only',
    },
  },
}

const SSE_DECISION = [
  'id: host-e301:decision-1',
  'event: decision',
  'data: {"event_id":"host-e301:decision-1","verdict":"indeterminate","reason_codes":["FUTURE_REASON"],"template_version_id":"version-e301","model_ids":["model-e301"]}',
  '',
  '',
].join('\n')

async function mockOverview(page: import('@playwright/test').Page) {
  await page.route('**/api/v1/auth/session', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(SESSION),
    })
  })
  await page.route('**/api/v1/overview', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(OVERVIEW),
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
}

test('SYS-35-46 — overview shows permission-scoped states and a raw SSE reason code', async ({
  page,
}) => {
  await mockOverview(page)

  await page.goto('/')

  await expect(page.getByRole('heading', { name: '概览' })).toBeVisible()
  await expect(page.getByRole('heading', { name: '设备拓扑' })).toBeVisible()
  await expect(page.getByText('状态未知')).toBeVisible()
  await expect(page.getByRole('heading', { name: 'SOP 模板' })).toBeVisible()
  await expect(page.getByText('无权限')).toBeVisible()
  await expect(page.getByText('训练数据摘要暂时不可用')).toBeVisible()
  await expect(page.getByText('实时上报镜像')).toBeVisible()
  await expect(page.getByText('decision · host-e301:decision-1')).toBeVisible()
  await expect(page.getByText('未知原因码：FUTURE_REASON')).toBeVisible()
  await expect(page.getByText('模板 version-e301')).toBeVisible()
  await expect(page.getByText('模型 model-e301')).toBeVisible()
})

test('SYS-35-46 — a monitor section without permission does not open the SSE stream', async ({
  page,
}) => {
  let streamRequested = false
  await page.route('**/api/v1/auth/session', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ ...SESSION, permissions: ['device.host.view'] }),
    })
  })
  await page.route('**/api/v1/overview', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        ...OVERVIEW,
        monitor: { status: 'not_permitted', data: {} },
      }),
    })
  })
  await page.route('**/api/v1/monitor/stream*', async (route) => {
    streamRequested = true
    await route.abort()
  })

  await page.goto('/')

  const monitorSection = page
    .getByRole('article')
    .filter({ has: page.getByRole('heading', { name: '运行观测' }) })
  await expect(monitorSection).toBeVisible()
  await expect(monitorSection.getByText('当前账号无权查看此模块。')).toBeVisible()
  expect(streamRequested).toBe(false)
})
