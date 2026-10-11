import { expect, test } from '@playwright/test'

/**
 * SYS-35 证据回看的真实浏览器验证。
 *
 * 会话与违规归档为最小测试接线；证据授权、来源回查与媒体字节全部走真实 Center/Edge。
 * 由 `tests/system/test_sys_35_evidence.py` 的 opt-in 用例显式传入环境变量后运行，否则跳过。
 */
const hostId = process.env.NVSOP_EVIDENCE_HOST_ID ?? ''
const stationId = process.env.NVSOP_EVIDENCE_STATION_ID ?? ''
const instanceId = Number(process.env.NVSOP_EVIDENCE_INSTANCE_ID ?? '0')
const sessionCookie = process.env.NVSOP_EVIDENCE_SESSION ?? ''
const csrfCookie = process.env.NVSOP_EVIDENCE_CSRF ?? ''
const edgeOrigin = process.env.NVSOP_EVIDENCE_ORIGIN ?? ''

test.use({ ignoreHTTPSErrors: true })

const SESSION = {
  user_id: '018f0000-0000-7000-8000-00000000e355',
  login_name: 'evidence.operator',
  display_name: '证据复核员',
  expires_at: '2027-09-14T09:00:00Z',
  permissions: [
    'monitor.report.view',
    'evidence.evidence.view',
    'evidence.review.edit',
    'device.station.view',
    'device.connector.view',
    'device.inference_host.view',
    'device.camera.view',
    'device.point.view',
  ],
}

const VIOLATION = {
  event_id: 'sys35-evidence-violation',
  decision_event_id: 'sys35-evidence-decision',
  host_id: hostId,
  station_id: stationId,
  instance_id: instanceId,
  reported_at: '2026-09-14T01:00:00Z',
  received_at: '2026-09-14T01:00:01Z',
  latched_at: '2026-09-14T01:00:01Z',
  violation: {
    reason_code: 'WRONG_STEP',
    detail: null,
    step_ids: ['1'],
    evidence: { anchor: 1, start: 0, end: 2 },
  },
}

test('SYS-35 — 浏览器经真实来源解码证据片段与关键帧', async ({ context, page }) => {
  test.skip(
    !hostId || !stationId || !sessionCookie || !edgeOrigin,
    '需要系统测试显式提供 Center/Edge 证据环境',
  )
  await context.addCookies([
    { name: 'sop_session', value: sessionCookie, domain: '127.0.0.1', path: '/', secure: false },
    { name: 'sop_csrf', value: csrfCookie, domain: '127.0.0.1', path: '/', secure: false },
  ])
  const mediaHeaders: Record<string, string>[] = []
  const mediaResponses: { status: number; headers: Record<string, string> }[] = []
  page.on('request', (request) => {
    if (request.url().startsWith(edgeOrigin)) {
      // `headers()` 会省略安全 Cookie；`allHeaders()` 才是证明“未泄漏凭据”的证据。
      void request.allHeaders().then((headers) => mediaHeaders.push(headers))
    }
  })
  page.on('response', (response) => {
    if (response.url().startsWith(edgeOrigin)) {
      mediaResponses.push({ status: response.status(), headers: response.headers() })
    }
  })

  await page.route('**/api/v1/auth/session', (route) => route.fulfill({ json: SESSION }))
  await page.route('**/api/v1/monitor/violations**', (route) =>
    route.fulfill({ json: { items: [VIOLATION], page: 1, page_size: 50, total: 1 } }),
  )
  await page.route('**/api/v1/overview', (route) =>
    route.fulfill({
      json: {
        device: { status: 'available', data: {} },
        template: { status: 'no_data', data: {} },
        dataset: { status: 'no_data', data: {} },
        monitor: { status: 'available', data: {} },
      },
    }),
  )
  await page.route('**/api/v1/monitor/stream*', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'text/event-stream',
      headers: { 'Cache-Control': 'no-cache' },
      body: '',
    }),
  )

  await page.goto('/devices')
  await page.getByRole('button', { name: '定位相关判定' }).first().click()
  await expect(page.getByRole('heading', { name: '证据回看（来源推理机直连）' })).toBeVisible()

  // 片段：真实来源 Range 读取、真实解码并推进播放时间。
  await page
    .getByRole('button', { name: /证据片段/ })
    .first()
    .click()
  const video = page.locator('video[aria-label="证据片段回放"]')
  await expect(video).toBeVisible()
  const metrics = await video.evaluate(async (element: HTMLVideoElement) => {
    element.muted = true
    await element.play()
    await new Promise((resolve) => setTimeout(resolve, 1200))
    return {
      readyState: element.readyState,
      videoWidth: element.videoWidth,
      currentTime: element.currentTime,
    }
  })
  expect(metrics.readyState).toBeGreaterThanOrEqual(2)
  expect(metrics.videoWidth).toBeGreaterThan(0)
  expect(metrics.currentTime).toBeGreaterThan(0)

  // AC3：原机器判定不在当前镜像时如实说明，不据违规推断为“不通过”。
  await expect(page.locator('.runtime__review')).toContainText('原判定结论未加载或缺失')

  // 人工复核：真实表单提交经 Center 持久化，再以 GET 查询证明字段与服务器时间。
  // 键盘可用性：标签 radio 可聚焦，提交按钮 focus 后 Enter 提交。
  await page.getByLabel('确认（同意原判定）').check()
  const saveButton = page.getByRole('button', { name: '保存复核结论' })
  await saveButton.focus()
  await saveButton.press('Enter')
  const savedStatus = page.getByRole('status').filter({ hasText: '已保存复核结论' })
  await expect(savedStatus).toBeVisible()
  await expect(savedStatus).toBeFocused()
  const clipSource = (await video.getAttribute('src')) ?? ''
  const evidenceId = decodeURIComponent(new URL(clipSource).pathname.replace('/evidence/', ''))
  const listed = await page.request.get(
    `/api/v1/evidence/${encodeURIComponent(evidenceId)}/reviews`,
  )
  expect(listed.status()).toBe(200)
  const saved = (await listed.json()).find(
    (row: { evidence_id: string }) => row.evidence_id === evidenceId,
  )
  expect(saved.review_id).toBeTruthy()
  expect(saved).toMatchObject({ generation: 'original', conclusion: 'confirmed' })
  expect(saved.reviewed_at).toBeTruthy()
  expect(saved.reviewer_name).toBeTruthy()

  // 关键帧：真实来源读取并解码。
  await page
    .getByRole('button', { name: /锚点关键帧/ })
    .first()
    .click()
  const frame = page.locator('img[alt="证据锚点关键帧"]')
  await expect(frame).toBeVisible()
  await expect
    .poll(() => frame.evaluate((image: HTMLImageElement) => image.naturalWidth), {
      timeout: 20_000,
    })
    .toBeGreaterThan(0)

  // 来源读取必须是 206 分段响应，且不携带中心 Cookie/Authorization。
  expect(mediaResponses.some((response) => response.status === 206)).toBe(true)
  expect(
    mediaResponses.some((response) => response.headers['content-range']?.startsWith('bytes ')),
  ).toBe(true)
  expect(mediaHeaders.length).toBeGreaterThan(0)
  for (const headers of mediaHeaders) {
    expect(headers['cookie']).toBeUndefined()
    expect(headers['authorization']).toBeUndefined()
  }
})
