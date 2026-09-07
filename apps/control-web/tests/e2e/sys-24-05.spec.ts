import { expect, test, type Page } from '@playwright/test'

const ADMIN_SESSION = {
  user_id: '018f0000-0000-7000-8000-00000000a501',
  login_name: 'wang.admin',
  display_name: '王管理员',
  expires_at: '2026-09-07T13:00:00Z',
  permissions: [
    'device.connector.view',
    'device.connector.edit',
    'device.connector.delete',
    'device.inference_host.view',
    'device.station.view',
  ],
}

const CREDENTIALS = {
  login_name: 'wang.admin',
  password: 'first-shift-key', // pragma: allowlist secret
}

function envelope<T>(items: T[]) {
  return { items, page: 1, page_size: 50, total: items.length }
}

interface ConnectorFixture {
  id: string
  station_id: string
  host_id: string
  name: string
  connector_type: 'hikvision_isapi'
  configuration: { address: string; port: number }
  credentials_configured: boolean
  reachability: 'unverified'
  health_detail: null
  status: 'active' | 'deactivated'
  revision: number
}

async function mockControlPlane(page: Page, sessionPayload = ADMIN_SESSION) {
  let signedIn = false
  const deleteRequests: { url: string; ifMatch: string | null }[] = []
  let connector: ConnectorFixture | null = {
    id: 'connector-1',
    station_id: 'station-1',
    host_id: 'host-1',
    name: '一号连接器',
    connector_type: 'hikvision_isapi' as const,
    configuration: { address: '10.0.8.21', port: 80 },
    credentials_configured: false,
    reachability: 'unverified',
    health_detail: null,
    status: 'active' as 'active' | 'deactivated',
    revision: 1,
  }

  const json = (status: number, body: unknown, cookies: string[] = []) => ({
    status,
    contentType: 'application/json',
    body: JSON.stringify(body),
    cookies,
  })

  await page.route('**/api/v1/auth/session', async (route) => {
    if (route.request().method() === 'POST') {
      signedIn = true
      await route.fulfill(
        json(201, sessionPayload, [
          'sop_session=browser-session; Path=/; HttpOnly; SameSite=Strict',
          'sop_csrf=browser-csrf; Path=/; SameSite=Strict',
        ]),
      )
      return
    }
    if (!signedIn) {
      await route.fulfill({
        status: 401,
        contentType: 'application/problem+json',
        body: JSON.stringify({ title: '请先登录', error_code: 'AUTHENTICATION_REQUIRED' }),
      })
      return
    }
    await route.fulfill(json(200, sessionPayload))
  })

  await page.route('**/api/v1/inference-hosts', async (route) => {
    await route.fulfill(
      json(
        200,
        envelope([
          {
            id: 'host-1',
            name: '推理机 A',
            address: '10.0.8.11',
            mediamtx_address: null,
            recording_window_seconds: 604800,
            disk_watermark_percent: 85,
            status: 'active',
            revision: 1,
          },
        ]),
      ),
    )
  })

  await page.route('**/api/v1/stations', async (route) => {
    await route.fulfill(
      json(
        200,
        envelope([
          {
            id: 'station-1',
            code: 'A-001',
            name: '一号装配工位',
            tags: [],
            status: 'active',
            revision: 1,
          },
        ]),
      ),
    )
  })

  await page.route('**/api/v1/connectors**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    const parts = url.pathname.split('/').filter(Boolean)
    const connectorId = parts.length === 4 ? parts[3] : undefined

    if (request.method() === 'GET') {
      if (connectorId) {
        await route.fulfill(
          connector
            ? json(200, connector)
            : json(404, { title: '未找到连接器', error_code: 'CONNECTOR_NOT_FOUND' }),
        )
      } else {
        await route.fulfill(json(200, envelope(connector ? [connector] : [])))
      }
      return
    }

    if (request.method() === 'POST') {
      const submitted = request.postDataJSON() as Omit<
        ConnectorFixture,
        'id' | 'revision' | 'status' | 'credentials_configured' | 'reachability' | 'health_detail'
      >
      connector = {
        ...(connector ?? {}),
        ...submitted,
        id: 'connector-2',
        revision: 1,
        status: 'active',
        credentials_configured: false,
        reachability: 'unverified',
        health_detail: null,
      }
      await route.fulfill(json(201, connector))
      return
    }

    if (request.method() === 'DELETE') {
      deleteRequests.push({
        url: request.url(),
        ifMatch: request.headers()['if-match'] ?? null,
      })
    }

    if (connector === null) {
      await route.fulfill(json(404, { title: '未找到连接器', error_code: 'CONNECTOR_NOT_FOUND' }))
      return
    }

    if (request.method() === 'PATCH') {
      connector = {
        ...connector,
        ...(request.postDataJSON() as Partial<ConnectorFixture>),
        revision: connector.revision + 1,
        reachability: 'unverified',
      }
      await route.fulfill(json(200, connector))
      return
    }

    if (request.method() === 'PUT') {
      connector = {
        ...connector,
        ...(request.postDataJSON() as { status: 'active' | 'deactivated' }),
        revision: connector.revision + 1,
      }
      await route.fulfill(json(200, connector))
      return
    }

    if (request.method() === 'DELETE') {
      connector = null
      await route.fulfill({ status: 204 })
    }
  })

  return { deleteRequests }
}

test('SYS-24-05 — an operator manages a connector without secrets or a test action', async ({
  page,
}) => {
  await mockControlPlane(page)

  await page.goto('/login')
  await page.getByRole('textbox', { name: '登录名' }).fill(CREDENTIALS.login_name)
  await page.getByLabel('密码').fill(CREDENTIALS.password)
  await page.getByRole('button', { name: '登录' }).click()
  await page.getByRole('navigation', { name: '主导航' }).getByText('工位与设备').click()

  const row = page.getByRole('row', { name: /一号连接器/ })
  await expect(row).toContainText('一号装配工位')
  await expect(row).toContainText('推理机 A')
  await expect(row).toContainText('未验证')
  await expect(page.getByRole('button', { name: '测试连接' })).toHaveCount(0)

  await page.getByRole('button', { name: '新建连接器' }).click()
  const dialog = page.getByRole('dialog', { name: '新建连接器' })
  await dialog.getByLabel('名称').fill('新连接器')
  await dialog.getByLabel('地址').fill('10.0.8.22')
  await dialog.getByLabel('端口（可选）').fill('8080')
  await expect(dialog.getByLabel('密码')).toHaveCount(0)
  await dialog.getByRole('button', { name: '创建' }).click()
  await expect(page.getByRole('row', { name: /新连接器/ })).toBeVisible()

  const createdRow = page.getByRole('row', { name: /新连接器/ })
  await createdRow.getByRole('button', { name: '编辑' }).click()
  const editDialog = page.getByRole('dialog', { name: '编辑连接器' })
  await editDialog.getByLabel('地址').fill('10.0.8.23')
  await editDialog.getByRole('button', { name: '保存' }).click()
  await expect(page.getByRole('row', { name: /新连接器/ })).toContainText('10.0.8.23')

  await page
    .getByRole('row', { name: /新连接器/ })
    .getByRole('button', { name: '停用' })
    .click()
  await expect(page.getByRole('row', { name: /新连接器/ })).toContainText('已停用')
  await page
    .getByRole('row', { name: /新连接器/ })
    .getByRole('button', { name: '恢复' })
    .click()
  await expect(page.getByRole('row', { name: /新连接器/ })).toContainText('在用')

  await page
    .getByRole('row', { name: /新连接器/ })
    .getByRole('button', { name: '详情' })
    .click()
  await expect(page.getByRole('dialog', { name: '连接器详情' })).toContainText('10.0.8.23')
  await expect(page.getByRole('dialog', { name: '连接器详情' })).toContainText('未验证')
  await page.keyboard.press('Escape')

  await page
    .getByRole('row', { name: /新连接器/ })
    .getByRole('button', { name: '删除' })
    .click()
  await expect(page.getByText('还没有连接器配置。')).toBeVisible()
})

test('a known-identifier delete submits exactly once from a real browser click', async ({
  page,
}) => {
  const controlPlane = await mockControlPlane(page, {
    ...ADMIN_SESSION,
    permissions: ['device.connector.delete'],
  })

  await page.goto('/login')
  await page.getByRole('textbox', { name: '登录名' }).fill(CREDENTIALS.login_name)
  await page.getByLabel('密码').fill(CREDENTIALS.password)
  await page.getByRole('button', { name: '登录' }).click()
  await page.getByRole('navigation', { name: '主导航' }).getByText('工位与设备').click()

  await page.getByLabel('连接器 ID').fill('connector-1')
  await page.getByLabel('已知修订号').fill('7')
  await Promise.all([
    page.waitForResponse(
      (response) => response.request().method() === 'DELETE' && response.status() === 204,
    ),
    page.getByRole('button', { name: '删除' }).click(),
  ])
  await page.waitForLoadState('networkidle')

  expect(controlPlane.deleteRequests).toHaveLength(1)
  expect(controlPlane.deleteRequests[0]).toEqual({
    url: expect.stringContaining('/api/v1/connectors/connector-1'),
    ifMatch: '7',
  })
})
