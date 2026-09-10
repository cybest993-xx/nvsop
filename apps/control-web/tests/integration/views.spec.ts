/**
 * The login page and the shell, as an operator meets them.
 *
 * Q32's baseline is what these assert: a label bound to each input, an error that is announced,
 * state that is not carried by colour alone, and a logout that ends the session. Element Plus is
 * really installed, because the labels under test are the ones it renders.
 *
 * This suite realizes acceptance scenario SYS-22-07 (issue #22): §5.15's 中文桌面布局 baseline
 * lives in the browser runtime, so its scenario is carried here rather than in `tests/system/`.
 */

import ElementPlus from 'element-plus'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { createRouter, createWebHistory, type Router } from 'vue-router'

import { ControlPlaneError } from '@/api/controlPlane'
import AppShell from '@/shell/AppShell.vue'
import LoginView from '@/session/LoginView.vue'
import { useSessionStore } from '@/session/store'

const { openSession, endSession } = vi.hoisted(() => ({
  openSession: vi.fn(),
  endSession: vi.fn(),
}))

vi.mock('@/api/controlPlane', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/controlPlane')>()),
  openSession,
  endSession,
}))

const SESSION = {
  user_id: '018f-1',
  login_name: 'wang.li',
  display_name: '王丽',
  expires_at: '2026-09-07T13:00:00+00:00',
  // C2.2 added this. Empty here: these suites are about the login and the shell, and an account
  // holding nothing is the case that proves neither depends on a permission.
  permissions: [] as string[],
}

/** A router with the named routes the views navigate between or link to, and no guard. */
function testRouter(): Router {
  const blank = { template: '<div />' }
  return createRouter({
    history: createWebHistory(),
    routes: [
      { path: '/login', name: 'login', component: blank },
      { path: '/', name: 'overview', component: blank },
      { path: '/devices', name: 'devices', component: blank },
      { path: '/templates', name: 'templates', component: blank },
      { path: '/training-datasets', name: 'datasets', component: blank },
      // The shell links to it when the caller holds an `auth` view permission. Present here without
      // the real guard: what these tests are about is which items the shell renders, and the guard
      // has its own suite.
      { path: '/access', name: 'access', component: blank },
      // Deep-link return is a valid navigation even when this focused test router does not
      // render the destination. Mirror production's catch-all so Vue Router does not warn.
      { path: '/:pathMatch(.*)*', name: 'not-found', component: blank },
    ],
  })
}

async function mountLogin(query: Record<string, string> = {}) {
  const router = testRouter()
  await router.push({ path: '/login', query })
  await router.isReady()
  // Attached to the document, because `document.activeElement` is only meaningful for an
  // element that is actually in it — a detached mount reports `<body>` whatever the component
  // focused.
  const wrapper = mount(LoginView, {
    attachTo: document.body,
    global: { plugins: [router, ElementPlus] },
  })
  return { wrapper, router }
}

beforeEach(() => {
  setActivePinia(createPinia())
  openSession.mockReset()
  endSession.mockReset()
})

describe('the login form', () => {
  it('labels both inputs', async () => {
    // Q32: every form control has a label. A placeholder is not one — it disappears on focus and
    // a screen reader does not announce it as the field's name.
    const { wrapper } = await mountLogin()

    const labels = wrapper.findAll('label').map((label) => ({
      text: label.text(),
      binds: label.attributes('for'),
    }))

    expect(labels.map((label) => label.text)).toEqual(['登录名', '密码'])
    expect(labels.every((label) => label.binds !== undefined)).toBe(true)
  })

  it('starts with the caret in the first field', async () => {
    // Q32 asks for keyboard operation, and the operator's first action on this page is always to
    // type their login name. Taken on mount rather than declared with `autofocus`, which the
    // browser only honours while parsing the document and so would do nothing when this page is
    // reached by a route change from a lapsed session.
    const { wrapper } = await mountLogin()

    expect(document.activeElement).toBe(wrapper.find('input[name="login_name"]').element)
  })

  it('is submitted by the keyboard alone', async () => {
    // A shop-floor operator signs in without reaching for a mouse. `submit` on the form is what
    // makes Enter work, rather than a click handler on the button.
    openSession.mockResolvedValue(SESSION)
    const { wrapper, router } = await mountLogin()
    await wrapper.find('input[name="login_name"]').setValue('wang.li')
    await wrapper.find('input[name="password"]').setValue('assembly-line-3')

    await wrapper.find('form').trigger('submit')
    await router.isReady()

    expect(openSession).toHaveBeenCalledWith({
      login_name: 'wang.li',
      password: 'assembly-line-3', // pragma: allowlist secret
    })
  })

  it('leaves the submit button unavailable until both fields are filled', async () => {
    const { wrapper } = await mountLogin()

    expect(wrapper.find('button[type="submit"]').attributes('disabled')).toBeDefined()

    await wrapper.find('input[name="login_name"]').setValue('wang.li')
    await wrapper.find('input[name="password"]').setValue('assembly-line-3')

    expect(wrapper.find('button[type="submit"]').attributes('disabled')).toBeUndefined()
  })

  it('goes on to the overview once a session is open', async () => {
    openSession.mockResolvedValue(SESSION)
    const { wrapper, router } = await mountLogin()
    await wrapper.find('input[name="login_name"]').setValue('wang.li')
    await wrapper.find('input[name="password"]').setValue('assembly-line-3')

    await wrapper.find('form').trigger('submit')
    await vi.waitFor(() => expect(router.currentRoute.value.name).toBe('overview'))
  })

  it('returns to the page the guard came from', async () => {
    openSession.mockResolvedValue(SESSION)
    const { wrapper, router } = await mountLogin({ next: '/some/deep/link' })
    await wrapper.find('input[name="login_name"]').setValue('wang.li')
    await wrapper.find('input[name="password"]').setValue('assembly-line-3')

    await wrapper.find('form').trigger('submit')
    await vi.waitFor(() => expect(router.currentRoute.value.fullPath).toBe('/some/deep/link'))
  })

  it('ignores an absolute destination', async () => {
    // A `next` pointing at another origin is how a login page becomes an open redirect. Only a
    // path this application itself produced is followed.
    openSession.mockResolvedValue(SESSION)
    const { wrapper, router } = await mountLogin({ next: 'https://elsewhere.example/harvest' })
    await wrapper.find('input[name="login_name"]').setValue('wang.li')
    await wrapper.find('input[name="password"]').setValue('assembly-line-3')

    await wrapper.find('form').trigger('submit')
    await vi.waitFor(() => expect(router.currentRoute.value.name).toBe('overview'))
  })

  it('announces a refusal rather than only colouring it', async () => {
    openSession.mockRejectedValue(
      new ControlPlaneError({
        message: '登录名或密码不正确',
        errorCode: 'CREDENTIALS_REJECTED',
        status: 401,
      }),
    )
    const { wrapper } = await mountLogin()
    await wrapper.find('input[name="login_name"]').setValue('wang.li')
    await wrapper.find('input[name="password"]').setValue('wrong')

    await wrapper.find('form').trigger('submit')
    await vi.waitFor(() => expect(wrapper.find('[role="alert"]').exists()).toBe(true))

    expect(wrapper.find('[role="alert"]').text()).toContain('登录名或密码不正确')
  })

  it('displays a deactivated account as its own message', async () => {
    // Retyping the password will never help — the operator has to ask an administrator, and the
    // page has to say so.
    openSession.mockRejectedValue(
      new ControlPlaneError({
        message: '账户已停用，请联系管理员',
        errorCode: 'ACCOUNT_DEACTIVATED',
        status: 403,
      }),
    )
    const { wrapper } = await mountLogin()
    await wrapper.find('input[name="login_name"]').setValue('wang.li')
    await wrapper.find('input[name="password"]').setValue('assembly-line-3')

    await wrapper.find('form').trigger('submit')
    await vi.waitFor(() => expect(wrapper.find('[role="alert"]').text()).toContain('账户已停用'))
  })

  it('clears the password after a refusal', async () => {
    openSession.mockRejectedValue(
      new ControlPlaneError({
        message: '登录名或密码不正确',
        errorCode: 'CREDENTIALS_REJECTED',
        status: 401,
      }),
    )
    const { wrapper } = await mountLogin()
    await wrapper.find('input[name="login_name"]').setValue('wang.li')
    await wrapper.find('input[name="password"]').setValue('wrong')

    await wrapper.find('form').trigger('submit')
    await vi.waitFor(() =>
      expect((wrapper.find('input[name="password"]').element as HTMLInputElement).value).toBe(''),
    )
    // The login name stays: retyping it is what the operator did not get wrong.
    expect((wrapper.find('input[name="login_name"]').element as HTMLInputElement).value).toBe(
      'wang.li',
    )
  })

  it('displays an error code it has never seen by its message', async () => {
    // §5.15's fallback, at the surface an operator actually sees.
    openSession.mockRejectedValue(
      new ControlPlaneError({
        message: '未来的某种拒绝',
        errorCode: 'SOME_FUTURE_REFUSAL',
        status: 409,
      }),
    )
    const { wrapper } = await mountLogin()
    await wrapper.find('input[name="login_name"]').setValue('wang.li')
    await wrapper.find('input[name="password"]').setValue('assembly-line-3')

    await wrapper.find('form').trigger('submit')
    await vi.waitFor(() =>
      expect(wrapper.find('[role="alert"]').text()).toContain('未来的某种拒绝'),
    )
  })
})

describe('the protected shell', () => {
  async function mountShell(permissions: string[] = []) {
    const router = testRouter()
    await router.push('/')
    await router.isReady()
    const session = useSessionStore()
    session.current = { ...SESSION, permissions }
    session.settled = true
    const wrapper = mount(AppShell, { global: { plugins: [router, ElementPlus] } })
    return { wrapper, router, session }
  }

  it('names the signed-in operator', async () => {
    const { wrapper } = await mountShell()

    expect(wrapper.text()).toContain('王丽')
  })

  it('offers the five navigation sections to a caller who may see all of them', async () => {
    // §5.4 fixes them and their order.
    const { wrapper } = await mountShell([
      'auth.user.view',
      'dataset.dataset.view',
      'device.connector.view',
      'template.draft.view',
    ])

    const labels = wrapper.findAll('nav li').map((item) => item.text())

    expect(labels[0]).toBe('概览')
    expect(labels.map((label) => label.replace('（未上线）', '').trim())).toEqual([
      '概览',
      '工位与设备',
      'SOP 模板',
      '训练数据集',
      '用户与权限',
    ])
  })

  it('omits a section the caller may not see rather than disabling it', async () => {
    // §5.4: 无权查看的模块不显示该导航项，不显示为"无权限"占位. An operator with no `auth` permission
    // does not get a 用户与权限 item at all — not a greyed one, and not one that leads to a refusal.
    const { wrapper } = await mountShell()

    const labels = wrapper.findAll('nav li').map((item) => item.text())

    expect(labels.some((label) => label.includes('用户与权限'))).toBe(false)
    expect(labels).toHaveLength(1)
  })

  it('shows the training-data section only when the caller holds a dataset permission', async () => {
    const { wrapper } = await mountShell(['dataset.dataset.view'])

    const trainingLink = wrapper.findAll('nav a').find((link) => link.text() === '训练数据集')

    expect(trainingLink).toBeDefined()
    expect(wrapper.findAll('nav [aria-disabled="true"]')).toHaveLength(0)
  })

  it('links the sections that exist and the caller may see', async () => {
    const { wrapper } = await mountShell(['auth.role.view'])

    // 概览 and 用户与权限.
    expect(wrapper.findAll('nav a')).toHaveLength(2)
  })

  it('links only 概览 for a caller holding no auth permission', async () => {
    const { wrapper } = await mountShell()

    expect(wrapper.findAll('nav a')).toHaveLength(1)
  })

  it('names the navigation for a screen reader', async () => {
    const { wrapper } = await mountShell()

    expect(wrapper.find('nav').attributes('aria-label')).toBe('主导航')
  })

  it('ends the session and returns to the login page on logout', async () => {
    endSession.mockResolvedValue(undefined)
    const { wrapper, router, session } = await mountShell()

    await wrapper.findAll('button').at(-1)!.trigger('click')
    await vi.waitFor(() => expect(router.currentRoute.value.name).toBe('login'))

    expect(endSession).toHaveBeenCalledOnce()
    expect(session.current).toBeNull()
  })

  it('returns to the login page when the identity is taken away mid-session', async () => {
    // Another administrator can deactivate this account while the shell is open. The store's
    // unauthorized hook clears the identity; the shell is what must stop rendering and send the
    // operator to the login page instead of leaving them among dead buttons.
    endSession.mockResolvedValue(undefined)
    const { router, session } = await mountShell(['auth.user.view'])

    session.current = null

    await vi.waitFor(() => expect(router.currentRoute.value.name).toBe('login'))
  })

  it('keeps the session visible and reports an unknown error when revocation fails', async () => {
    // The HttpOnly cookie and server-side row are still live. Clearing only the Pinia cache
    // would pretend logout succeeded, then a refresh would sign the operator straight back in.
    endSession.mockRejectedValue(
      new ControlPlaneError({ message: '请求未能完成', errorCode: 'UNKNOWN', status: 502 }),
    )
    const { wrapper, router, session } = await mountShell()

    await wrapper.findAll('button').at(-1)!.trigger('click')
    await vi.waitFor(() => expect(wrapper.find('[role="alert"]').exists()).toBe(true))

    expect(wrapper.find('[role="alert"]').text()).toContain('请求未能完成')
    expect(router.currentRoute.value.name).toBe('overview')
    expect(session.current).toEqual(SESSION)
  })
})
