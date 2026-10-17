/**
 * 原因码提示由判定核心派生，两个真实消费者共用同一函数。
 *
 * 这里只替换浏览器边界（fetch / EventSource）：视图、控制面适配器与生成常量都是真实代码。
 * 已知码显示核心提示；未知码显示原始码 + 通用排障提示，且不丢该条结论的其余字段。
 */

import { createPinia, setActivePinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises } from '@vue/test-utils'
import { createRouter, createWebHistory, type Router } from 'vue-router'

import StationRuntimePanel from '@/modules/devices/StationRuntimePanel.vue'
import OverviewView from '@/modules/overview/OverviewView.vue'
import { useSessionStore } from '@/session/store'
import { mountWithNaive } from '../support/naive'

const GENERIC_ADVICE = '请查阅版本说明或联系管理员'

function testRouter(): Router {
  const blank = { template: '<div />' }
  return createRouter({
    history: createWebHistory(),
    routes: [
      { path: '/', name: 'overview', component: blank },
      { path: '/devices', name: 'devices', component: blank },
      { path: '/templates', name: 'templates', component: blank },
      { path: '/training-datasets', name: 'datasets', component: blank },
      { path: '/:pathMatch(.*)*', name: 'not-found', component: blank },
    ],
  })
}

function installFetch(routes: Record<string, unknown>): void {
  vi.stubGlobal('fetch', (input: RequestInfo | URL) => {
    const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
    const path = new URL(url, window.location.origin).pathname
    if (!(path in routes)) {
      return Promise.resolve(new Response('', { status: 404 }))
    }
    return Promise.resolve(
      new Response(JSON.stringify(routes[path]), {
        status: 200,
        headers: { 'content-type': 'application/json' },
      }),
    )
  })
}

function installEventSource(frames: { type: string; data: string }[]): (data: unknown) => void {
  let send: (data: unknown) => void = () => undefined
  class FakeEventSource extends EventTarget {
    onopen: ((event: Event) => void) | null = null
    onerror: ((event: Event) => void) | null = null

    constructor(_url: string) {
      super()
      send = (data) =>
        this.dispatchEvent(new MessageEvent('runtime', { data: JSON.stringify(data) }))
      queueMicrotask(() => {
        this.onopen?.(new Event('open'))
        for (const frame of frames) {
          this.dispatchEvent(new MessageEvent(frame.type, { data: frame.data }))
        }
      })
    }

    close(): void {}
  }
  vi.stubGlobal('EventSource', FakeEventSource)
  return (data) => send(data)
}

function signIn(permissions: string[]): void {
  const session = useSessionStore()
  session.current = {
    user_id: 'c16-user',
    login_name: 'c16.viewer',
    display_name: '原因码查看者',
    expires_at: '2026-09-14T09:00:00Z',
    permissions,
  }
}

beforeEach(() => {
  setActivePinia(createPinia())
})

afterEach(() => {
  vi.unstubAllGlobals()
})

const STATION_PROJECTION = {
  station_id: 'station-c16',
  decision: {
    event_id: 'decision-c16',
    verdict: 'indeterminate',
    reason_codes: ['WRONG_STEP', 'FUTURE_REASON', 'toString'],
    instance_id: 16,
    lifecycle: 'open',
    reported_at: '2026-09-14T08:00:02Z',
    template_version_id: 'actual-decision-template',
    template_sha256: 'c'.repeat(64),
    backend_provenance: [{ backend_id: 'backend-c16', model_ids: ['actual-model'] }],
  },
}

describe('工位运行镜像的原因码', () => {
  it('shows measured storage watermarks and a recovery without hiding locked violations', async () => {
    signIn(['monitor.report.view'])
    installFetch({ '/api/v1/monitor/violations': { items: [], page: 1, page_size: 50, total: 0 } })
    const fact = {
      event_id: 'disk-1',
      host_id: 'host-1',
      station_id: 'station-c16',
      stream_id: 'storage:evidence',
      status: 'storage_unavailable',
      reason_code: 'STORAGE_UNAVAILABLE',
      reported_at: '2026-09-14T08:00:02Z',
      occurred_at: '2026-09-14T08:00:02Z',
      source_anchor: null,
      anchor_offset: null,
      detail: JSON.stringify({
        path: '/var/lib/nvsop/evidence',
        used_percent: 91,
        high_watermark_percent: 90,
        recovery_watermark_percent: 80,
      }),
    }
    const snapshot = { ...STATION_PROJECTION, health: [fact] }
    const send = installEventSource([{ type: 'runtime', data: JSON.stringify(snapshot) }])
    const wrapper = mountWithNaive(StationRuntimePanel, {
      props: { stations: [] },
    })
    await flushPromises()
    expect(wrapper.text()).toContain('证据目录')
    expect(wrapper.text()).toContain('91%')
    expect(wrapper.text()).toContain('高水位 ≥ 90%')
    expect(wrapper.text()).toContain('恢复 ≤ 80%')
    expect(wrapper.text()).toContain('/var/lib/nvsop/evidence')
    expect(wrapper.text()).toContain('存储失效')
    send({
      ...snapshot,
      health: [
        {
          ...fact,
          status: 'delivering',
          reason_code: null,
          detail: JSON.stringify({
            path: '/var/lib/nvsop/evidence',
            used_percent: 80,
            high_watermark_percent: 90,
            recovery_watermark_percent: 80,
          }),
        },
      ],
    })
    await flushPromises()
    expect(wrapper.text()).toContain('存储可用')
    // 恢复后的当前健康不能洗白历史判定。
    expect(wrapper.text()).toContain('WRONG_STEP — 出现了错误步骤')
    send({ ...snapshot, health: [{ ...fact, detail: '{invalid' }] })
    await flushPromises()
    expect(wrapper.text()).toContain('暂无容量测量')
    wrapper.unmount()
  })

  it('known codes show the core hint, unknown codes the raw code plus generic advice', async () => {
    signIn(['monitor.report.view'])
    installFetch({
      '/api/v1/monitor/violations': { items: [], page: 1, page_size: 50, total: 0 },
    })
    installEventSource([{ type: 'runtime', data: JSON.stringify(STATION_PROJECTION) }])

    const wrapper = mountWithNaive(StationRuntimePanel, {
      props: { stations: [] },
    })
    await flushPromises()
    await vi.waitFor(() => expect(wrapper.text()).toContain('WRONG_STEP — 出现了错误步骤'))

    const text = wrapper.text()
    expect(text).toContain('未知原因码：FUTURE_REASON')
    expect(text).toContain(GENERIC_ADVICE)
    // `toString` 不是原因码：不能命中对象原型上的成员。
    expect(text).toContain('未知原因码：toString')
    // 未知码不丢该条结论的其余字段。
    expect(text).toContain('actual-decision-template')
    expect(text).toContain('16 / open')
  })
})

const OVERVIEW = {
  device: { status: 'no_data', data: {} },
  template: { status: 'no_data', data: {} },
  dataset: { status: 'no_data', data: {} },
  monitor: { status: 'available', data: {} },
}

const DECISION_EVENT = {
  event_id: 'decision-c16',
  verdict: 'indeterminate',
  reason_codes: ['WRONG_STEP', 'FUTURE_REASON', 'toString'],
  template_version_id: 'actual-decision-template',
  model_ids: ['actual-model'],
}

describe('概览实时上报的原因码', () => {
  it('known codes show the core hint, unknown codes the raw code plus generic advice', async () => {
    signIn(['device.inference_host.view'])
    installFetch({ '/api/v1/overview': OVERVIEW })
    installEventSource([{ type: 'decision', data: JSON.stringify(DECISION_EVENT) }])

    const router = testRouter()
    await router.push('/')
    await router.isReady()
    const wrapper = mountWithNaive(OverviewView, { global: { plugins: [router] } })
    await flushPromises()
    await vi.waitFor(() => expect(wrapper.text()).toContain('WRONG_STEP — 出现了错误步骤'))

    const text = wrapper.text()
    expect(text).toContain('FUTURE_REASON — 未知原因码：FUTURE_REASON')
    expect(text).toContain(GENERIC_ADVICE)
    expect(text).toContain('未知原因码：toString')
    // 未知码不丢该条结论的其余字段。
    expect(text).toContain('模板 actual-decision-template')
    expect(text).toContain('模型 actual-model')
  })
})
