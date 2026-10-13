import { expect, test, type Page } from '@playwright/test'

/**
 * SYS-74 — 重开训练数据集页面从服务端恢复持久 DDM 训练命令。
 *
 * 页面跑的是真实组件与真实生成客户端；控制面 API 按其已发布契约打桩（列表信封、双提交
 * CSRF Cookie、problem+json 失败）。这里只补充浏览器可见的重开/失败/手动刷新契约，真实
 * PostgreSQL/文件/DDM 查询与 8444 只读结果访问由 `tests/system/test_sys_74_ddm_command_result.py`
 * 对外部 seam 证明；完整训练面板仍由 #241/#258 承担。
 */

const DATASET_ID = '018f0000-0000-7000-8000-00000000d401'
const COMMAND_ID = '018f0000-0000-7000-8000-00000000d402'
const BASE_JOB_ID = '018f0000-0000-7000-8000-00000000d403'

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

/** 基座返回未知原状态、查询失败诊断仍在，结果尚未登记：三个事实必须分别显示。 */
const BOUND_COMMAND = {
  id: COMMAND_ID,
  dataset_id: DATASET_ID,
  status: 'bound',
  request_digest: 'e'.repeat(64),
  base_job_id: BASE_JOB_ID,
  base_status: 'telemetry-paused',
  base_progress: 12.5,
  base_current_step: 3,
  base_total_steps: 24,
  last_observed_at: '2026-09-08T01:05:00Z',
  observation_failure_code: 'BASE_OBSERVATION_UNAVAILABLE',
  observation_failure_detail: '无法查询基座原任务状态（HTTP 503）',
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
  created_by: SESSION.user_id,
  created_at: '2026-09-08T01:00:00Z',
  updated_at: '2026-09-08T01:05:00Z',
}

function envelope<T>(items: T[]) {
  return { items, page: 1, page_size: 50, total: items.length }
}

/** 登录、列表与命令查询都按发布契约打桩；第一次命令查询可由场景切换为失败。 */
async function mockControlPlane(page: Page, options: { failCommandsOnce?: boolean } = {}) {
  const requests: { url: string; method: string }[] = []
  page.on('request', (request) => requests.push({ url: request.url(), method: request.method() }))
  let signedIn = false
  let commandFailuresRemaining = options.failCommandsOnce === true ? 1 : 0

  // 未在本场景用到的读接口返回空信封，避免页面等不到响应。
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
        body: JSON.stringify(SESSION),
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
      body: JSON.stringify(SESSION),
    })
  })
  await page.route('**/api/v1/auth/permissions', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(envelope(SESSION.permissions)),
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
      if (commandFailuresRemaining > 0) {
        commandFailuresRemaining -= 1
        await route.fulfill({
          status: 503,
          contentType: 'application/problem+json',
          body: JSON.stringify({
            title: '训练命令查询暂时不可用',
            error_code: 'STORAGE_UNAVAILABLE',
          }),
        })
        return
      }
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(envelope([BOUND_COMMAND])),
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

function countCommandReads(requests: { url: string; method: string }[]): number {
  return requests.filter(
    (request) =>
      request.method === 'GET' &&
      /\/ddm-training-commands(\?.*)?$/.test(new URL(request.url).pathname),
  ).length
}

test('SYS-74 — 重开页面从服务端恢复命令，未知基座状态原样显示且结果独立', async ({ page }) => {
  const requests = await mockControlPlane(page)
  await signIn(page)
  await page.goto('/training-datasets')

  await expect(page.getByRole('heading', { name: 'DDM 训练命令' })).toBeVisible()
  // 应用状态、基座未知原状态、查询诊断与结果登记是四个独立事实。
  await expect(page.getByText('已绑定基座任务')).toBeVisible()
  await expect(page.getByText('未知基座状态（telemetry-paused）')).toBeVisible()
  await expect(page.getByText('BASE_OBSERVATION_UNAVAILABLE')).toBeVisible()
  await expect(page.getByText('结果未登记')).toBeVisible()
  // 结果未登记时没有已授权读取通道的链接。
  await expect(page.getByRole('button', { name: '读取结果' })).toHaveCount(0)

  const firstLoadReads = countCommandReads(requests)
  expect(firstLoadReads).toBeGreaterThanOrEqual(1)

  // 重开页面：原命令再次来自服务端 GET，不依赖页面内缓存，也不启动训练。
  await page.reload()
  await expect(page.getByRole('heading', { name: 'DDM 训练命令' })).toBeVisible()
  await expect.poll(() => countCommandReads(requests)).toBeGreaterThan(firstLoadReads)
  await expect(page.getByText('未知基座状态（telemetry-paused）')).toBeVisible()
  expect(requests.some((request) => request.url.includes('/fine-tuning/start'))).toBe(false)
})

test('SYS-74 — 命令查询失败显示失败态，手动刷新只重新读取而不启动训练', async ({ page }) => {
  const requests = await mockControlPlane(page, { failCommandsOnce: true })
  await signIn(page)
  await page.goto('/training-datasets')

  // 查询失败必须显示明确失败态，不能从缓存推断成功。
  const alert = page.getByRole('alert')
  await expect(alert).toBeVisible()
  await expect(alert).toContainText('训练命令查询暂时不可用')
  await expect(alert).toContainText('STORAGE_UNAVAILABLE')
  await expect(page.getByText('DDM 训练命令读取失败，请手动刷新。')).toBeVisible()
  await expect(page.getByText('当前数据集还没有 DDM 训练命令')).toHaveCount(0)

  // 手动刷新只重新 GET；随后显示原命令，仍不触发任何训练操作。
  await page.getByRole('button', { name: '手动刷新' }).click()
  await expect(page.getByText('未知基座状态（telemetry-paused）')).toBeVisible()
  expect(requests.some((request) => request.url.includes('/fine-tuning/start'))).toBe(false)
  expect(
    requests.some(
      (request) => request.method !== 'GET' && /\/ddm-training-commands/.test(request.url),
    ),
  ).toBe(false)
})
