import { expect, test, type Page, type Route } from '@playwright/test'

declare global {
  interface Window {
    runtimeEventIds: string[]
  }
}

const stationId = 'station-runtime-19'
const session = {
  user_id: '01900000-0000-7000-8000-000000001619',
  login_name: 'viewer',
  display_name: '查看者',
  expires_at: '2026-09-14T09:00:00Z',
  permissions: ['monitor.report.view'],
}
const station = { id: stationId, name: '装配工位十九', code: 'A19', revision: 1 }
const projection = (verdict: string) =>
  JSON.parse(`{
  "station_id":"${stationId}",
  "instance":{"event_id":"instance-19","trace_id":"trace-instance-19","host_id":"host-19","instance_id":19,"opened_at":12.5,"closed_at":null,"close_reason":null,"open_boundary_signal":"start","close_boundary_signal":null,"contract_version":1,"template_version_id":"actual-instance-template","template_sha256":"${'a'.repeat(64)}","backend_provenance":[{"backend_id":"backend-19","model_ids":["actual-instance-model"]}],"configuration_revision":7,"configuration_sha256":"${'b'.repeat(64)}","reported_at":"2026-09-14T08:00:00Z"},
  "observation":{"event_id":"observation-19","contract_version":1,"instance_id":19,"source":"action","signal":"(2) 拧紧螺栓","source_time":8.25,"source_anchor":100.5,"observed_at":8.5,"template_version_id":"actual-observation-template","template_sha256":"${'d'.repeat(64)}","backend":{"backend_id":"backend-observation","model_ids":["actual-observation-model"]},"reported_at":"2026-09-14T08:00:01Z"},
  "decision":{"event_id":"decision-19","trace_id":"trace-19","host_id":"host-19","instance_id":19,"verdict":"${verdict}","reason_codes":["FUTURE_REASON_19"],"lifecycle":"open","evidence":{"anchor":8.25,"start":7.5,"end":9},"template_version_id":"actual-decision-template","template_sha256":"${'c'.repeat(64)}","model_ids":[],"backend_provenance":[{"backend_id":"backend-decision","model_ids":["actual-field-model"]}],"configuration_revision":7,"configuration_sha256":"${'e'.repeat(64)}","contract_version":2,"violations":[],"reported_at":"2026-09-14T08:00:02Z"},
  "health":[{"event_id":"health-19","trace_id":"trace-health-19","host_id":"host-19","station_id":"${stationId}","contract_version":1,"stream_id":"camera-19","status":"future_health_state","reason_code":"FUTURE_HEALTH_REASON","detail":"synthetic stream fact","occurred_at":"2026-09-14T08:00:00Z","source_anchor":100.5,"anchor_offset":0.25,"reported_at":"2026-09-14T08:00:03Z"}]
}`)
const frame = (value: object) => `event: runtime\ndata: ${JSON.stringify(value)}\n\n`
const pageOf = (items: unknown[]) => ({ items, page: 1, page_size: 50, total: items.length })
const stationConfig = JSON.parse(
  `{"station_id":"${stationId}","station_revision":1,"status":"waiting","desired":{"version_id":"desired-runtime-template","sha256":"${'f'.repeat(64)}","config_revision":1},"version":null,"backends":[],"runtime_parameter_mode":"follow_template","runtime_parameters_revision":1,"effective_runtime_parameters":null,"runtime_overrides":null,"template_defaults":null,"topology_issues":[]}`,
)

async function standardRoutes(
  page: Page,
  permissions = session.permissions,
  stream?: (route: Route) => Promise<void>,
) {
  await page.route('**/api/v1/**', async (route) => {
    const path = new URL(route.request().url()).pathname
    if (path === '/api/v1/auth/session') {
      await route.fulfill({ status: 200, json: { ...session, permissions } })
      return
    }
    if (path === '/api/v1/stations') {
      await route.fulfill({ status: 200, json: pageOf([station]) })
      return
    }
    if (path.endsWith(`/templates/stations/${stationId}/configuration`)) {
      await route.fulfill({ status: 200, json: stationConfig })
      return
    }
    if (path === '/api/v1/connectors') {
      await route.fulfill({ status: 200, json: pageOf([]) })
      return
    }
    if (path === '/api/v1/monitor/stream') {
      if (stream) await stream(route)
      else
        await route.fulfill({
          status: 200,
          contentType: 'text/event-stream',
          body: frame(projection('pass')),
        })
      return
    }
    await route.fulfill({ status: 403, json: {} })
  })
}

test('SYS-19 — reconnect retains projection, preserves Last-Event-ID and ignores duplicate runtime/legacy decision frames', async ({
  page,
}) => {
  let requests = 0
  await page.addInitScript(() => {
    window.runtimeEventIds = []
    EventSource.prototype.addEventListener = function (
      this: EventSource,
      type: string,
      listener: EventListenerOrEventListenerObject | null,
      options?: boolean | AddEventListenerOptions,
    ) {
      if (type === 'runtime')
        EventTarget.prototype.addEventListener.call(this, type, (event) =>
          window.runtimeEventIds.push((event as MessageEvent).lastEventId),
        )
      return EventTarget.prototype.addEventListener.call(this, type, listener, options)
    } as typeof EventSource.prototype.addEventListener
  })
  await standardRoutes(page, session.permissions, async (route) => {
    requests += 1
    if (requests === 2 || requests === 4) {
      await route.abort()
      return
    }
    if (requests === 3 || requests === 5) await new Promise((resolve) => setTimeout(resolve, 500))
    const value = projection(requests < 3 ? 'pass' : requests < 5 ? 'indeterminate' : 'fail')
    const body =
      requests === 1
        ? `retry: 50\nid: legacy-cursor\nevent: decision\ndata: {}\n\n${frame(value)}${frame(value)}`
        : requests === 3
          ? `${frame(projection('pass'))}${frame(value)}id: legacy-cursor\nevent: decision\ndata: {"verdict":"pass"}\n\n`
          : `${frame(projection('indeterminate'))}${frame(value)}`
    await route.fulfill({
      status: 200,
      contentType: 'text/event-stream',
      headers: { 'Cache-Control': 'no-cache' },
      body,
    })
  })
  await page.goto('/devices')
  const decision = page.getByRole('region', { name: '最新判定' })
  await expect(decision).toContainText('通过')
  await expect(decision).toContainText('未知原因码：FUTURE_REASON_19')
  await expect(page.getByText(/连接中断，保留上次镜像等待恢复/)).toBeVisible()
  await expect(decision).toContainText('不可判定')
  await expect(page.getByText(/连接中断，保留上次镜像等待恢复/)).toBeVisible()
  await expect(decision).toContainText('不通过')
  await expect(decision).toContainText('actual-decision-template')
  await expect(page.getByRole('region', { name: '各路观测健康' })).toContainText(
    'future_health_state',
  )
  await expect(page.getByRole('region', { name: 'SOP 实例与实际来源' })).toContainText(
    'actual-instance-template',
  )
  await expect(page.getByRole('region', { name: '最新观测' })).toContainText(
    'actual-observation-template',
  )
  await expect(page.getByText(/undefined|NaN/)).toHaveCount(0)
  await expect(page.getByRole('alert')).toHaveCount(0)
  expect(requests).toBeGreaterThanOrEqual(5)
  expect(await page.evaluate(() => window.runtimeEventIds)).toContain('legacy-cursor')
})

test('SYS-19 — station permission separates configured/actual versions and keyboard navigation reaches the runtime panel', async ({
  page,
}) => {
  await standardRoutes(page, ['monitor.report.view', 'device.station.view'])
  await page.goto('/')
  await expect(page.getByRole('heading', { name: '概览' })).toBeVisible()
  await page.keyboard.press('Tab')
  await page.keyboard.press('Tab')
  await page.keyboard.press('Tab')
  const devices = page.getByRole('link', { name: '工位与设备' })
  await expect(devices).toBeFocused()
  await page.keyboard.press('Enter')
  await expect(page.getByRole('heading', { name: '工位运行镜像' })).toBeVisible()
  await expect(page.getByRole('region', { name: '最新判定' })).toContainText(
    'actual-decision-template',
  )
  await expect(page.getByRole('region', { name: '最新判定' })).toContainText('actual-field-model')
  await expect(page.getByText('desired-runtime-template')).toBeVisible()
  await expect(page.getByText('装配工位十九', { exact: true })).toBeVisible()
})

test('SYS-19 — device-only permission does not open stream or expose runtime facts', async ({
  page,
}) => {
  let streams = 0
  await standardRoutes(page, ['device.connector.view'], async (route) => {
    streams += 1
    await route.abort()
  })
  await page.goto('/devices')
  await expect(page.getByRole('heading', { name: '工位与设备' })).toBeVisible()
  await expect(page.getByRole('heading', { name: '工位运行镜像' })).toHaveCount(0)
  expect(streams).toBe(0)
})
