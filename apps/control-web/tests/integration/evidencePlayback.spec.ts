import { createPinia, setActivePinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'

import { ControlPlaneError } from '@/api/controlPlane'
import EvidencePlayer from '@/modules/devices/EvidencePlayer.vue'
import { useSessionStore } from '@/session/store'

const api = vi.hoisted(() => ({
  readEvidence: vi.fn(),
  createEvidenceAccessContext: vi.fn(),
  readEvidenceReviews: vi.fn(),
  readEvidenceReclipRequests: vi.fn(),
  createEvidenceReclipRequest: vi.fn(),
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
  reclip_deadline: 4_000_000_000,
  reclip_available: true,
}

const RECLIP_RECORD = {
  request_id: '11111111-1111-4111-8111-111111111111',
  evidence_id: 'host-1/clip.mp4',
  generation: 'original',
  host_id: 'host-1',
  anchor: 12.5,
  before_seconds: 2,
  after_seconds: 3,
  window_start: 7.5,
  window_end: 17.5,
  requested_by: 'viewer-1',
  requested_at: '2026-09-14T01:00:00Z',
  command_id: '22222222-2222-4222-8222-222222222222',
  status: 'pending' as const,
  failure_reason: null,
}

const REVIEW_PERMISSIONS = ['monitor.report.view', 'evidence.evidence.view', 'evidence.review.edit']

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

function signIn(permissions = ['monitor.report.view', 'evidence.evidence.view']) {
  const session = useSessionStore()
  session.current = {
    user_id: 'viewer-1',
    login_name: 'viewer',
    display_name: '证据查看者',
    expires_at: '2026-09-07T13:00:00Z',
    permissions,
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
  api.readEvidenceReclipRequests.mockResolvedValue([])
  api.readEvidenceReviews.mockResolvedValue([])
  api.createEvidenceAccessContext.mockResolvedValue({
    context: 'signed.ticket',
    origin: 'https://edge.internal:8765',
    expires_at: 1,
    host_id: 'host-1',
  })
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.useRealTimers()
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

  it('shows each evidence absolute reclip deadline and disables expired or unknown ones', async () => {
    const nowSeconds = Date.now() / 1000
    const future = nowSeconds + 3600
    const past = nowSeconds - 3600
    api.readEvidence.mockResolvedValue([
      {
        ...LISTING,
        evidence_id: 'host-1/future.mp4',
        reclip_deadline: future,
        reclip_available: true,
      },
      {
        ...LISTING,
        evidence_id: 'host-1/expired.mp4',
        reclip_deadline: past,
        reclip_available: false,
      },
      {
        ...LISTING,
        evidence_id: 'host-1/legacy.mp4',
        reclip_deadline: null,
        reclip_available: false,
      },
    ])
    signIn()
    const wrapper = mountPlayer()
    await flushPromises()

    const expected = new Date(future * 1000).toLocaleString('zh-CN', {
      timeZone: 'Asia/Shanghai',
    })
    const text = wrapper.text()
    // 每条证据显示具体绝对截止时刻，按 Asia/Shanghai 展示。
    expect(text).toContain(expected)
    // 过期：明确原因，不再是可提交状态。
    expect(text).toContain('已过期')
    expect(text).toContain('已回收')
    // 缺冻结时间元数据：截止未知，说明原因。
    expect(text).toContain('截止时刻：未知')
    expect(text).toContain('冻结')
  })

  it('expires a still-enabled reclip deadline as time passes and guards the submit instant', async () => {
    vi.useFakeTimers({ toFake: ['Date', 'setInterval', 'clearInterval'] })
    vi.setSystemTime(new Date('2026-09-14T01:00:00Z'))
    vi.stubGlobal('fetch', probeOk())
    signIn(REVIEW_PERMISSIONS)
    const deadline = Math.floor(Date.now() / 1000) + 5
    api.readEvidence.mockResolvedValue([
      { ...LISTING, reclip_deadline: deadline, reclip_available: true },
    ])
    api.readEvidenceReclipRequests.mockResolvedValue([])
    const wrapper = mountPlayer()
    await flushPromises()
    await wrapper.find('.evidence__list button').trigger('click')
    await flushPromises()

    const at = new Date(deadline * 1000).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai' })
    // 拉取时仍在截止前：显示具体日期、可提交、列表项未置灰。
    expect(wrapper.text()).toContain(at)
    expect(wrapper.find('form.evidence__reclip-form').exists()).toBe(true)
    expect(wrapper.find('.evidence__list .evidence__deadline').classes()).not.toContain(
      'evidence__deadline--disabled',
    )

    // 反应式时钟未推进，但系统时间已过截止：提交瞬间的复核必须拦下。
    vi.setSystemTime(new Date((deadline + 2) * 1000))
    await wrapper.find('form.evidence__reclip-form').trigger('submit')
    await flushPromises()
    expect(api.createEvidenceReclipRequest).not.toHaveBeenCalled()
    expect(wrapper.find('.evidence__reclip [role="alert"]').text()).toContain('已回收')

    // 时钟跨过截止：不重新拉取，列表与所选证据都置灰并说明原因，日期仍具体。
    await vi.advanceTimersByTimeAsync(1000)
    await flushPromises()
    expect(api.readEvidence).toHaveBeenCalledTimes(1)
    expect(wrapper.text()).toContain(at)
    expect(wrapper.text()).toContain('已过期')
    expect(wrapper.text()).toContain('已回收')
    expect(wrapper.find('.evidence__list .evidence__deadline').classes()).toContain(
      'evidence__deadline--disabled',
    )
    expect(wrapper.find('form.evidence__reclip-form').exists()).toBe(false)

    wrapper.unmount()
  })

  it('submits a reclip request once per intent and shows the server waiting status', async () => {
    vi.stubGlobal('fetch', probeOk())
    signIn(REVIEW_PERMISSIONS)
    api.readEvidenceReclipRequests.mockResolvedValue([])
    api.createEvidenceReclipRequest.mockResolvedValue(RECLIP_RECORD)
    const wrapper = mountPlayer()
    await flushPromises()
    await wrapper.find('.evidence__list button').trigger('click')
    await flushPromises()

    // 打开即查询真实等待/失败状态，不假装同步切片成功。
    expect(api.readEvidenceReclipRequests).toHaveBeenCalledWith('host-1/clip.mp4')

    await wrapper.find('#reclip-before').setValue('2')
    await wrapper.find('#reclip-after').setValue('3')
    await wrapper.find('form.evidence__reclip-form').trigger('submit')
    await flushPromises()

    const [evidenceId, body] = api.createEvidenceReclipRequest.mock.calls[0] ?? []
    expect(evidenceId).toBe('host-1/clip.mp4')
    expect(body).toMatchObject({ generation: 'original', before_seconds: 2, after_seconds: 3 })
    expect(typeof body.request_id).toBe('string')
    expect(wrapper.text()).toContain('等待来源推理机拉取执行')
    // 服务器回执状态与请求 ID 都来自真实记录。
    expect(wrapper.text()).toContain(RECLIP_RECORD.request_id)
    expect(wrapper.find('.evidence__reclip [role="alert"]').exists()).toBe(false)
  })

  it('reuses the same request id across a failed retry and surfaces a failed request', async () => {
    vi.stubGlobal('fetch', probeOk())
    signIn(REVIEW_PERMISSIONS)
    api.readEvidenceReclipRequests.mockResolvedValue([
      {
        ...RECLIP_RECORD,
        request_id: 'failed-1',
        status: 'failed' as const,
        failure_reason: '素材已过期',
      },
    ])
    api.createEvidenceReclipRequest.mockRejectedValue(
      new ControlPlaneError({ message: '请求被拒绝', errorCode: 'UNKNOWN', status: 0 }),
    )
    const wrapper = mountPlayer()
    await flushPromises()
    await wrapper.find('.evidence__list button').trigger('click')
    await flushPromises()

    // 历史查询的失败原因如实展示。
    expect(wrapper.text()).toContain('素材已过期')

    await wrapper.find('#reclip-before').setValue('2')
    await wrapper.find('#reclip-after').setValue('3')
    await wrapper.find('form.evidence__reclip-form').trigger('submit')
    await flushPromises()
    expect(wrapper.find('.evidence__reclip [role="alert"]').text()).toContain('请求被拒绝')
    const first = api.createEvidenceReclipRequest.mock.calls[0]?.[1].request_id

    // 同一意图重试复用同一幂等键，不做自动重试。
    await wrapper.find('form.evidence__reclip-form').trigger('submit')
    await flushPromises()
    expect(api.createEvidenceReclipRequest.mock.calls[1]?.[1].request_id).toBe(first)

    // 换余量是新意图：旧幂等键作废。
    await wrapper.find('#reclip-after').setValue('9')
    await wrapper.find('form.evidence__reclip-form').trigger('submit')
    await flushPromises()
    expect(api.createEvidenceReclipRequest.mock.calls[2]?.[1].request_id).not.toBe(first)
  })

  it('offers no reclip submit control to a view-only session', async () => {
    vi.stubGlobal('fetch', probeOk())
    signIn(['monitor.report.view', 'evidence.evidence.view'])
    const wrapper = mountPlayer()
    await flushPromises()
    await wrapper.find('.evidence__list button').trigger('click')
    await flushPromises()

    expect(wrapper.find('form.evidence__reclip-form').exists()).toBe(false)
    expect(wrapper.text()).toContain('只有查看权限，不能发起再切片')
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
