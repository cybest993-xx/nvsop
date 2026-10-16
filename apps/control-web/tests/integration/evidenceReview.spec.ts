import { createPinia, setActivePinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'

import { ControlPlaneError, type EvidenceReview, type EvidenceReviewHold } from '@/api/controlPlane'
import EvidencePlayer from '@/modules/devices/EvidencePlayer.vue'
import { useSessionStore } from '@/session/store'

const api = vi.hoisted(() => ({
  readEvidence: vi.fn(),
  createEvidenceAccessContext: vi.fn(),
  readEvidenceReviews: vi.fn(),
  registerEvidenceReview: vi.fn(),
  beginEvidenceReview: vi.fn(),
  cancelEvidenceReview: vi.fn(),
  readEvidenceReclipRequests: vi.fn(),
}))

vi.mock('@/api/controlPlane', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/controlPlane')>()),
  ...api,
}))

const LISTING = {
  evidence_id: 'host-1/first.mp4',
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

const RECORD = {
  review_id: 'review-1',
  evidence_id: 'host-1/first.mp4',
  generation: 'original',
  reviewer_id: 'user-1',
  reviewer_name: '复核员甲',
  reviewed_at: '2026-09-14T01:00:00Z',
  conclusion: 'confirmed' as const,
}

const REVIEW_PERMISSIONS = ['monitor.report.view', 'evidence.evidence.view', 'evidence.review.edit']

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((promiseResolve) => {
    resolve = promiseResolve
  })
  return { promise, resolve }
}

function signIn(permissions: string[]) {
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
  api.readEvidence.mockResolvedValue([LISTING])
  api.readEvidenceReviews.mockResolvedValue([])
  api.readEvidenceReclipRequests.mockResolvedValue([])
  api.beginEvidenceReview.mockResolvedValue({
    review_id: 'hold-1',
    evidence_id: 'host-1/first.mp4',
    generation: 'original',
    reviewer_id: 'viewer-1',
    started_at: '2026-09-14T01:00:00Z',
    ended_at: null,
  })
  api.cancelEvidenceReview.mockResolvedValue(undefined)
  api.createEvidenceAccessContext.mockResolvedValue({
    context: 'signed.ticket',
    origin: 'https://edge.internal:8765',
    expires_at: 1,
    host_id: 'host-1',
  })
  vi.stubGlobal(
    'fetch',
    vi.fn().mockResolvedValue({ ok: true, status: 200, headers: new Headers() }),
  )
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('人工复核结论', () => {
  it('keeps one intent id across a failed retry and never reports saved', async () => {
    signIn(REVIEW_PERMISSIONS)
    api.registerEvidenceReview.mockRejectedValue(
      new ControlPlaneError({ message: '保存被拒绝', errorCode: 'UNKNOWN', status: 0 }),
    )
    const wrapper = mountPlayer()
    await flushPromises()
    await wrapper.find('.evidence__list button').trigger('click')
    await flushPromises()

    await wrapper.find('form.evidence__form').trigger('submit')
    await flushPromises()
    expect(wrapper.find('[role="alert"]').text()).toContain('保存被拒绝')
    expect(wrapper.text()).not.toContain('已保存复核结论')
    const first = api.registerEvidenceReview.mock.calls[0]?.[1].review_id

    // 同一意图重试复用同一幂等键，服务器可据此返回原记录。
    await wrapper.find('form.evidence__form').trigger('submit')
    await flushPromises()
    expect(api.registerEvidenceReview.mock.calls[1]?.[1].review_id).toBe(first)

    // 换结论是新意图：旧幂等键作废，不覆盖已保存历史。
    await wrapper.find('#review-conclusion-rejected').setValue()
    await flushPromises()
    await wrapper.find('form.evidence__form').trigger('submit')
    await flushPromises()
    expect(api.registerEvidenceReview.mock.calls[2]?.[1].review_id).not.toBe(first)
  })

  it('keeps a saved receipt when a late history read resolves, and a conclusion change keeps history', async () => {
    signIn(REVIEW_PERMISSIONS)
    const slowHistory = deferred<EvidenceReview[]>()
    api.readEvidenceReviews.mockReturnValueOnce(slowHistory.promise)
    api.registerEvidenceReview.mockResolvedValue(RECORD)
    const wrapper = mountPlayer()
    await flushPromises()
    await wrapper.find('.evidence__list button').trigger('click')
    await flushPromises()

    // 保存回执先到；随后迟到的历史读取不得覆盖它。
    await wrapper.find('form.evidence__form').trigger('submit')
    await flushPromises()
    expect(wrapper.text()).toContain('已保存复核结论')
    // 换结论作废提交意图，但不丢弃已加载历史。
    await wrapper.find('#review-conclusion-rejected').setValue()
    await flushPromises()
    slowHistory.resolve([])
    await flushPromises()

    expect(wrapper.text()).toContain('复核员甲')
    expect(wrapper.find('.evidence__reviews li').text()).toContain('确认（同意原判定）')
  })

  it('lets a view-only session query history but offers no submit control', async () => {
    signIn(['evidence.evidence.view'])
    api.readEvidenceReviews.mockResolvedValue([RECORD])
    const wrapper = mountPlayer()
    await flushPromises()
    await wrapper.find('.evidence__list button').trigger('click')
    await flushPromises()

    // 只查看权限不自动读取历史，也不提供提交表单。
    expect(api.readEvidenceReviews).not.toHaveBeenCalled()
    expect(wrapper.find('form.evidence__form').exists()).toBe(false)
    await wrapper.find('.evidence__review button').trigger('click')
    await flushPromises()

    expect(api.readEvidenceReviews).toHaveBeenCalledWith('host-1/first.mp4')
    expect(wrapper.text()).toContain('复核员甲')
    expect(wrapper.find('form.evidence__form').exists()).toBe(false)
  })

  it('re-enters a fresh hold after saving so another review stays possible', async () => {
    signIn(REVIEW_PERMISSIONS)
    api.registerEvidenceReview.mockResolvedValue(RECORD)
    const wrapper = mountPlayer()
    await flushPromises()
    await wrapper.find('.evidence__list button').trigger('click')
    await flushPromises()
    await wrapper.find('form.evidence__form').trigger('submit')
    await flushPromises()

    expect(wrapper.text()).toContain('已保存复核结论')
    // 初次进入 + 保存后重新进入 = 两次 begin；表单仍可再次提交独立复核。
    expect(api.beginEvidenceReview).toHaveBeenCalledTimes(2)
    expect(wrapper.find('fieldset').attributes('disabled')).toBeUndefined()
  })

  it('disables submission and explains when the hold cannot be entered', async () => {
    signIn(REVIEW_PERMISSIONS)
    api.beginEvidenceReview.mockRejectedValue(
      new ControlPlaneError({ message: '不可用', errorCode: 'STATE_CONFLICT', status: 410 }),
    )
    const wrapper = mountPlayer()
    await flushPromises()
    await wrapper.find('.evidence__list button').trigger('click')
    await flushPromises()

    expect(wrapper.text()).toContain('删除保护未生效')
    expect(wrapper.find('fieldset').attributes('disabled')).toBeDefined()
  })

  it('releases the current hold when an older cancellation finishes after another selection', async () => {
    signIn(REVIEW_PERMISSIONS)
    const slowCancel = deferred<void>()
    const second = { ...LISTING, evidence_id: 'host-1/second.mp4' }
    const third = { ...LISTING, evidence_id: 'host-1/third.mp4' }
    api.readEvidence.mockResolvedValue([LISTING, second, third])
    const wrapper = mountPlayer()
    await flushPromises()
    const [firstButton, secondButton, thirdButton] = wrapper.findAll('.evidence__list button')
    if (!firstButton || !secondButton || !thirdButton) {
      throw new Error('expected three evidence rows')
    }
    await firstButton.trigger('click')
    await flushPromises()

    api.cancelEvidenceReview.mockReturnValueOnce(slowCancel.promise)
    await secondButton.trigger('click')
    await thirdButton.trigger('click')
    await flushPromises()
    const currentHold = api.beginEvidenceReview.mock.calls.find(
      ([evidenceId]) => evidenceId === third.evidence_id,
    )?.[1].review_id
    expect(currentHold).toEqual(expect.any(String))

    slowCancel.resolve(undefined)
    await flushPromises()
    wrapper.unmount()
    await flushPromises()
    expect(api.cancelEvidenceReview).toHaveBeenCalledWith(third.evidence_id, {
      review_id: currentHold,
    })
  })

  it('cancels a stale begin when another evidence is selected first', async () => {
    signIn(REVIEW_PERMISSIONS)
    const slow = deferred<EvidenceReviewHold>()
    api.beginEvidenceReview.mockReturnValueOnce(slow.promise)
    const second = { ...LISTING, evidence_id: 'host-1/second.mp4' }
    api.readEvidence.mockResolvedValue([LISTING, second])
    const wrapper = mountPlayer()
    await flushPromises()
    const items = wrapper.findAll('.evidence__list button')
    const firstItem = items[0]
    const secondItem = items[1]
    if (firstItem === undefined || secondItem === undefined) {
      throw new Error('expected two evidence rows')
    }
    await firstItem.trigger('click')
    await secondItem.trigger('click')
    await flushPromises()
    slow.resolve({
      review_id: 'stale-hold',
      evidence_id: 'host-1/first.mp4',
      generation: 'original',
      reviewer_id: 'viewer-1',
      started_at: '2026-09-14T01:00:00Z',
      ended_at: null,
    })
    await flushPromises()

    // 过期 begin 成功后必须主动取消，不能留下无人持有的占用。
    expect(api.cancelEvidenceReview).toHaveBeenCalledWith('host-1/first.mp4', {
      review_id: expect.any(String),
    })
  })
})
