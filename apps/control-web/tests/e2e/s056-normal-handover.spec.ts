import { expect, test, type Page } from '@playwright/test'

const record = {
  handover_id: '018f0000-0000-7000-8000-00000000a101',
  station_id: '018f0000-0000-7000-8000-00000000a102',
  from_host_id: '018f0000-0000-7000-8000-00000000a103',
  to_host_id: '018f0000-0000-7000-8000-00000000a104',
  grant_id: '018f0000-0000-7000-8000-00000000a105',
  command_id: '018f0000-0000-7000-8000-00000000a106',
  requested_by: 'operator',
  requested_at: '2026-10-17T00:00:00Z',
  expires_at: '2026-10-17T00:05:00Z',
  idempotency_key: 'browser-key',
  status: 'pending',
  confirmed_at: null as string | null,
  cancelled_at: null,
  timed_out_at: null,
  failed_at: null,
  failure_code: null,
  failure_detail: null,
  new_grant_id: null as string | null,
  granted_at: null as string | null,
  granted_by: null,
}

async function arrange(
  page: Page,
  options: { grantConflict?: boolean; mayEdit?: boolean; mayMonitor?: boolean } = {},
) {
  let current = { ...record }
  const grants: string[] = []
  await page.route('**/api/v1/**', async (route) => {
    const path = new URL(route.request().url()).pathname
    let body: unknown = { items: [], total: 0, page: 1, page_size: 50 }
    let status = 200
    if (path === '/api/v1/auth/session') {
      body = {
        user_id: 'operator',
        login_name: 'operator',
        display_name: '交权管理员',
        permissions: [
          ...(options.mayEdit === false ? [] : ['execution.handover.edit']),
          ...(options.mayMonitor ? ['monitor.report.view'] : []),
        ],
        expires_at: '2099-01-01T00:00:00Z',
      }
    } else if (path.endsWith('/handovers/risk')) {
      body = { risk_statement: '若旧机仍在运行将造成重复物理写入' }
    } else if (path.endsWith('/normal-handovers')) {
      current = { ...record, ...route.request().postDataJSON() }
      body = current
      status = 201
    } else if (path.endsWith('/grant')) {
      grants.push(route.request().method())
      if (options.grantConflict) {
        status = 409
        body = { title: '停写确认已陈旧', error_code: 'NORMAL_HANDOVER_STALE_CONFIRMATION' }
      } else {
        current = {
          ...current,
          new_grant_id: '018f0000-0000-7000-8000-00000000a107',
          granted_at: '2026-10-17T00:01:00Z',
        }
        body = current
      }
    } else if (path.includes('/normal-handovers/')) {
      body = current
    }
    await route.fulfill({
      status,
      contentType: status === 409 ? 'application/problem+json' : 'application/json',
      body: JSON.stringify(body),
    })
  })
  await page.goto('/devices')
  return {
    grants,
    confirm: () => {
      current = { ...current, status: 'confirmed', confirmed_at: '2026-10-17T00:00:30Z' }
    },
  }
}

async function requestHandover(page: Page) {
  const panel = page.getByRole('region', { name: '正常交权', exact: true })
  await panel.getByRole('textbox', { name: '交权工位', exact: true }).fill(record.station_id)
  await panel.getByRole('textbox', { name: '旧推理机', exact: true }).fill(record.from_host_id)
  await panel.getByRole('textbox', { name: '新推理机', exact: true }).fill(record.to_host_id)
  await panel.getByRole('button', { name: '请求旧机交权', exact: true }).click()
  await expect(panel.getByText('等待旧机停写确认', { exact: true })).toBeVisible()
  await expect(panel.getByRole('button', { name: '授予新机执行权', exact: true })).toHaveCount(0)
  return panel
}

test('正常交权在旧机确认后授予新机，并保留运行状态权限边界', async ({ page }, info) => {
  const service = await arrange(page)
  const panel = await requestHandover(page)
  service.confirm()
  await panel.getByRole('button', { name: '查询 / 刷新交权', exact: true }).click()
  await expect(panel.getByText('旧机已停写，等待授予新机', { exact: true })).toBeVisible()
  await panel.getByRole('button', { name: '授予新机执行权', exact: true }).click()
  await expect(panel.getByText('已授予新机执行权', { exact: true })).toBeVisible()
  await expect(
    panel.getByText('已授予新机；当前账户无运行状态查看权限', { exact: true }),
  ).toBeVisible()
  expect(service.grants).toEqual(['POST'])
  await panel.screenshot({ path: info.outputPath('normal-handover.png') })
})

test('运行镜像必须匹配新机与新授权才显示写入有效，断流后恢复未知', async ({ page }) => {
  await page.addInitScript(() => {
    class RuntimeStream extends EventTarget {
      onopen: (() => void) | null = null
      onerror: (() => void) | null = null
      constructor() {
        super()
        window.addEventListener('handover-runtime', (event) => {
          this.onopen?.()
          this.dispatchEvent(
            new MessageEvent('runtime', { data: JSON.stringify((event as CustomEvent).detail) }),
          )
        })
        window.addEventListener('handover-disconnected', () => this.onerror?.())
      }
      close() {}
    }
    Object.defineProperty(window, 'EventSource', { value: RuntimeStream })
  })
  const service = await arrange(page, { mayMonitor: true })
  const panel = await requestHandover(page)
  service.confirm()
  await panel.getByRole('button', { name: '查询 / 刷新交权', exact: true }).click()
  await panel.getByRole('button', { name: '授予新机执行权', exact: true }).click()
  const waiting = panel.getByText('等待新机确认配置与上报写入状态', { exact: true })
  await expect(waiting).toBeVisible()
  async function report(hostId: string, grantId: string, stale = false) {
    await page.evaluate(
      ({ stationId, hostId, grantId, stale }) => {
        window.dispatchEvent(
          new CustomEvent('handover-runtime', {
            detail: {
              station_id: stationId,
              physical_safety: {
                status: 'protected',
                detail: '合成运行镜像',
                center_authorization: {
                  state: 'active',
                  grant_id: grantId,
                  holder_host_id: hostId,
                },
                edge_status: {
                  host_id: hostId,
                  grant_id: grantId,
                  holder_host_id: hostId,
                  stale,
                  authority_state: 'active',
                  write_state: 'enabled',
                  reason_code: null,
                  detail: null,
                  lease_expires_at: null,
                  renewed_at: null,
                  reported_at: null,
                  received_at: null,
                },
              },
            },
          }),
        )
      },
      { stationId: record.station_id, hostId, grantId, stale },
    )
  }
  const grantId = '018f0000-0000-7000-8000-00000000a107'
  await report(record.from_host_id, grantId)
  await expect(waiting).toBeVisible()
  await report(record.to_host_id, record.grant_id)
  await expect(waiting).toBeVisible()
  await report(record.to_host_id, grantId, true)
  await expect(waiting).toBeVisible()
  await report(record.to_host_id, grantId)
  await expect(panel.getByText('新机已确认配置，物理写入门禁有效', { exact: true })).toBeVisible()
  await page.evaluate(() => window.dispatchEvent(new Event('handover-disconnected')))
  await expect(waiting).toBeVisible()
})

test('授予冲突展示后端拒绝原因，保留真实进度', async ({ page }) => {
  const service = await arrange(page, { grantConflict: true })
  const panel = await requestHandover(page)
  service.confirm()
  await panel.getByRole('button', { name: '查询 / 刷新交权', exact: true }).click()
  await panel.getByRole('button', { name: '授予新机执行权', exact: true }).click()
  await expect(panel.getByRole('alert')).toContainText('NORMAL_HANDOVER_STALE_CONFIRMATION')
  await expect(panel.getByText('旧机已停写，等待授予新机', { exact: true })).toBeVisible()
})

test('无交权权限不挂载正常交权操作', async ({ page }) => {
  await arrange(page, { mayEdit: false })
  await expect(page.getByRole('heading', { name: '正常交权', exact: true })).toHaveCount(0)
})
