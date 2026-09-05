/**
 * Rendered markup for the two user-visible surfaces this slice adds.
 *
 * Harness §4 requires a user-visible UI change to land with snapshot coverage, and it answers a
 * different question than the assertions in `views.spec.ts`. Those state a rule each — a label is
 * bound, a refusal is announced, an unavailable section says so in words — and pass whatever else
 * the markup does. A snapshot fails on the change nobody thought to write a rule about: an
 * `aria-*` attribute dropped by a refactor, a heading level changed, a control that quietly
 * stopped being a `button`.
 *
 * The fixtures are frozen. A snapshot over live data would churn on every run and be updated
 * without being read, which is the failure mode that makes snapshots worthless.
 */

import ElementPlus from 'element-plus'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import { createRouter, createWebHistory, type Router } from 'vue-router'

import AppShell from '@/shell/AppShell.vue'
import LoginView from '@/session/LoginView.vue'
import { useSessionStore } from '@/session/store'

// Element Plus seeds its id counter randomly per process, so `el-id-4870-0` running this file
// alone is `el-id-6572-0` running the whole suite. Left in, the snapshot would fail on how it was
// invoked. Only the random prefix is dropped: the per-instance suffix stays, so
// `aria-labelledby="el-id-0"` still has to point at `id="el-id-0"` — the label binding is the fact
// worth holding, and it remains checkable.
const GENERATED_ID = /el-id-\d+-(\d+)/g
// The scoped-style hash changes whenever the component's own `<style>` block does. A CSS edit is
// not a markup change, and leaving this in would churn the snapshot on one.
const SCOPE_HASH = / data-v-[0-9a-f]{8}=""/g

function normalize(html: string): string {
  return html.replace(GENERATED_ID, 'el-id-$1').replace(SCOPE_HASH, '')
}

const SESSION = {
  user_id: '018f-1',
  login_name: 'wang.li',
  display_name: '王丽',
  // Fixed, so the rendered 会话到期 is the same string on every run. `Intl` formats it in
  // Asia/Shanghai, which is the conversion the snapshot is there to hold.
  expires_at: '2026-09-07T13:00:00+00:00',
}

function testRouter(): Router {
  const blank = { template: '<div />' }
  return createRouter({
    history: createWebHistory(),
    routes: [
      { path: '/login', name: 'login', component: blank },
      { path: '/', name: 'overview', component: blank },
    ],
  })
}

beforeEach(() => {
  setActivePinia(createPinia())
})

describe('the login page', () => {
  it('renders the form it has been rendering', async () => {
    const router = testRouter()
    await router.push('/login')
    await router.isReady()

    const wrapper = mount(LoginView, { global: { plugins: [router, ElementPlus] } })

    expect(normalize(wrapper.html())).toMatchSnapshot()
  })
})

describe('the protected shell', () => {
  it('renders the navigation and the account strip it has been rendering', async () => {
    const router = testRouter()
    await router.push('/')
    await router.isReady()
    useSessionStore().current = SESSION

    const wrapper = mount(AppShell, { global: { plugins: [router, ElementPlus] } })

    expect(normalize(wrapper.html())).toMatchSnapshot()
  })
})
