/** 浏览器保留请求态与来源机观测; 真实 PG/TLS/FFmpeg 闭环另由 S049 system 场景证明。 */
import { expect, test } from '@playwright/test'

for (const editable of [true, false]) {
  test(`录像压缩页面：${editable ? '控制与确认' : '只读权限'}`, async ({ page }, testInfo) => {
    let action = 'inspect'
    let complete = false
    const keys: string[] = []
    await page.route('**/api/v1/**', async (route) => {
      const request = route.request()
      const path = new URL(request.url()).pathname
      if (path === '/api/v1/auth/session') {
        await route.fulfill({
          json: {
            user_id: 'operator',
            login_name: 'operator',
            display_name: '录像操作员',
            expires_at: '2099-01-01T00:00:00Z',
            permissions: [
              'device.inference_host.view',
              ...(editable ? ['device.inference_host.edit'] : []),
            ],
          },
        })
      } else if (path === '/api/v1/inference-hosts') {
        await route.fulfill({
          json: {
            items: [{ id: 'host-a', name: '装配线推理机', status: 'active' }],
            page: 1,
            page_size: 50,
            total: 1,
          },
        })
      } else if (path.includes('/recording-compression/commands')) {
        if (request.method() === 'POST') {
          action = request.postDataJSON().action
          complete = false
          keys.push(request.headers()['idempotency-key'] ?? '')
        }
        await route.fulfill({
          status: request.method() === 'POST' ? 202 : 200,
          json: {
            id: 'command-a',
            host_id: 'host-a',
            action,
            status: complete ? 'succeeded' : 'pending',
            observation: complete
              ? {
                  state: action === 'pause' ? 'paused' : action === 'abort' ? 'aborted' : 'running',
                  requested: action === 'pause' ? 'pause' : action === 'abort' ? 'abort' : 'run',
                  segment: '2026-10-17_08-00-00.mp4',
                  pid: action === 'abort' ? null : 123,
                  readrate: 0.5,
                  failure_code: null,
                  detail: null,
                }
              : null,
            observed_at: complete ? '2026-10-17T01:00:00Z' : null,
            failure_code: null,
          },
        })
      } else {
        await route.fulfill({ json: { items: [], page: 1, page_size: 50, total: 0 } })
      }
    })
    await page.goto('/devices')
    const panel = page.getByRole('region', { name: '录像压缩归档' })
    await expect(panel).toBeVisible()
    await expect(panel).toContainText('尚未取得实际状态')
    if (!editable) {
      // 控制入队需要编辑权限: 只读会话只看到状态文本, 不向推理机派发任何请求。
      await expect(panel.getByRole('button', { name: '刷新实际状态' })).toHaveCount(0)
      await expect(panel.getByRole('button', { name: '暂停压缩' })).toHaveCount(0)
      await expect(panel.getByRole('button', { name: '中止压缩' })).toHaveCount(0)
      expect(keys).toEqual([])
      await page.screenshot({
        path: testInfo.outputPath('recording-compression.png'),
        fullPage: true,
      })
      return
    }
    await panel.getByRole('button', { name: '暂停压缩' }).click()
    await expect(panel).toContainText('请求已入队，等待推理机领取')
    await expect(panel).not.toContainText('已暂停')
    complete = true
    await panel.getByRole('button', { name: '查询命令结果' }).click()
    await expect(panel).toContainText('已暂停')
    await expect(panel).toContainText('0.5 倍实时速率')
    for (const [name, label] of [
      ['恢复压缩', '正在压缩'],
      ['中止压缩', '已中止'],
    ] as const) {
      await panel.getByRole('button', { name }).click()
      complete = true
      await panel.getByRole('button', { name: '查询命令结果' }).click()
      await expect(panel).toContainText(label)
    }
    expect(keys.every(Boolean)).toBe(true)
    await page.screenshot({
      path: testInfo.outputPath('recording-compression.png'),
      fullPage: true,
    })
  })
}
