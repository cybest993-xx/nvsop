import { expect, test, type Page } from '@playwright/test'

/**
 * SYS-81 — 前端待派发 DDM 训练命令取消交互验收。
 *
 * 验证：
 * 1. 具备 dataset.dataset.edit 权限的操作员可取消 pending 命令，并展示持久化的取消人与时间；
 * 2. 派发冲突（HTTP 409 USAGE_STATE_CONFLICT）时展示真实冲突原因，且不伪造取消状态；
 * 3. 仅具有 view 或 import 权限时不提供取消命令操作。
 */

const DATASET_ID = '018f0000-0000-7000-8000-00000000d401'
const COMMAND_ID = '018f0000-0000-7000-8000-00000000d402'

const SESSION = {
  user_id: '018f0000-0000-7000-8000-00000000d201',
  login_name: 'zhang.operator',
  display_name: '张操作员',
  expires_at: '2026-09-07T13:00:00Z',
  permissions: ['dataset.dataset.view', 'dataset.dataset.edit', 'dataset.dataset.import'],
}

const CREDENTIALS = { login_name: 'zhang.operator', password: 'first-shift-key' } // pragma: allowlist secret

const DATASET = {
  id: DATASET_ID,
  name: '装配训练集',
  created_by: SESSION.user_id,
  updated_by: SESSION.user_id,
  created_at: '2026-09-08T01:00:00Z',
  updated_at: '2026-09-08T01:00:00Z',
}

const PENDING_COMMAND = {
  id: COMMAND_ID,
  dataset_id: DATASET_ID,
  status: 'pending',
  request_digest: 'e'.repeat(64),
  base_job_id: null,
  base_status: null,
  base_progress: null,
  base_current_step: null,
  base_total_steps: null,
  last_observed_at: null,
  observation_failure_code: null,
  observation_failure_detail: null,
  failure_code: null,
  failure_detail: null,
  result_status: 'not_registered',
  result_generation: 0,
  result_config_sha256: null,
  result_config_size: null,
  result_checkpoint_sha256: null,
  result_checkpoint_size: null,
  result_failure_code: null,
  result_failure_detail: null,
  result_registered_at: null,
  cancelled_by: null as string | null,
  cancelled_at: null as string | null,
  created_by: SESSION.user_id,
  created_at: '2026-09-08T01:00:00Z',
  updated_at: '2026-09-08T01:00:00Z',
}

function envelope<T>(items: T[]) {
  return { items, page: 1, page_size: 50, total: items.length }
}

async function mockControlPlane(
  page: Page,
  options: {
    permissions?: string[]
    cancelConflict?: boolean
  } = {},
) {
  const requests: { url: string; method: string }[] = []
  page.on('request', (request) => requests.push({ url: request.url(), method: request.method() }))
  let signedIn = false
  const currentPermissions = options.permissions ?? SESSION.permissions
  let currentCommand = { ...PENDING_COMMAND }

  await page.route(/\/api\/v1\/.*/, async (route) => {
    if (route.request().method() !== 'GET') {
      await route.fulfill({
        status: 404,
        contentType: 'application/problem+json',
        body: JSON.stringify({ title: '未打桩的写接口', error_code: 'NOT_FOUND' }),
      })
      return
    }
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(envelope([])),
    })
  })

  await page.route('**/api/v1/auth/session', async (route) => {
    if (route.request().method() === 'POST') {
      signedIn = true
      await route.fulfill({
        status: 201,
        contentType: 'application/json',
        headers: {
          'set-cookie':
            'sop_session=mocked-session; Path=/; HttpOnly; SameSite=Strict\n' +
            'sop_csrf=mocked-csrf; Path=/; SameSite=Strict',
        },
        body: JSON.stringify({ ...SESSION, permissions: currentPermissions }),
      })
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
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ ...SESSION, permissions: currentPermissions }),
    })
  })

  await page.route('**/api/v1/auth/permissions', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(envelope(currentPermissions)),
    })
  })

  await page.route(new RegExp('/api/v1/training-datasets(\\?.*)?$'), async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(envelope([DATASET])),
    })
  })

  await page.route(
    new RegExp(`/api/v1/training-datasets/${DATASET_ID}/ddm-training-commands(\\?.*)?$`),
    async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(envelope([currentCommand])),
      })
    },
  )

  await page.route(
    `**/api/v1/training-datasets/${DATASET_ID}/ddm-training-commands/${COMMAND_ID}/cancel`,
    async (route) => {
      if (options.cancelConflict) {
        await route.fulfill({
          status: 409,
          contentType: 'application/problem+json',
          body: JSON.stringify({
            title: '训练数据集用途状态冲突',
            error_code: 'USAGE_STATE_CONFLICT',
            detail: '命令已进入派发，不能按未派发取消；该结果不证明基座已停止',
          }),
        })
        return
      }
      currentCommand = {
        ...currentCommand,
        status: 'cancelled',
        cancelled_by: SESSION.user_id,
        cancelled_at: '2026-09-08T01:05:00Z',
        updated_at: '2026-09-08T01:05:00Z',
      }
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(currentCommand),
      })
    },
  )

  return requests
}

async function signIn(page: Page) {
  await page.goto('/login')
  await page.getByLabel('登录名').fill(CREDENTIALS.login_name)
  await page.getByLabel('密码').fill(CREDENTIALS.password)
  await page.getByRole('button', { name: '登录' }).click()
  await expect(page.getByRole('banner')).toBeVisible()
}

test('SYS-81 — 有权限用户取消待派发命令，更新持久化状态与取消人/时间', async ({ page }) => {
  const requests = await mockControlPlane(page)
  await signIn(page)
  await page.goto('/training-datasets')

  await expect(page.getByRole('heading', { name: 'DDM 训练命令' })).toBeVisible()
  await expect(page.getByText('已受理，待派发')).toBeVisible()

  const cancelButton = page.getByRole('button', { name: '取消命令' })
  await expect(cancelButton).toBeVisible()

  await cancelButton.click()

  // 权威响应更新持久化事实
  await expect(page.getByText('已取消（未派发）')).toBeVisible()
  await expect(page.getByText(`取消人：${SESSION.user_id}`)).toBeVisible()
  await expect(page.getByText(/取消时间：/)).toBeVisible()
  await expect(page.getByRole('button', { name: '取消命令' })).toHaveCount(0)

  // 验证 cancel 请求确实发送
  expect(
    requests.some(
      (r) => r.method === 'POST' && r.url.includes(`/ddm-training-commands/${COMMAND_ID}/cancel`),
    ),
  ).toBe(true)
})

test('SYS-81 — 已派发冲突（409）时显示真实冲突说明，不伪造已取消状态', async ({ page }) => {
  await mockControlPlane(page, { cancelConflict: true })
  await signIn(page)
  await page.goto('/training-datasets')

  await expect(page.getByRole('heading', { name: 'DDM 训练命令' })).toBeVisible()
  await expect(page.getByText('已受理，待派发')).toBeVisible()

  const cancelButton = page.getByRole('button', { name: '取消命令' })
  await expect(cancelButton).toBeVisible()
  await cancelButton.click()

  // 必须显示真实的冲突报错与说明
  const alert = page.getByRole('alert')
  await expect(alert).toBeVisible()
  await expect(alert).toContainText('命令已进入派发，不能按未派发取消；该结果不证明基座已停止')
  await expect(alert).toContainText('USAGE_STATE_CONFLICT')

  // 应用状态保持原值，不伪造为已取消
  await expect(page.getByText('已受理，待派发')).toBeVisible()
  await expect(page.getByText('已取消（未派发）')).toHaveCount(0)
})

test('SYS-81 — 仅具备 view 或 import 权限时不暴露取消按钮', async ({ page }) => {
  await mockControlPlane(page, { permissions: ['dataset.dataset.view', 'dataset.dataset.import'] })
  await signIn(page)
  await page.goto('/training-datasets')

  await expect(page.getByRole('heading', { name: 'DDM 训练命令' })).toBeVisible()
  await expect(page.getByText('已受理，待派发')).toBeVisible()

  // 无编辑权限：不展示取消命令按钮
  await expect(page.getByRole('button', { name: '取消命令' })).toHaveCount(0)
})
