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
  "physical_safety":{"status":"protected","detail":"Center 授权有效，Edge 报告物理写入门禁已启用","center_authorization":{"state":"active","grant_id":"grant-19","holder_host_id":"host-19","lease_expires_at":"2026-09-15T08:00:00Z","renewed_at":"2026-09-14T08:00:00Z"},"edge_status":{"host_id":"host-19","authority_state":"active","write_state":"enabled","reason_code":null,"detail":null,"grant_id":"grant-19","holder_host_id":"host-19","lease_expires_at":"2026-09-15T08:00:00Z","renewed_at":"2026-09-14T08:00:00Z","reported_at":"2026-09-14T08:00:03Z","received_at":"2026-09-14T08:00:04Z","stale":false}},
  "instance":{"event_id":"instance-19","trace_id":"trace-instance-19","host_id":"host-19","instance_id":19,"opened_at":12.5,"closed_at":null,"close_reason":null,"open_boundary_signal":"start","close_boundary_signal":null,"contract_version":1,"template_version_id":"actual-instance-template","template_sha256":"${'a'.repeat(64)}","backend_provenance":[{"backend_id":"backend-19","model_ids":["actual-instance-model"]}],"configuration_revision":7,"configuration_sha256":"${'b'.repeat(64)}","reported_at":"2026-09-14T08:00:00Z"},
  "observation":{"event_id":"observation-19","contract_version":1,"instance_id":19,"source":"action","signal":"(2) 拧紧螺栓","source_time":8.25,"source_anchor":100.5,"observed_at":8.5,"template_version_id":"actual-observation-template","template_sha256":"${'d'.repeat(64)}","backend":{"backend_id":"backend-observation","model_ids":["actual-observation-model"]},"reported_at":"2026-09-14T08:00:01Z"},
  "decision":{"event_id":"decision-19","trace_id":"trace-19","host_id":"host-19","instance_id":19,"verdict":"${verdict}","reason_codes":["FUTURE_REASON_19"],"lifecycle":"open","evidence":{"anchor":8.25,"start":7.5,"end":9},"template_version_id":"actual-decision-template","template_sha256":"${'c'.repeat(64)}","model_ids":[],"backend_provenance":[{"backend_id":"backend-decision","model_ids":["actual-field-model"]}],"configuration_revision":7,"configuration_sha256":"${'e'.repeat(64)}","contract_version":2,"violations":[],"reported_at":"2026-09-14T08:00:02Z"},
  "health":[{"event_id":"health-19","trace_id":"trace-health-19","host_id":"host-19","station_id":"${stationId}","contract_version":1,"stream_id":"camera-19","status":"future_health_state","reason_code":"FUTURE_HEALTH_REASON","detail":"synthetic stream fact","occurred_at":"2026-09-14T08:00:00Z","source_anchor":100.5,"anchor_offset":0.25,"reported_at":"2026-09-14T08:00:03Z"}]
}`)
const frame = (value: object) => `event: runtime\ndata: ${JSON.stringify(value)}\n\n`
const decisionFrame = (value: object) =>
  `id: ${(value as { event_id: string }).event_id}\nevent: decision\ndata: ${JSON.stringify(value)}\n\n`
const liveFrame = 'event: live\ndata: {}\n\n'
const violation = (
  id: string,
  reportedAt = '2026-09-14T08:00:02Z',
  receivedAt = '2026-10-09T08:00:00Z',
) => ({
  event_id: `${id}#0`,
  decision_event_id: id,
  host_id: 'host-19',
  station_id: stationId,
  instance_id: 19,
  reported_at: reportedAt,
  received_at: receivedAt,
  violation: {
    reason_code: id === 'decision-19' ? 'FUTURE_REASON_19' : 'MISSED_STEP',
    detail: '来源判定已锁存',
    step_ids: ['bolt'],
    evidence: { anchor: 8.25, start: 7.5, end: 9 },
  },
})
const pageOf = (items: unknown[]) => ({ items, page: 1, page_size: 50, total: items.length })
const stationConfig = JSON.parse(
  `{"station_id":"${stationId}","station_revision":1,"status":"waiting","desired":{"version_id":"desired-runtime-template","sha256":"${'f'.repeat(64)}","config_revision":1},"version":null,"backends":[],"runtime_parameter_mode":"follow_template","runtime_parameters_revision":1,"effective_runtime_parameters":null,"runtime_overrides":null,"template_defaults":null,"topology_issues":[]}`,
)

async function standardRoutes(
  page: Page,
  permissions = session.permissions,
  stream?: (route: Route) => Promise<void>,
  violations: () => object[] = () => [],
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
    if (path === '/api/v1/monitor/violations') {
      await route.fulfill({ status: 200, json: pageOf(violations()) })
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

async function installStableRuntimeStream(page: Page, value: object) {
  await page.addInitScript((projectionValue) => {
    class StableEventSource extends EventTarget {
      onopen: ((event: Event) => void) | null = null
      onerror: ((event: Event) => void) | null = null

      constructor() {
        super()
        setTimeout(() => {
          this.onopen?.(new Event('open'))
          this.dispatchEvent(new MessageEvent('runtime', { data: JSON.stringify(projectionValue) }))
          this.dispatchEvent(new MessageEvent('live', { data: '{}' }))
        }, 0)
      }

      close() {}
    }
    Object.defineProperty(window, 'EventSource', { value: StableEventSource, configurable: true })
  }, value)
}

test('SYS-19 — reconnect retains projection and native MessageEvent.lastEventId while ignoring duplicate/legacy frames', async ({
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
        ? `retry: 50\nid: legacy-cursor\nevent: decision\ndata: {}\n\n${frame(value)}${frame(value)}${liveFrame}`
        : requests === 3
          ? `${frame(projection('pass'))}${frame(value)}${liveFrame}id: legacy-cursor\nevent: decision\ndata: {"verdict":"pass"}\n\n`
          : `${frame(projection('indeterminate'))}${frame(value)}${liveFrame}`
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
  await expect(page.getByRole('region', { name: '物理防错与执行权' })).toContainText(
    '物理防错状态未知（数据已过期）',
  )
  await expect(decision).toContainText('不可判定')
  await expect(page.getByText(/连接中断，保留上次镜像等待恢复/)).toBeVisible()
  await expect(decision).toContainText('不通过')
  await expect(decision).toContainText('actual-decision-template')
  await expect(page.getByRole('region', { name: '各路观测健康' })).toContainText(
    'future_health_state',
  )
  const physicalSafety = page.getByRole('region', { name: '物理防错与执行权' })
  await expect(physicalSafety).toContainText('物理防错状态未知（数据已过期）')
  await expect(physicalSafety).toContainText('Center 授权有效')
  await expect(physicalSafety).toContainText('host-19')
  await expect(page.getByRole('region', { name: 'SOP 实例与实际来源' })).toContainText(
    'actual-instance-template',
  )
  await expect(page.getByRole('region', { name: 'SOP 实例与实际来源' })).toContainText('运行中')
  await expect(page.getByRole('region', { name: '最新观测' })).toContainText(
    'actual-observation-template',
  )
  await expect(page.getByRole('region', { name: '最新观测' })).toContainText('(2) 拧紧螺栓')
  await expect(page.getByText(/undefined|NaN/)).toHaveCount(0)
  await expect(page.getByRole('alert')).toHaveCount(0)
  expect(requests).toBeGreaterThanOrEqual(5)
  expect(await page.evaluate(() => window.runtimeEventIds)).toContain('legacy-cursor')
})

test('S054 — Edge stopped-write is a failed physical-safety state separate from judgment', async ({
  page,
}) => {
  const value = projection('pass')
  value.physical_safety = {
    ...value.physical_safety,
    status: 'failed',
    detail: '工位已停止写入',
    edge_status: {
      ...value.physical_safety.edge_status,
      write_state: 'stopped',
      reason_code: 'write_stopped',
      detail: '工位已停止写入',
    },
  }
  await installStableRuntimeStream(page, value)
  await standardRoutes(page, session.permissions)
  await page.goto('/devices')
  await expect(page.getByRole('region', { name: '物理防错与执行权' })).toContainText('物理防错失效')
  await expect(page.getByRole('region', { name: '物理防错与执行权' })).toContainText(
    'Edge 已停止物理写入',
  )
  await expect(page.getByRole('region', { name: '最新判定' })).toContainText('通过')
})

test('S054 — stale Edge state shows known time and never a green write indication', async ({
  page,
}) => {
  const value = projection('pass')
  value.physical_safety = {
    ...value.physical_safety,
    status: 'stale',
    detail: 'Edge 执行权/停写状态已过期或尚未取得；不显示为仍可写',
    edge_status: { ...value.physical_safety.edge_status, stale: true },
  }
  await installStableRuntimeStream(page, value)
  await standardRoutes(page, session.permissions)
  await page.goto('/devices')
  const physical = page.getByRole('region', { name: '物理防错与执行权' })
  await expect(physical).toContainText('物理防错状态未知（数据已过期）')
  await expect(physical).toContainText('Edge 已知时间')
  await expect(physical).toContainText('数据已过期；不会沿用最后一次正常状态表示仍可写')
  await expect(physical).not.toContainText('物理防错有效')
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

test('SYS-23 — archive preserves original timestamps, handles keyboard location and unknown codes', async ({
  page,
}) => {
  const history = violation('decision-19')
  await standardRoutes(page, ['monitor.report.view', 'device.station.view'], undefined, () => [
    history,
  ])
  await page.goto('/devices')
  const archive = page.getByRole('region', { name: '违规告警归档' })
  await expect(archive).toContainText('未知原因码：FUTURE_REASON_19')
  await expect(archive).toContainText('8.25（推理机时间轴）')
  await expect(archive).toContainText('2026/10/9')
  await expect(archive).toContainText('装配工位十九')
  await expect(page.getByRole('alert')).toHaveCount(0)
  const locator = archive.getByRole('button', { name: '定位相关判定' })
  await locator.focus()
  await locator.press('Enter')
  await expect(page.getByRole('region', { name: '最新判定' })).toBeFocused()
  await expect(page.getByText('已定位至当前运行镜像中的原判定。', { exact: false })).toBeVisible()
})

test('SYS-23 — only live, fresh, stable events notify; reconnect and delayed reports remain archival', async ({
  page,
}) => {
  const historical = violation('decision-19')
  const freshId = 'decision-new-19'
  const now = Date.now()
  const fresh = violation(
    freshId,
    new Date(now + 1_000).toISOString(),
    new Date(now + 2_000).toISOString(),
  )
  // 来源判定发生于页面打开之后，但中心在很久以后才接收：只能归档，不能当作新告警。
  const delayed = violation(
    'decision-delayed-19',
    new Date(now + 10_000).toISOString(),
    new Date(now + 130_000).toISOString(),
  )
  let reads = 0
  await standardRoutes(
    page,
    ['monitor.report.view'],
    async (route) => {
      const decision = {
        event_id: freshId,
        realtime: true,
        reported_at: fresh.reported_at,
        station_id: stationId,
        violations: [fresh.violation],
      }
      const oldDecision = {
        event_id: delayed.decision_event_id,
        realtime: false,
        reported_at: delayed.reported_at,
        station_id: stationId,
        violations: [delayed.violation],
      }
      await route.fulfill({
        status: 200,
        contentType: 'text/event-stream',
        body: [
          decisionFrame({ ...decision, event_id: historical.decision_event_id }),
          frame(projection('pass')),
          liveFrame,
          decisionFrame(decision),
          decisionFrame(decision),
          decisionFrame(oldDecision),
        ].join(''),
      })
    },
    () => {
      reads += 1
      return reads < 3 ? [historical] : [fresh, delayed, historical]
    },
  )
  await page.goto('/devices')
  const archive = page.getByRole('region', { name: '违规告警归档' })
  await expect(archive).toContainText(fresh.event_id)
  await expect(archive).toContainText(delayed.event_id)
  await expect(page.getByText('新上报违规：', { exact: false })).toHaveCount(1)
  await expect(page.getByText('连接中断，保留上次镜像等待恢复')).toBeVisible()
  await expect(archive).toContainText('来源判定上报')
})

test('SYS-23 — slow archive request never blocks the runtime SSE projection', async ({ page }) => {
  let releaseArchive: (() => void) | undefined
  await standardRoutes(page)
  await page.route('**/api/v1/monitor/violations*', async (route) => {
    await new Promise<void>((resolve) => {
      releaseArchive = resolve
    })
    await route.fulfill({ status: 200, json: pageOf([]) })
  })
  await page.goto('/devices')
  await expect(page.getByText('正在读取违规归档…')).toBeVisible()
  try {
    await expect(page.getByRole('region', { name: '最新判定' })).toContainText('通过')
  } finally {
    releaseArchive?.()
  }
  await expect(page.getByText('尚无已归档违规；不推断工位合规。')).toBeVisible()
})

test('SYS-23 — failure to read the archive stays visible and retry does not erase old records', async ({
  page,
}) => {
  const history = violation('decision-19')
  let fail = false
  await standardRoutes(page, session.permissions, undefined, () => [history])
  await page.route('**/api/v1/monitor/violations*', async (route) => {
    if (fail) {
      await route.fulfill({
        status: 500,
        json: { title: '归档服务暂不可用', error_code: 'MONITOR_DOWN' },
      })
    } else {
      await route.fulfill({ status: 200, json: pageOf([history]) })
    }
  })
  await page.goto('/devices')
  const archive = page.getByRole('region', { name: '违规告警归档' })
  await expect(archive).toContainText(history.event_id)
  fail = true
  await archive.getByRole('button', { name: '刷新归档' }).click()
  await expect(page.getByRole('alert')).toContainText('归档服务暂不可用')
  await expect(archive).toContainText(history.event_id)
})
