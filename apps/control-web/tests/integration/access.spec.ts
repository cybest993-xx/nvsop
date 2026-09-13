/**
 * 用户与权限, as an administrator meets it.
 *
 * What these assert is the half of AC1–AC3 that lives in the browser: that the screen offers only
 * the operations the caller's permissions include, that the role form's checkboxes come from the
 * backend's catalogue rather than from a list kept in the front end, that a refusal is shown as
 * text, and that deactivating reports how many sessions it closed.
 *
 * The API module is mocked at its own seam — these are about the view's behaviour, and the client
 * itself is covered by `controlPlane.spec.ts` against a stubbed `fetch`.
 */

import ElementPlus from 'element-plus'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'

import { ControlPlaneError } from '@/api/controlPlane'
import AccessView from '@/modules/access/AccessView.vue'
import { useSessionStore } from '@/session/store'

const api = vi.hoisted(() => ({
  readUsers: vi.fn(),
  readRoles: vi.fn(),
  readPermissionCatalogue: vi.fn(),
  readSession: vi.fn(),
  createUser: vi.fn(),
  editUser: vi.fn(),
  resetUserPassword: vi.fn(),
  setUserStatus: vi.fn(),
  setUserRoles: vi.fn(),
  deleteUser: vi.fn(),
  createRole: vi.fn(),
  editRole: vi.fn(),
  deleteRole: vi.fn(),
}))

vi.mock('@/api/controlPlane', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/controlPlane')>()),
  ...api,
}))

/** The §5.15 list envelope the backend serves every listing in. */
function page<T>(items: T[]) {
  return { items, page: 1, page_size: 50, total: items.length }
}

const OPERATOR = {
  id: 'user-1',
  login_name: 'wang.li',
  display_name: '王丽',
  status: 'active' as const,
  role_ids: ['role-1'],
}

const DEACTIVATED = {
  ...OPERATOR,
  id: 'user-2',
  login_name: 'zhao.min',
  status: 'deactivated' as const,
  role_ids: [],
}

const VIEWER_ROLE = {
  id: 'role-1',
  code: 'viewer',
  name: '只读',
  permissions: ['auth.user.view'],
}

const CATALOGUE = [
  'auth.role.delete',
  'auth.role.edit',
  'auth.role.view',
  'auth.user.delete',
  'auth.user.edit',
  'auth.user.view',
]

/** Sign in with exactly these permissions, then mount the page. */
async function mountAs(...permissions: string[]) {
  setActivePinia(createPinia())
  const session = useSessionStore()
  session.current = {
    user_id: 'admin-1',
    login_name: 'administrator',
    display_name: '系统管理员',
    expires_at: '2026-09-07T13:00:00+00:00',
    permissions,
  }
  // The page re-reads the caller's identity after every change it makes; the mock reports the
  // same signed-in state so a refresh never silently strips the session under test.
  api.readSession.mockResolvedValue({ ...session.current })
  const wrapper = mount(AccessView, { global: { plugins: [ElementPlus] } })
  await flushPromises()
  return wrapper
}

beforeEach(() => {
  vi.clearAllMocks()
  api.readUsers.mockResolvedValue(page([OPERATOR, DEACTIVATED]))
  api.readRoles.mockResolvedValue(page([VIEWER_ROLE]))
  api.readPermissionCatalogue.mockResolvedValue(page(CATALOGUE))
})

describe('what the screen offers', () => {
  it('lists accounts including deactivated ones', async () => {
    // 恢复 is reachable only from the listing, so hiding a deactivated account would put it beyond
    // the screen that restores it.
    const wrapper = await mountAs('auth.user.view')

    expect(wrapper.text()).toContain('wang.li')
    expect(wrapper.text()).toContain('zhao.min')
    expect(wrapper.text()).toContain('已停用')
  })

  it('states the status in words and not only as a colour', async () => {
    const wrapper = await mountAs('auth.user.view')

    // Q32: colour is not the only expression of state.
    expect(wrapper.text()).toContain('在用')
    expect(wrapper.text()).toContain('已停用')
  })

  it('offers no write action to a caller who may only read', async () => {
    const wrapper = await mountAs('auth.user.view')

    const labels = wrapper.findAll('button').map((button) => button.text())
    expect(labels).not.toContain('新建账户')
    expect(labels).not.toContain('停用')
    expect(labels).not.toContain('删除')
  })

  it('offers editing to a caller who holds auth.user.edit', async () => {
    const wrapper = await mountAs('auth.user.view', 'auth.user.edit')

    const labels = wrapper.findAll('button').map((button) => button.text())
    expect(labels).toContain('新建账户')
    expect(labels).toContain('停用')
  })

  it('does not offer role assignment without the role catalogue permission', async () => {
    // USER_EDIT authorizes the API operation, but without ROLE_VIEW the selector would be empty.
    // Hiding only this action keeps the UI from promising an operation it cannot complete.
    const wrapper = await mountAs('auth.user.view', 'auth.user.edit')

    expect(wrapper.findAll('button').map((button) => button.text())).not.toContain('分配角色')
  })

  it('offers role assignment when editing users and viewing roles are both allowed', async () => {
    const wrapper = await mountAs('auth.user.view', 'auth.user.edit', 'auth.role.view')

    expect(wrapper.findAll('button').map((button) => button.text())).toContain('分配角色')
  })

  it('does not offer delete to a caller who may only edit', async () => {
    // §5.15 keeps 删除 behind its own permission precisely so this pair can differ.
    const wrapper = await mountAs('auth.user.view', 'auth.user.edit')

    expect(wrapper.findAll('button').map((button) => button.text())).not.toContain('删除')
  })

  it('offers delete once auth.user.delete is held', async () => {
    const wrapper = await mountAs('auth.user.view', 'auth.user.edit', 'auth.user.delete')

    expect(wrapper.findAll('button').map((button) => button.text())).toContain('删除')
  })

  it('omits the accounts tab entirely from a caller who may not read them', async () => {
    // §5.4: 无权查看的模块不显示该导航项，不显示为"无权限"占位. A disabled tab or an explanation is
    // the thing that section rules out — the caller is shown what they can use, and nothing else.
    const wrapper = await mountAs('auth.role.view')

    expect(wrapper.text()).not.toContain('您没有查看账户的权限')
    expect(wrapper.findAll('.el-tabs__item').map((tab) => tab.text())).toEqual(['角色'])
    expect(api.readUsers).not.toHaveBeenCalled()
  })

  it('omits the roles tab from a caller who may not read roles', async () => {
    const wrapper = await mountAs('auth.user.view')

    expect(wrapper.findAll('.el-tabs__item').map((tab) => tab.text())).toEqual(['账户'])
    expect(api.readRoles).not.toHaveBeenCalled()
  })

  it('shows role names rather than identifiers against an account', async () => {
    const wrapper = await mountAs('auth.user.view', 'auth.role.view')

    expect(wrapper.text()).toContain('只读')
    expect(wrapper.text()).not.toContain('role-1')
  })
})

describe('the role form', () => {
  it('renders its checkboxes from the backend catalogue', async () => {
    // AC2's "不能引入未注册权限", at the screen: the options are what the backend registers, so one
    // it does not register cannot be ticked and one it adds appears with no change here.
    const wrapper = await mountAs('auth.role.view', 'auth.role.edit')

    await wrapper
      .findAll('button')
      .find((button) => button.text() === '新建角色')!
      .trigger('click')
    await flushPromises()

    const options = wrapper.findAll('.el-checkbox').map((box) => box.text())
    expect(options).toEqual(CATALOGUE)
  })

  it('submits the ticked permissions as they were offered', async () => {
    api.createRole.mockResolvedValue({
      id: 'role-2',
      code: 'fresh',
      name: '新角色',
      permissions: [],
    })
    const wrapper = await mountAs('auth.role.view', 'auth.role.edit')
    await wrapper
      .findAll('button')
      .find((button) => button.text() === '新建角色')!
      .trigger('click')
    await flushPromises()

    await wrapper.find('input[name="code"]').setValue('fresh')
    await wrapper.find('input[name="name"]').setValue('新角色')
    // The third option in the catalogue order asserted above: `auth.role.view`.
    const checkbox = wrapper.findAll('.el-checkbox input')[2]
    expect(checkbox).toBeDefined()
    await checkbox!.setValue(true)
    await wrapper
      .findAll('button')
      .find((button) => button.text() === '保存')!
      .trigger('click')
    await flushPromises()

    expect(api.createRole).toHaveBeenCalledWith({
      code: 'fresh',
      name: '新角色',
      permissions: ['auth.role.view'],
    })
  })
})

describe('what it reports back', () => {
  it('says how many sessions a deactivation closed', async () => {
    // The count is the point: an administrator needs to know the operator at a terminal has been
    // signed out, not merely that a field changed.
    api.setUserStatus.mockResolvedValue({
      user: { ...OPERATOR, status: 'deactivated' },
      revoked_sessions: 3,
    })
    const wrapper = await mountAs('auth.user.view', 'auth.user.edit')

    await wrapper
      .findAll('button')
      .find((button) => button.text() === '停用')!
      .trigger('click')
    await flushPromises()

    expect(api.setUserStatus).toHaveBeenCalledWith('user-1', 'deactivated')
    expect(document.body.textContent).toContain('同时下线 3 个会话')
  })

  it('shows a refusal as text rather than swallowing it', async () => {
    // §5.15's fallback: `title` is a displayable sentence even for an `error_code` this application
    // has never seen, and the operator has to see it.
    api.setUserStatus.mockRejectedValue(
      new ControlPlaneError({
        message: '该操作会使系统无人可管理',
        errorCode: 'ADMINISTRATION_WOULD_BE_LOST',
        status: 409,
      }),
    )
    const wrapper = await mountAs('auth.user.view', 'auth.user.edit')

    await wrapper
      .findAll('button')
      .find((button) => button.text() === '停用')!
      .trigger('click')
    await flushPromises()

    const alert = wrapper.find('[role="alert"]')
    expect(alert.exists()).toBe(true)
    expect(alert.text()).toContain('该操作会使系统无人可管理')
  })

  it('refuses a short password before making a request', async () => {
    const wrapper = await mountAs('auth.user.view', 'auth.user.edit')
    await wrapper
      .findAll('button')
      .find((button) => button.text() === '新建账户')!
      .trigger('click')
    await flushPromises()

    await wrapper.find('input[name="login_name"]').setValue('new.person')
    await wrapper.find('input[name="display_name"]').setValue('新人')
    await wrapper.find('input[name="password"]').setValue('short')
    await wrapper
      .findAll('button')
      .find((button) => button.text() === '创建')!
      .trigger('click')
    await flushPromises()

    expect(api.createUser).not.toHaveBeenCalled()
    expect(wrapper.find('[role="alert"]').text()).toContain('密码至少需要 12 个字符')
  })

  it('creates an account and reloads the listing', async () => {
    api.createUser.mockResolvedValue({ ...OPERATOR, id: 'user-3', login_name: 'new.person' })
    const wrapper = await mountAs('auth.user.view', 'auth.user.edit')
    await wrapper
      .findAll('button')
      .find((button) => button.text() === '新建账户')!
      .trigger('click')
    await flushPromises()

    await wrapper.find('input[name="login_name"]').setValue('new.person')
    await wrapper.find('input[name="display_name"]').setValue('新人')
    await wrapper.find('input[name="password"]').setValue('assembly-line-3')
    await wrapper
      .findAll('button')
      .find((button) => button.text() === '创建')!
      .trigger('click')
    await flushPromises()

    expect(api.createUser).toHaveBeenCalledWith({
      login_name: 'new.person',
      display_name: '新人',
      password: 'assembly-line-3', // pragma: allowlist secret
    })
    // Reloaded, so the new row is the backend's record rather than the draft the form held.
    expect(api.readUsers).toHaveBeenCalledTimes(2)
  })

  it('assigns several roles at once', async () => {
    // Q5/Q14: a user may hold more than one role.
    api.setUserRoles.mockResolvedValue({ ...OPERATOR, role_ids: ['role-1', 'role-2'] })
    api.readRoles.mockResolvedValue(
      page([VIEWER_ROLE, { id: 'role-2', code: 'reviewer', name: '复核人员', permissions: [] }]),
    )
    const wrapper = await mountAs('auth.user.view', 'auth.user.edit', 'auth.role.view')

    await wrapper
      .findAll('button')
      .find((button) => button.text() === '分配角色')!
      .trigger('click')
    await flushPromises()
    await wrapper
      .findAll('button')
      .find((button) => button.text() === '保存')!
      .trigger('click')
    await flushPromises()

    expect(api.setUserRoles).toHaveBeenCalledWith('user-1', ['role-1'])
  })
})

describe('what it tells the caller about themselves', () => {
  it('re-reads the caller’s own session after a change that could affect it', async () => {
    // Assigning roles, deactivating, deleting — any of these can change the caller's own
    // permissions (or end their session). Re-reading the identity after a success is what keeps
    // the buttons in step with what the backend will actually accept.
    api.setUserStatus.mockResolvedValue({
      user: { ...OPERATOR, status: 'deactivated' },
      revoked_sessions: 1,
    })
    const wrapper = await mountAs('auth.user.view', 'auth.user.edit')
    expect(api.readSession).toHaveBeenCalledTimes(1)

    await wrapper
      .findAll('button')
      .find((button) => button.text() === '停用')!
      .trigger('click')
    await flushPromises()

    expect(api.readSession).toHaveBeenCalledTimes(2)
  })

  it('shows the backend’s specific reason rather than only its title', async () => {
    // `detail` is the part that says which permission would have lost its last holder; showing
    // only the title leaves the administrator guessing which of their operations is the problem.
    api.setUserStatus.mockRejectedValue(
      new ControlPlaneError({
        message: '该操作会使系统无人可管理',
        errorCode: 'ADMINISTRATION_WOULD_BE_LOST',
        status: 409,
        detail: '该操作会使系统再无任何在用账户持有 auth.user.edit 权限，之后无人能管理用户与角色',
      }),
    )
    const wrapper = await mountAs('auth.user.view', 'auth.user.edit')

    await wrapper
      .findAll('button')
      .find((button) => button.text() === '停用')!
      .trigger('click')
    await flushPromises()

    const alert = wrapper.find('[role="alert"]')
    expect(alert.exists()).toBe(true)
    expect(alert.text()).toContain('auth.user.edit')
  })

  it('repeats a field error beside the field it names', async () => {
    api.createUser.mockRejectedValue(
      new ControlPlaneError({
        message: '提交的内容不合要求',
        errorCode: 'REQUEST_INVALID',
        status: 422,
        fieldErrors: [{ field: 'login_name', message: '该字段是必填的' }],
      }),
    )
    const wrapper = await mountAs('auth.user.view', 'auth.user.edit')
    await wrapper
      .findAll('button')
      .find((button) => button.text() === '新建账户')!
      .trigger('click')
    await flushPromises()
    await wrapper.find('input[name="login_name"]').setValue('wang.li')
    await wrapper.find('input[name="display_name"]').setValue('新人')
    await wrapper.find('input[name="password"]').setValue('assembly-line-3')

    await wrapper
      .findAll('button')
      .find((button) => button.text() === '创建')!
      .trigger('click')
    await flushPromises()

    // The banner carries the title; the form carries the field's own message.
    expect(wrapper.find('[role="alert"]').text()).toContain('提交的内容不合要求')
    const fieldAlerts = wrapper.findAll('.panel__field-error').map((item) => item.text())
    expect(fieldAlerts).toContain('该字段是必填的')
  })
})
