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
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { createRouter, createWebHistory, type Router } from 'vue-router'

import { flushPromises } from '@vue/test-utils'

import AccessView from '@/modules/access/AccessView.vue'
import DevicesView from '@/modules/devices/DevicesView.vue'
import AppShell from '@/shell/AppShell.vue'
import LoginView from '@/session/LoginView.vue'
import { useSessionStore } from '@/session/store'

// The access page loads its own data on mount. Frozen fixtures, for the same reason the
// fixtures above are frozen: a snapshot over live data churns without being read.
const ACCESS_FIXTURES = vi.hoisted(() => ({
  users: [
    {
      id: '018f-1',
      login_name: 'wang.li',
      display_name: '王丽',
      status: 'active' as const,
      role_ids: ['018f-10'],
    },
    {
      id: '018f-2',
      login_name: 'zhao.min',
      display_name: '赵敏',
      status: 'deactivated' as const,
      role_ids: [],
    },
  ],
  roles: [
    {
      id: '018f-10',
      code: 'system_administrator',
      name: '系统管理员',
      permissions: [
        'auth.role.delete',
        'auth.role.edit',
        'auth.role.view',
        'auth.user.delete',
        'auth.user.edit',
        'auth.user.view',
      ],
    },
  ],
  catalogue: [
    'auth.role.delete',
    'auth.role.edit',
    'auth.role.view',
    'auth.user.delete',
    'auth.user.edit',
    'auth.user.view',
  ],
}))

const DEVICE_FIXTURES = vi.hoisted(() => ({
  connectors: [
    {
      id: 'connector-1',
      station_id: 'station-1',
      host_id: 'host-1',
      name: '装配线输入',
      connector_type: 'hikvision_isapi' as const,
      configuration: { address: '192.168.10.21', port: 80 },
      credentials_configured: true,
      reachability: 'unverified',
      health_detail: null,
      status: 'active' as const,
      revision: 3,
    },
  ],
  hosts: [{ id: 'host-1', name: '推理机 A' }],
  stations: [{ id: 'station-1', code: 'A-01', name: '一号装配工位' }],
  points: [
    {
      id: 'point-1',
      identifier: 'DI-01',
      semantic_label: '工件到位',
      direction: 'input' as const,
      station_id: 'station-1',
      connector_id: 'connector-1',
      status: 'active' as const,
      revision: 4,
      created_at: '2026-09-07T04:00:00Z',
      created_by: 'operator-1',
      updated_at: '2026-09-07T04:00:00Z',
      updated_by: 'operator-1',
    },
  ],
}))

vi.mock('@/api/controlPlane', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/controlPlane')>()),
  readUsers: vi.fn(() =>
    Promise.resolve({ items: ACCESS_FIXTURES.users, page: 1, page_size: 50, total: 2 }),
  ),
  readRoles: vi.fn(() =>
    Promise.resolve({ items: ACCESS_FIXTURES.roles, page: 1, page_size: 50, total: 1 }),
  ),
  readPermissionCatalogue: vi.fn(() =>
    Promise.resolve({ items: ACCESS_FIXTURES.catalogue, page: 1, page_size: 50, total: 6 }),
  ),
  readConnectors: vi.fn(() =>
    Promise.resolve({
      items: DEVICE_FIXTURES.connectors,
      page: 1,
      page_size: 50,
      total: DEVICE_FIXTURES.connectors.length,
    }),
  ),
  readPoints: vi.fn(() =>
    Promise.resolve({ items: DEVICE_FIXTURES.points, page: 1, page_size: 20, total: 1 }),
  ),
  readInferenceHosts: vi.fn(() =>
    Promise.resolve({ items: DEVICE_FIXTURES.hosts, page: 1, page_size: 50, total: 1 }),
  ),
  readStations: vi.fn(() =>
    Promise.resolve({ items: DEVICE_FIXTURES.stations, page: 1, page_size: 50, total: 1 }),
  ),
}))

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

describe('the access page', () => {
  it('renders the accounts and roles an administrator works with', async () => {
    useSessionStore().current = {
      user_id: '018f-0',
      login_name: 'administrator',
      display_name: '系统管理员',
      expires_at: '2026-09-07T13:00:00+00:00',
      permissions: [
        'auth.role.delete',
        'auth.role.edit',
        'auth.role.view',
        'auth.user.delete',
        'auth.user.edit',
        'auth.user.view',
      ],
    }

    const wrapper = mount(AccessView, { global: { plugins: [ElementPlus] } })
    await flushPromises()

    expect(normalize(wrapper.html())).toMatchSnapshot()
  })
})

describe('the device page', () => {
  function mountAsDeviceAdmin() {
    useSessionStore().current = {
      ...SESSION,
      permissions: [
        'device.connector.view',
        'device.connector.edit',
        'device.connector.delete',
        'device.inference_host.view',
        'device.station.view',
      ],
    }
    return mount(DevicesView, { global: { plugins: [ElementPlus] } })
  }

  it('renders connector configuration without a test or secret control', async () => {
    const wrapper = mountAsDeviceAdmin()
    await flushPromises()

    expect(normalize(wrapper.html())).toMatchSnapshot()
  })

  it('renders the connector form with only non-secret fields', async () => {
    const wrapper = mountAsDeviceAdmin()
    await flushPromises()
    await wrapper
      .findAll('button')
      .find((button) => button.text() === '新建连接器')!
      .trigger('click')
    await flushPromises()

    expect(normalize(wrapper.html())).toMatchSnapshot()
  })

  it('renders point management for an authorized device operator', async () => {
    useSessionStore().current = {
      ...SESSION,
      permissions: [
        'device.connector.view',
        'device.connector.edit',
        'device.point.view',
        'device.point.edit',
        'device.point.delete',
        'device.inference_host.view',
        'device.station.view',
      ],
    }
    const wrapper = mount(DevicesView, { global: { plugins: [ElementPlus] } })
    await flushPromises()

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
