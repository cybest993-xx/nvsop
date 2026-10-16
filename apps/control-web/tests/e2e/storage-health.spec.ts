/** 仅替换认证/健康传输边界, 浏览器渲染真实设备页; 不模拟磁盘或判定内核。 */
import { expect, test } from '@playwright/test'

function projection(recovered: boolean) {
  return {
    station_id: 'station-209',
    decision: {
      event_id: 'historical-decision-209',
      verdict: 'indeterminate',
      reason_codes: ['WRONG_STEP', 'STORAGE_UNAVAILABLE'],
      instance_id: 209,
      lifecycle: 'closed',
      reported_at: '2026-10-16T08:00:00Z',
    },
    health: ['recording', 'evidence', 'local_state'].map((role, index) => ({
      event_id: `${role}-${recovered}`,
      stream_id: `storage:${role}`,
      status: index === 0 && !recovered ? 'storage_unavailable' : 'delivering',
      reason_code: index === 0 && !recovered ? 'STORAGE_UNAVAILABLE' : null,
      detail: JSON.stringify({
        path: `/var/lib/nvsop/${role}`,
        used_percent: index === 0 ? (recovered ? 80 : 91) : 30 + index,
        high_watermark_percent: 90,
        recovery_watermark_percent: 80,
      }),
      occurred_at: '2026-10-16T08:00:00Z',
      source_anchor: null,
      anchor_offset: null,
    })),
  }
}

test('设备页按独立路径显示水位, 恢复不改写历史违规', async ({ page }, testInfo) => {
  let recovered = false
  const errors: string[] = []
  page.on('pageerror', (error) => errors.push(error.message))
  await page.route('**/api/v1/**', async (route) => {
    const path = new URL(route.request().url()).pathname
    if (path === '/api/v1/auth/session') {
      await route.fulfill({
        json: {
          user_id: 'synthetic-storage-viewer',
          login_name: 'synthetic-storage-viewer',
          display_name: '存储查看者',
          expires_at: '2099-01-01T00:00:00Z',
          permissions: ['monitor.report.view'],
        },
      })
    } else if (path === '/api/v1/monitor/violations') {
      await route.fulfill({ json: { items: [], page: 1, page_size: 50, total: 0 } })
    } else if (path === '/api/v1/monitor/stream') {
      await route.fulfill({
        contentType: 'text/event-stream',
        body: `event: runtime\ndata: ${JSON.stringify(projection(recovered))}\n\n`,
      })
    } else {
      await route.fulfill({ status: 404, json: { detail: 'synthetic test has no such API' } })
    }
  })
  await page.goto('/devices')
  const storage = page.getByRole('region', { name: '边缘存储健康' })
  await expect(storage).toContainText('连续录像')
  await expect(storage).toContainText('证据目录')
  await expect(storage).toContainText('SQLite / 本地待办')
  await expect(storage).toContainText('使用 91%')
  await expect(storage).toContainText('高水位 ≥ 90% · 恢复 ≤ 80%')
  await expect(storage.getByText('存储失效', { exact: true })).toHaveCount(1)
  await expect(storage.getByText('存储可用', { exact: true })).toHaveCount(2)
  await storage.scrollIntoViewIfNeeded()
  await storage.screenshot({ path: testInfo.outputPath('storage-high.png') })
  expect(await storage.evaluate((node) => node.scrollWidth <= node.clientWidth)).toBeTruthy()
  recovered = true
  await page.reload()
  await expect(storage).toContainText('使用 80%')
  await expect(storage.getByText('存储可用', { exact: true })).toHaveCount(3)
  await expect(page.getByText('WRONG_STEP — 出现了错误步骤', { exact: false })).toBeVisible()
  await storage.screenshot({ path: testInfo.outputPath('storage-recovered.png') })
  expect(errors).toEqual([])
})
