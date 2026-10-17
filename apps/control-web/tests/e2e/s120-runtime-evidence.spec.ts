import { expect, test } from '@playwright/test'
import { execFileSync } from 'node:child_process'

/**
 * S120 — 运行、告警、证据跨模块验证的真实浏览器复核边界。
 *
 * 本 spec 不 mock 自家 HTTP/SSE/auth：会话、违规归档、原判定、证据授权与复核全部读真实 Center，
 * 媒体字节从真实来源推理机（HTTPS）读取并由浏览器解码。由 `tests/system/test_s120_runtime_evidence.py`
 * 的 opt-in 用例显式传入环境变量后分阶段运行（阶段由 `NVSOP_EVIDENCE_OFFLINE` 区分），否则 skip。
 */
const hostId = process.env.NVSOP_EVIDENCE_HOST_ID ?? ''
const stationId = process.env.NVSOP_EVIDENCE_STATION_ID ?? ''
const clipId = process.env.NVSOP_EVIDENCE_CLIP_ID ?? ''
const sessionCookie = process.env.NVSOP_EVIDENCE_SESSION ?? ''
const csrfCookie = process.env.NVSOP_EVIDENCE_CSRF ?? ''
const edgeOrigin = process.env.NVSOP_EVIDENCE_ORIGIN ?? ''
const nginxContainer = process.env.NVSOP_EVIDENCE_NGINX_CONTAINER ?? ''
const offlineStage = process.env.NVSOP_EVIDENCE_OFFLINE === '1'

/**
 * 常量注入脚本：只对当前测试自己网关容器内、确属 PID1 nginx master 的 worker 发 SIGTERM
 * （fast shutdown），让已建立的真实 EventSource TCP 连接 EOF，再由原生 EventSource 自动续接。
 * 不 signal PID1、不重写 EventSource、不 mock HTTP/SSE/授权。
 */
const NGINX_WORKER_FAULT_SCRIPT = [
  'set -e',
  '[ "$(cat /proc/1/comm)" = "nginx" ]',
  'children="$(cat /proc/1/task/1/children)"',
  '[ -n "$children" ]',
  'count=0',
  'for pid in $children; do',
  '  [ "$(cat /proc/$pid/comm)" = "nginx" ]',
  '  kill -TERM "$pid"',
  '  count=$((count + 1))',
  'done',
  'echo "terminated $count nginx workers"',
].join('\n')

test.use({ ignoreHTTPSErrors: true })

async function openViolationEvidence(page: import('@playwright/test').Page) {
  await page.goto('/devices')
  const violationRow = page.locator('.runtime__violation-list li').filter({ hasText: stationId })
  await expect(violationRow.first()).toBeVisible()
  await violationRow.first().getByRole('button', { name: '定位相关判定' }).click()
  await expect(page.getByRole('heading', { name: '证据回看（来源推理机直连）' })).toBeVisible()
}

test('S120 — 真实浏览器经来源推理机读取并复核证据', async ({ context, page }) => {
  test.skip(
    offlineStage || !hostId || !stationId || !clipId || !sessionCookie || !edgeOrigin,
    '需要系统测试显式提供真实 Center/Edge 证据环境',
  )
  await context.addCookies([
    { name: 'sop_session', value: sessionCookie, domain: '127.0.0.1', path: '/', secure: false },
    { name: 'sop_csrf', value: csrfCookie, domain: '127.0.0.1', path: '/', secure: false },
  ])
  const mediaHeaders: Record<string, string>[] = []
  const mediaResponses: { status: number; headers: Record<string, string> }[] = []
  page.on('request', (request) => {
    if (request.url().startsWith(edgeOrigin)) {
      void request.allHeaders().then((headers) => mediaHeaders.push(headers))
    }
  })
  page.on('response', (response) => {
    if (response.url().startsWith(edgeOrigin)) {
      mediaResponses.push({ status: response.status(), headers: response.headers() })
    }
  })

  // 真实会话、违规与证据均来自 Center；不定位其它夹具的工位行。
  await openViolationEvidence(page)
  const beforeReview = await page.request.get('/api/v1/monitor/violations?page_size=50')
  expect(beforeReview.status()).toBe(200)
  const beforeItems = await beforeReview.json()

  // AC2 不重复提示：历史归档已显示且无新提示；真实断线→续接后同来源事实不变、0 重复提示。
  await expect(page.getByText('实时连接正常')).toBeVisible({ timeout: 30000 })
  await page.evaluate(() => {
    const target = window as unknown as { __s120NewPrompts: number }
    target.__s120NewPrompts = 0
    const observer = new MutationObserver((records) => {
      for (const record of records) {
        for (const node of Array.from(record.addedNodes)) {
          if ((node.textContent ?? '').includes('新上报违规')) target.__s120NewPrompts += 1
        }
      }
    })
    observer.observe(document.body, { childList: true, subtree: true })
  })
  // 初始历史归档不弹新提示。
  await expect(page.getByText('新上报违规')).toHaveCount(0)
  const archiveBefore = await page.locator('.runtime__violation-list li').count()
  expect(archiveBefore).toBeGreaterThan(0)

  // 真实 TCP 连接中断：快停当前测试自己网关容器内的 nginx worker，使已建立的
  // EventSource 连接真实 EOF；不 mock HTTP/SSE，由原生 EventSource 自动续接恢复。
  expect(nginxContainer).toMatch(/^[0-9a-f]{12,64}$/i)
  execFileSync('docker', ['exec', nginxContainer, 'sh', '-c', NGINX_WORKER_FAULT_SCRIPT], {
    timeout: 10000,
  })
  await expect(page.getByText('连接中断，保留上次镜像等待恢复')).toBeVisible({ timeout: 30000 })
  await expect(page.getByText('实时连接正常')).toBeVisible({ timeout: 30000 })

  // 续接后同一来源的判定/归档不变，断线-恢复期间没有重复“新上报违规”提示。
  await expect
    .poll(async () => page.locator('.runtime__violation-list li').count(), { timeout: 30000 })
    .toBe(archiveBefore)
  await expect(
    page.locator('.runtime__violation-list li').filter({ hasText: stationId }).first(),
  ).toBeVisible()
  expect(
    await page.evaluate(() => (window as unknown as { __s120NewPrompts: number }).__s120NewPrompts),
  ).toBe(0)

  await page
    .getByRole('button', { name: /证据片段/ })
    .first()
    .click()
  const video = page.locator('video[aria-label="证据片段回放"]')
  await expect(video).toBeVisible()
  const source = (await video.getAttribute('src')) ?? ''
  expect(source).toContain(encodeURIComponent(clipId))
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

  // 人工复核：真实表单提交经 Center 持久化；不改写原判定。
  await page.getByLabel('确认（同意原判定）').check()
  const saveButton = page.getByRole('button', { name: '保存复核结论' })
  await saveButton.focus()
  await saveButton.press('Enter')
  await expect(page.getByRole('status').filter({ hasText: '已保存复核结论' })).toBeVisible()

  const listed = await page.request.get(`/api/v1/evidence/${encodeURIComponent(clipId)}/reviews`)
  expect(listed.status()).toBe(200)
  const rows: { evidence_id: string; generation: string; conclusion: string }[] =
    await listed.json()
  expect(
    rows.some(
      (row) =>
        row.evidence_id === clipId &&
        row.generation === 'original' &&
        row.conclusion === 'confirmed',
    ),
  ).toBe(true)

  const afterReview = await page.request.get('/api/v1/monitor/violations?page_size=50')
  expect(afterReview.status()).toBe(200)
  expect(await afterReview.json()).toEqual(beforeItems)

  // 来源读取：Range 生效且不带凭据 Cookie/Authorization。
  expect(mediaResponses.some((response) => response.status === 206)).toBe(true)
  expect(mediaHeaders.length).toBeGreaterThan(0)
  for (const headers of mediaHeaders) {
    expect(headers['cookie']).toBeUndefined()
    expect(headers['authorization']).toBeUndefined()
  }
})

test('S120 — 来源推理机离线时真实浏览器显示不可达', async ({ context, page }) => {
  test.skip(
    !offlineStage || !hostId || !stationId || !clipId || !sessionCookie || !edgeOrigin,
    '仅在来源主机停机的离线阶段运行',
  )
  await context.addCookies([
    { name: 'sop_session', value: sessionCookie, domain: '127.0.0.1', path: '/', secure: false },
    { name: 'sop_csrf', value: csrfCookie, domain: '127.0.0.1', path: '/', secure: false },
  ])
  await openViolationEvidence(page)
  await page
    .getByRole('button', { name: /证据片段/ })
    .first()
    .click()
  // 来源真实停机：读取失败，页面显示不可达，而不是伪造成功。
  await expect(
    page.locator('.evidence').getByRole('alert').filter({ hasText: '来源推理机不可达' }),
  ).toBeVisible()
})
