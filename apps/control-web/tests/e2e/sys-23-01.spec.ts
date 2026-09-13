import { expect, test, type Page } from '@playwright/test'

/**
 * SYS-23-01 — 用户与权限, as a system scenario through the browser.
 *
 * 从登录开始，管理员经生成客户端完成角色创建、账户创建、多角色分配，并在停用时看到该账户
 * 的会话被同一下线。控制面 API 按其已发布契约打桩（信封、problem+json、`Set-Cookie`），页面
 * 本身跑的是真实组件与真实客户端代码；后端对同一批用例的行为由 `tests/system/` 的 SYS-23
 * 场景对着真实部署证明。
 *
 * The routes below fulfil the contract the backend publishes: list envelope, `problem+json`
 * errors, and the two cookies the double-submit CSRF flow reads. State is kept by the test so a
 * change made through the UI is what the next read returns.
 */

const ADMIN_SESSION = {
  user_id: '018f0000-0000-7000-8000-00000000c201',
  login_name: 'wang.admin',
  display_name: '王管理员',
  expires_at: '2026-09-07T13:00:00Z',
  permissions: [
    'auth.role.delete',
    'auth.role.edit',
    'auth.role.view',
    'auth.user.delete',
    'auth.user.edit',
    'auth.user.view',
  ],
}

const CATALOGUE = [
  'auth.role.delete',
  'auth.role.edit',
  'auth.role.view',
  'auth.user.delete',
  'auth.user.edit',
  'auth.user.view',
]

const CREDENTIALS = {
  login_name: 'wang.admin',
  password: 'first-shift-key', // pragma: allowlist secret
}

function envelope<T>(items: T[]) {
  return { items, page: 1, page_size: 50, total: items.length }
}

/** The deployment state the mocked routes serve and mutate, as the backend's tables would. */
function freshState() {
  const adminRole = {
    id: '018f0000-0000-7000-8000-00000000ff01',
    code: 'system_administrator',
    name: '系统管理员',
    permissions: [...CATALOGUE],
  }
  return {
    roles: [adminRole],
    users: [
      {
        id: '018f0000-0000-7000-8000-00000000c201',
        login_name: 'wang.admin',
        display_name: '王管理员',
        status: 'active' as 'active' | 'deactivated',
        role_ids: [adminRole.id],
      },
    ],
    adminRole,
  }
}

async function mockTheControlPlane(page: Page, state: ReturnType<typeof freshState>) {
  const json = (status: number, body: unknown, cookies: string[] = []) => ({
    status,
    contentType: 'application/json',
    body: JSON.stringify(body),
    cookies,
  })

  // Nobody is signed in until the login form submits — the router guard asks first, and a
  // 200 here would bounce the login page to the overview before the operator can type.
  let signedIn = false

  await page.route('**/api/v1/auth/session', async (route) => {
    if (route.request().method() === 'POST') {
      signedIn = true
      await route.fulfill({
        ...json(201, ADMIN_SESSION, [
          'sop_session=mocked-session; Path=/; HttpOnly; SameSite=Strict',
          'sop_csrf=mocked-csrf; Path=/; SameSite=Strict',
        ]),
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
    await route.fulfill(json(200, ADMIN_SESSION))
  })

  await page.route('**/api/v1/auth/permissions', async (route) => {
    await route.fulfill(json(200, envelope(CATALOGUE)))
  })

  await page.route('**/api/v1/auth/roles', async (route) => {
    if (route.request().method() === 'POST') {
      const submitted = route.request().postDataJSON() as {
        code: string
        name: string
        permissions: string[]
      }
      const created = { id: `role-${state.roles.length + 1}`, ...submitted }
      state.roles.push(created)
      await route.fulfill(json(201, created))
      return
    }
    await route.fulfill(json(200, envelope(state.roles)))
  })

  await page.route('**/api/v1/auth/users', async (route) => {
    if (route.request().method() === 'POST') {
      const submitted = route.request().postDataJSON() as {
        login_name: string
        display_name: string
      }
      const created = {
        id: `user-${state.users.length + 1}`,
        login_name: submitted.login_name,
        display_name: submitted.display_name,
        status: 'active' as const,
        role_ids: [],
      }
      state.users.push(created)
      await route.fulfill(json(201, created))
      return
    }
    await route.fulfill(json(200, envelope(state.users)))
  })

  await page.route('**/api/v1/auth/users/*/roles', async (route) => {
    const submitted = route.request().postDataJSON() as { role_ids: string[] }
    const id = route.request().url().split('/').at(-2)!
    const user = state.users.find((candidate) => candidate.id === id)
    if (user) {
      user.role_ids = submitted.role_ids
    }
    await route.fulfill(json(200, { ...user!, role_ids: submitted.role_ids }))
  })

  await page.route('**/api/v1/auth/users/*/status', async (route) => {
    const submitted = route.request().postDataJSON() as { status: 'active' | 'deactivated' }
    const id = route.request().url().split('/').at(-2)!
    const user = state.users.find((candidate) => candidate.id === id)
    if (user) {
      user.status = submitted.status
    }
    await route.fulfill(
      json(200, {
        user: { ...user!, status: submitted.status },
        // 停用 closes the account's live sessions; the screen says how many.
        revoked_sessions: submitted.status === 'deactivated' ? 1 : 0,
      }),
    )
  })
}

test('SYS-23-01 — an administrator creates a role, an account, assigns it, and deactivates', async ({
  page,
}) => {
  const state = freshState()
  await mockTheControlPlane(page, state)

  await page.goto('/login')
  await page.getByRole('textbox', { name: '登录名' }).fill(CREDENTIALS.login_name)
  await page.getByLabel('密码').fill(CREDENTIALS.password)
  await page.getByRole('button', { name: '登录' }).click()
  await expect(page.getByRole('banner').getByText('王管理员')).toBeVisible()

  // The navigation item exists because this caller holds the `auth` view permissions.
  await page.getByRole('navigation', { name: '主导航' }).getByText('用户与权限').click()
  await expect(page.getByRole('heading', { name: '用户与权限' })).toBeVisible()

  // A role is created from the catalogue's checkboxes.
  await page.getByRole('tab', { name: '角色' }).click()
  await page.getByRole('button', { name: '新建角色' }).click()
  await page.getByRole('dialog').getByLabel('编码').fill('dataset_manager')
  await page.getByRole('dialog').getByLabel('名称').fill('数据集管理者')
  // The native input Element Plus renders is visually hidden; the label is what a person
  // clicks, and it is what this clicks too.
  await page.getByRole('dialog').getByText('auth.user.view').click()
  await page.getByRole('dialog').getByRole('button', { name: '保存' }).click()
  await expect(page.getByRole('cell', { name: 'dataset_manager' })).toBeVisible()

  // An account is created; it can log in immediately and holds no roles yet.
  await page.getByRole('tab', { name: '账户' }).click()
  await page.getByRole('button', { name: '新建账户' }).click()
  await page.getByRole('dialog').getByLabel('登录名').fill('zhao.min')
  await page.getByRole('dialog').getByLabel('姓名').fill('赵敏')
  await page.getByRole('dialog').getByLabel('初始密码').fill('assembly-line-3') // pragma: allowlist secret
  await page.getByRole('dialog').getByRole('button', { name: '创建' }).click()
  await expect(page.getByRole('cell', { name: 'zhao.min' })).toBeVisible()

  // Several roles in one submission: the administrator submits the whole set.
  const row = page.getByRole('row', { name: /zhao\.min/ })
  await row.getByRole('button', { name: '分配角色' }).click()
  // The multi-select opens from its wrapper; its options land in a body-level dropdown.
  await page.getByRole('dialog').locator('.el-select').click()
  await page.getByRole('option', { name: '系统管理员' }).click()
  await page.getByRole('option', { name: '数据集管理者' }).click()
  // The open dropdown overlays the dialog's own footer; toggling it closed is what a person
  // does before reaching for 保存.
  await page.getByRole('dialog').locator('.el-select').click()
  await page.getByRole('dialog').getByRole('button', { name: '保存' }).click()
  await expect(page.getByRole('row', { name: /zhao\.min/ })).toContainText(
    '系统管理员、数据集管理者',
  )

  // Deactivation closes the account's live sessions, and the screen says so.
  await page
    .getByRole('row', { name: /zhao\.min/ })
    .getByRole('button', { name: '停用' })
    .click()
  await expect(page.getByText('已停用，同时下线 1 个会话')).toBeVisible()
  await expect(page.getByRole('row', { name: /zhao\.min/ })).toContainText('已停用')
})
