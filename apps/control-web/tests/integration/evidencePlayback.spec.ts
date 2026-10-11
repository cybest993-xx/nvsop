import { createPinia, setActivePinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'

import { ControlPlaneError } from '@/api/controlPlane'
import EvidencePlayer from '@/modules/devices/EvidencePlayer.vue'
import { useSessionStore } from '@/session/store'

const api = vi.hoisted(() => ({
  readEvidence: vi.fn(),
  createEvidenceAccessContext: vi.fn(),
}))

vi.mock('@/api/controlPlane', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/controlPlane')>()),
  ...api,
}))

const LISTING = {
  evidence_id: 'host-1/clip.mp4',
  host_id: 'host-1',
  station_id: 'station-1',
  instance_id: 7,
  violation_id: null,
  kind: 'clip' as const,
  origin: 'automatic' as const,
  anchor: 12.5,
  window_start: 7.5,
  window_end: 17.5,
  generation: 'original',
  sha256: 'a'.repeat(64),
  size: 10,
  reference: 'artifact/clip.mp4',
  status: 'available' as const,
}

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((promiseResolve) => {
    resolve = promiseResolve
  })
  return { promise, resolve }
}

function response(ok: boolean, status: number, code?: string) {
  return { ok, status, headers: new Headers(code ? { 'X-Evidence-Error': code } : {}) }
}

function probeOk() {
  return vi.fn().mockResolvedValue(response(true, 200))
}

function signIn() {
  const session = useSessionStore()
  session.current = {
    user_id: 'viewer-1',
    login_name: 'viewer',
    display_name: '证据查看者',
    expires_at: '2026-09-07T13:00:00Z',
    permissions: ['monitor.report.view', 'evidence.evidence.view'],
  }
}

function mountPlayer() {
  return mount(EvidencePlayer, {
    props: { hostId: 'host-1', stationId: 'station-1', instanceId: 7 },
  })
}

beforeEach(() => {
  setActivePinia(createPinia())
  vi.clearAllMocks()
  vi.unstubAllGlobals()
  api.readEvidence.mockResolvedValue([LISTING])
  api.createEvidenceAccessContext.mockResolvedValue({
    context: 'signed.ticket',
    origin: 'https://edge.internal:8765',
    expires_at: 1,
    host_id: 'host-1',
  })
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('证据回看', () => {
  it('lists only the selected host and exposes unusable references', async () => {
    api.readEvidence.mockResolvedValue([
      LISTING,
      { ...LISTING, evidence_id: 'host-2/clip.mp4', host_id: 'host-2' },
      { ...LISTING, evidence_id: 'host-1/upper.mp4', host_id: 'HOST-1' },
      { ...LISTING, evidence_id: 'host-1/pending.mp4', status: 'pending' as const },
    ])
    signIn()
    const wrapper = mountPlayer()
    await flushPromises()

    expect(api.readEvidence).toHaveBeenCalledWith('station-1', 7)
    // UUID 主机按小写规范匹配：大写拼写的同一主机也算本机。
    expect(wrapper.findAll('button')).toHaveLength(2)
    expect(wrapper.text()).toContain('证据片段')
    expect(wrapper.text()).toContain('引用尚未对账，不可回看')
    expect(wrapper.text()).not.toContain('host-2')
  })

  it('probes the source without credentials and reports success only after decode', async () => {
    const fetchMock = probeOk()
    vi.stubGlobal('fetch', fetchMock)
    signIn()
    const wrapper = mountPlayer()
    await flushPromises()
    await wrapper.find('button').trigger('click')
    await flushPromises()

    expect(api.createEvidenceAccessContext).toHaveBeenCalledWith('host-1/clip.mp4')
    const [url, options] = fetchMock.mock.calls[0] ?? []
    expect(String(url)).toBe(
      'https://edge.internal:8765/evidence/host-1%2Fclip.mp4?context=signed.ticket',
    )
    expect(options).toEqual(
      expect.objectContaining({ method: 'HEAD', credentials: 'omit', cache: 'no-store' }),
    )
    const video = wrapper.find('video')
    expect(video.attributes('src')).toContain('edge.internal')
    // 媒体元素本身也必须匿名跨源：HEAD 省略凭据保护不了真正的 src 请求。
    expect(video.attributes('crossorigin')).toBe('anonymous')
    expect(video.attributes('referrerpolicy')).toBe('no-referrer')
    expect(wrapper.text()).not.toContain('证据已开始解码播放')
    await video.trigger('canplay')
    expect(wrapper.text()).toContain('证据已开始解码播放')
  })

  it('renders distinct source failures instead of a generic error', async () => {
    for (const [code, message] of [
      ['context_expired', '访问票据已过期'],
      ['media_missing', '来源推理机上已无该证据文件'],
      ['media_changed', '来源文件大小与登记不一致'],
      ['reference_expired', '证据引用已过期'],
      ['permission_denied', '当前会话无权读取该证据'],
    ] as const) {
      vi.stubGlobal('fetch', vi.fn().mockResolvedValue(response(false, 410, code)))
      signIn()
      const wrapper = mountPlayer()
      await flushPromises()
      await wrapper.find('button').trigger('click')
      await flushPromises()
      expect(wrapper.find('[role="alert"]').text()).toContain(message)
      expect(wrapper.find('video').exists()).toBe(false)
      wrapper.unmount()
    }
  })

  it('stops playback and shows a permission failure when the center refuses a context', async () => {
    vi.stubGlobal('fetch', probeOk())
    api.createEvidenceAccessContext.mockRejectedValue(
      new ControlPlaneError({
        message: '请求被拒绝',
        errorCode: 'PERMISSION_DENIED',
        status: 403,
        detail: '当前会话无权读取该证据。',
      }),
    )
    signIn()
    const wrapper = mountPlayer()
    await flushPromises()
    await wrapper.find('button').trigger('click')
    await flushPromises()

    expect(wrapper.find('[role="alert"]').text()).toContain('当前会话无权读取该证据。')
    expect(wrapper.find('video').exists()).toBe(false)
  })

  it('does not let a late context revive a player after a newer selection', async () => {
    const stale = deferred<{
      context: string
      origin: string
      expires_at: number
      host_id: string
    }>()
    api.readEvidence.mockResolvedValue([
      { ...LISTING, evidence_id: 'host-1/first.mp4' },
      { ...LISTING, evidence_id: 'host-1/second.mp4' },
    ])
    api.createEvidenceAccessContext
      .mockImplementationOnce(() => stale.promise)
      .mockResolvedValueOnce({
        context: 'fresh',
        origin: 'https://edge.internal:8765',
        expires_at: 1,
        host_id: 'host-1',
      })
    const fetchMock = probeOk()
    vi.stubGlobal('fetch', fetchMock)
    signIn()
    const wrapper = mountPlayer()
    await flushPromises()

    const buttons = wrapper.findAll('button')
    await buttons[0]!.trigger('click')
    await buttons[1]!.trigger('click')
    await flushPromises()

    stale.resolve({
      context: 'stale',
      origin: 'https://edge.internal:8765',
      expires_at: 1,
      host_id: 'host-1',
    })
    await flushPromises()

    expect(wrapper.find('video').attributes('src')).toContain('second.mp4')
    expect(wrapper.find('video').attributes('src')).not.toContain('first.mp4')
  })

  it('tears down and blocks a late context after permission is revoked', async () => {
    const stale = deferred<{
      context: string
      origin: string
      expires_at: number
      host_id: string
    }>()
    api.createEvidenceAccessContext.mockImplementation(() => stale.promise)
    const fetchMock = probeOk()
    vi.stubGlobal('fetch', fetchMock)
    signIn()
    const wrapper = mountPlayer()
    await flushPromises()
    await wrapper.find('button').trigger('click')
    await flushPromises()

    const session = useSessionStore()
    session.current = { ...session.current!, permissions: [] }
    await flushPromises()
    stale.resolve({
      context: 'late',
      origin: 'https://edge.internal:8765',
      expires_at: 1,
      host_id: 'host-1',
    })
    await flushPromises()

    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('aborts the in-flight source request on unmount', async () => {
    const fetchMock = probeOk()
    vi.stubGlobal('fetch', fetchMock)
    signIn()
    const wrapper = mountPlayer()
    await flushPromises()
    await wrapper.find('button').trigger('click')
    await flushPromises()

    const options = fetchMock.mock.calls[0]?.[1] as { signal: AbortSignal }
    wrapper.unmount()
    expect(options.signal.aborted).toBe(true)
  })
})
