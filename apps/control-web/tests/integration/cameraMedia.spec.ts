import ElementPlus from 'element-plus'
import { createPinia, setActivePinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi, type MockInstance } from 'vitest'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'

import DevicesView from '@/modules/devices/DevicesView.vue'
import { useSessionStore } from '@/session/store'

import { cameraMedia } from '../support/cameraMedia'

/**
 * 集成层在浏览器原生 adapter seam（`RTCPeerConnection` / `fetch` / `HTMLMediaElement`）上用确定性
 * 替身验证组件状态机与资源释放。这里的 `loadeddata` 是 jsdom 事件，不构成真实解码帧证据；真实
 * 出帧由 e2e 的原生 HTMLMediaElement sink 证明。
 */
const api = vi.hoisted(() => ({
  readConnectors: vi.fn(),
  readConnector: vi.fn(),
  readCameraMedia: vi.fn(),
  readInferenceHosts: vi.fn(),
  readStations: vi.fn(),
  createConnector: vi.fn(),
  editConnector: vi.fn(),
  setConnectorStatus: vi.fn(),
  deleteConnector: vi.fn(),
  readStationTemplateConfiguration: vi.fn(),
  readTemplateVersions: vi.fn(),
  validateTemplateBinding: vi.fn(),
  bindTemplateVersion: vi.fn(),
  updateStationRuntimeParameters: vi.fn(),
}))

vi.mock('@/api/controlPlane', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/controlPlane')>()),
  ...api,
}))

class FakeTrack {
  readonly kind = 'video'
}

class FakeStream {
  constructor(readonly tracks: FakeTrack[]) {}
}

type TrackHandler = (event: { streams: FakeStream[] }) => void

/** 浏览器原生 `RTCPeerConnection` seam 的最小替身：只暴露组件实际使用的能力。 */
class FakePeerConnection extends EventTarget {
  static instances: FakePeerConnection[] = []

  ontrack: TrackHandler | null = null
  connectionState: RTCPeerConnectionState = 'new'
  iceGatheringState: RTCIceGatheringState = 'complete'
  localDescription: { type: RTCSdpType; sdp: string } | null = null
  closed = false
  transceiverCount = 0
  #connectionStateHandler: ((event: Event) => void) | null = null

  constructor() {
    super()
    FakePeerConnection.instances.push(this)
  }

  get onconnectionstatechange(): ((event: Event) => void) | null {
    return this.#connectionStateHandler
  }

  set onconnectionstatechange(handler: ((event: Event) => void) | null) {
    if (this.#connectionStateHandler) {
      this.removeEventListener('connectionstatechange', this.#connectionStateHandler)
    }
    this.#connectionStateHandler = handler
    if (handler) this.addEventListener('connectionstatechange', handler)
  }

  addTransceiver(): void {
    this.transceiverCount += 1
  }

  createOffer(): Promise<RTCSessionDescriptionInit> {
    return Promise.resolve({ type: 'offer', sdp: 'offer-sdp' })
  }

  setLocalDescription(description: RTCSessionDescriptionInit): Promise<void> {
    this.localDescription = { type: 'offer', sdp: description.sdp ?? '' }
    return Promise.resolve()
  }

  setRemoteDescription(): Promise<void> {
    return Promise.resolve()
  }

  close(): void {
    this.closed = true
  }

  /** 模拟 answer 协商后到达的视频轨。 */
  deliverTrack(): void {
    this.ontrack?.({ streams: [new FakeStream([new FakeTrack()])] })
  }

  /** 模拟原生连接状态变化：通过 EventTarget 派发事件，处理函数收到真实 Event。 */
  changeConnectionState(state: RTCPeerConnectionState): void {
    this.connectionState = state
    this.dispatchEvent(new Event('connectionstatechange'))
  }
}

interface Deferred<T> {
  promise: Promise<T>
  resolve: (value: T) => void
}

function deferred<T>(): Deferred<T> {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((promiseResolve) => {
    resolve = promiseResolve
  })
  return { promise, resolve }
}

interface FetchCall {
  url: string
  method: string
  credentials: RequestCredentials | null
}

interface MediaRequest {
  url: string
  method: string
  signal: AbortSignal | null
  /** 让 fetch 头阶段返回响应；body 仍未到达，由用例决定何时解决。 */
  deliverHeaders: (status?: number) => void
  textBody: Deferred<string>
  jsonBody: Deferred<unknown>
}

/**
 * 浏览器原生 `fetch` seam 替身。
 *
 * fetch 头阶段与 body 阶段分开：`deliverHeaders` 让响应头先到达，`textBody`/`jsonBody` 仍挂起。
 * 这样可以在“响应已到达但 body 未到达”时停止，再让 body 迟到到达，验证代际守卫。abort 只拒绝
 * 头阶段，模拟真实 fetch 在请求头到达前被取消的行为。
 */
function installFetch(): { requests: MediaRequest[]; calls: FetchCall[] } {
  const requests: MediaRequest[] = []
  const calls: FetchCall[] = []
  vi.stubGlobal('fetch', (input: RequestInfo | URL, init?: RequestInit) => {
    const url = input instanceof URL ? input.href : typeof input === 'string' ? input : input.url
    const method = init?.method ?? 'GET'
    calls.push({ url, method, credentials: init?.credentials ?? null })
    const signal = init?.signal ?? null
    const textBody = deferred<string>()
    const jsonBody = deferred<unknown>()
    let deliver!: (status: number) => void
    const headers = new Promise<unknown>((resolve, reject) => {
      deliver = (status: number) => {
        resolve({
          ok: status >= 200 && status < 300,
          status,
          text: () => textBody.promise,
          json: () => jsonBody.promise,
        })
      }
      signal?.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')), {
        once: true,
      })
    })
    requests.push({
      url,
      method,
      signal,
      deliverHeaders: (status = 200) => deliver(status),
      textBody,
      jsonBody,
    })
    return headers
  })
  return { requests, calls }
}

function latest(requests: MediaRequest[], method: string): MediaRequest {
  const request = [...requests].reverse().find((candidate) => candidate.method === method)
  if (!request) throw new Error(`没有 ${method} 请求`)
  return request
}

function pageOf<T>(items: T[]) {
  return { items, page: 1, page_size: 50, total: items.length }
}

function cameraViewerSession(permissions: string[] = ['device.camera.view']): void {
  const session = useSessionStore()
  session.current = {
    user_id: 'camera-viewer',
    login_name: 'viewer',
    display_name: '查看人员',
    expires_at: '2026-09-07T13:00:00Z',
    permissions,
  }
}

function buttonNamed(wrapper: VueWrapper, label: string) {
  const button = wrapper.findAll('button').find((candidate) => candidate.text() === label)
  if (!button) throw new Error(`找不到按钮：${label}`)
  return button
}

async function clickButton(wrapper: VueWrapper, label: string): Promise<void> {
  await buttonNamed(wrapper, label).trigger('click')
}

function cameraCard(wrapper: VueWrapper, cameraName: string) {
  const card = wrapper
    .findAll('article.media-card')
    .find((candidate) => candidate.text().includes(cameraName))
  if (!card) throw new Error(`找不到相机卡片：${cameraName}`)
  return card
}

function cardButton(wrapper: VueWrapper, cameraName: string, label: string) {
  const button = cameraCard(wrapper, cameraName)
    .findAll('button')
    .find((candidate) => candidate.text() === label)
  if (!button) throw new Error(`相机 ${cameraName} 找不到按钮：${label}`)
  return button
}

function previewVideo(wrapper: VueWrapper, cameraName: string): HTMLVideoElement {
  const element = wrapper.find(`video[aria-label="${cameraName}实时预览"]`).element
  if (!(element instanceof HTMLVideoElement)) throw new Error('预览 video 未挂载')
  return element
}

/** 触发组件订阅的 `loadeddata`，代表媒体元素报告已拿到可解码帧。 */
async function emitLoadedData(
  wrapper: VueWrapper,
  peer: FakePeerConnection,
  cameraName: string,
): Promise<void> {
  peer.deliverTrack()
  await flushPromises()
  previewVideo(wrapper, cameraName).onloadeddata?.(new Event('loadeddata'))
  await flushPromises()
}

async function mountDevices(): Promise<VueWrapper> {
  const wrapper = mount(DevicesView, { global: { plugins: [ElementPlus] } })
  await flushPromises()
  return wrapper
}

let pauseSpy: MockInstance<() => void>
let loadSpy: MockInstance<() => void>

beforeEach(() => {
  setActivePinia(createPinia())
  vi.clearAllMocks()
  FakePeerConnection.instances = []
  vi.stubGlobal('RTCPeerConnection', FakePeerConnection)
  vi.spyOn(HTMLMediaElement.prototype, 'play').mockResolvedValue(undefined)
  pauseSpy = vi.spyOn(HTMLMediaElement.prototype, 'pause').mockImplementation(() => {})
  loadSpy = vi.spyOn(HTMLMediaElement.prototype, 'load').mockImplementation(() => {})
  api.readConnectors.mockResolvedValue(pageOf([]))
  api.readCameraMedia.mockResolvedValue(pageOf([]))
  api.readInferenceHosts.mockResolvedValue(pageOf([]))
  api.readStations.mockResolvedValue(pageOf([]))
})

afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('相机媒体：录像查询', () => {
  it('窗口内 list 成功空数组不生成回放地址，只表达无录像', async () => {
    api.readCameraMedia.mockResolvedValue(
      pageOf([cameraMedia('camera-1', '一号相机', { recording_window_seconds: 7200 })]),
    )
    const { requests, calls } = installFetch()
    cameraViewerSession()
    const wrapper = await mountDevices()

    await clickButton(wrapper, '查询片段')
    await flushPromises()
    const list = latest(requests, 'GET')
    list.deliverHeaders()
    list.jsonBody.resolve([])
    await flushPromises()

    expect(calls).toHaveLength(1)
    expect(calls[0]?.credentials).toBe('omit')
    expect(wrapper.find('.media-card__recording').exists()).toBe(false)
    expect(wrapper.find('.media-card__playback .media-card__error').text()).toBe(
      '该时间范围未录像。',
    )
  })

  it('区间存在时间空洞时单独提示空洞', async () => {
    api.readCameraMedia.mockResolvedValue(
      pageOf([cameraMedia('camera-1', '一号相机', { recording_window_seconds: 7200 })]),
    )
    const { requests } = installFetch()
    cameraViewerSession()
    const wrapper = await mountDevices()

    await clickButton(wrapper, '查询片段')
    await flushPromises()
    const list = latest(requests, 'GET')
    const url = new URL(list.url)
    const start = url.searchParams.get('start')!
    const end = url.searchParams.get('end')!
    list.deliverHeaders()
    list.jsonBody.resolve([
      { start, duration: 60 },
      { start: new Date(Date.parse(end) - 60_000).toISOString(), duration: 60 },
    ])
    await flushPromises()

    expect(wrapper.find('.media-panel__warning').text()).toContain('时间空洞')
  })

  it('过期区间表达已过期，而不是未录像或空洞', async () => {
    api.readCameraMedia.mockResolvedValue(
      pageOf([cameraMedia('camera-1', '一号相机', { recording_window_seconds: 3600 })]),
    )
    const { requests } = installFetch()
    cameraViewerSession()
    const wrapper = await mountDevices()

    await wrapper.find('#media-range-start-camera-1').setValue('2020-01-01T00:00')
    await wrapper.find('#media-range-end-camera-1').setValue('2020-01-01T01:00')
    await clickButton(wrapper, '查询片段')
    await flushPromises()
    const list = latest(requests, 'GET')
    list.deliverHeaders()
    list.jsonBody.resolve([])
    await flushPromises()

    expect(wrapper.find('.media-card__playback .media-card__error').text()).toContain('已过期')
    expect(wrapper.find('.media-card__recording').exists()).toBe(false)
  })

  it('只有查看权限的用户看不到编辑媒体策略入口', async () => {
    api.readCameraMedia.mockResolvedValue(pageOf([cameraMedia('camera-1', '一号相机')]))
    cameraViewerSession()
    const wrapper = await mountDevices()

    expect(wrapper.text()).toContain('相机预览与录像')
    expect(wrapper.findAll('button').map((button) => button.text())).not.toContain('编辑媒体策略')
  })
})

describe('相机媒体：首帧失败与手动重试', () => {
  it('信令成功但无解码帧时以首帧失败为终点，loadeddata 到达后才播放', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
    api.readCameraMedia.mockResolvedValue(pageOf([cameraMedia('camera-1', '一号相机')]))
    const { requests } = installFetch()
    cameraViewerSession()
    const wrapper = await mountDevices()

    await clickButton(wrapper, '开始预览')
    await flushPromises()
    expect(FakePeerConnection.instances).toHaveLength(1)
    latest(requests, 'POST').deliverHeaders()
    latest(requests, 'POST').textBody.resolve('answer-sdp')
    await flushPromises()
    expect(wrapper.text()).not.toContain('播放中')

    // 首帧 5s 超时 -> 第一次重试（1s 后）
    await vi.advanceTimersByTimeAsync(5000)
    expect(wrapper.text()).toContain('重试中')
    await vi.advanceTimersByTimeAsync(1000)
    expect(FakePeerConnection.instances).toHaveLength(2)
    latest(requests, 'POST').deliverHeaders()
    latest(requests, 'POST').textBody.resolve('answer-sdp')
    await flushPromises()

    // 第二次重试（2.5s 后）
    await vi.advanceTimersByTimeAsync(5000)
    await vi.advanceTimersByTimeAsync(2500)
    expect(FakePeerConnection.instances).toHaveLength(3)
    latest(requests, 'POST').deliverHeaders()
    latest(requests, 'POST').textBody.resolve('answer-sdp')
    await flushPromises()

    // 第三次尝试仍无帧：到达首帧失败终点，不再无限重试
    await vi.advanceTimersByTimeAsync(5000)
    expect(wrapper.text()).toContain('首帧失败')
    const bounded = FakePeerConnection.instances.length
    await vi.advanceTimersByTimeAsync(60_000)
    expect(FakePeerConnection.instances).toHaveLength(bounded)
    expect(wrapper.text()).not.toContain('播放中')

    // 手动重试且 loadeddata 到达后才显示播放中
    await clickButton(wrapper, '开始预览')
    await flushPromises()
    latest(requests, 'POST').deliverHeaders()
    latest(requests, 'POST').textBody.resolve('answer-sdp')
    await flushPromises()
    await emitLoadedData(wrapper, FakePeerConnection.instances.at(-1)!, '一号相机')
    expect(wrapper.text()).toContain('播放中')
  })
})

describe('相机媒体：多路隔离与断流恢复', () => {
  it('两路已就绪后一路原生连接失败并恢复，另一路保持且不触发判定或健康事件', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
    api.readCameraMedia.mockResolvedValue(
      pageOf([cameraMedia('camera-a', 'A相机'), cameraMedia('camera-b', 'B相机')]),
    )
    const { requests, calls } = installFetch()
    cameraViewerSession()
    const wrapper = await mountDevices()

    await cardButton(wrapper, 'A相机', '开始预览').trigger('click')
    await flushPromises()
    latest(requests, 'POST').deliverHeaders()
    latest(requests, 'POST').textBody.resolve('answer-sdp')
    await flushPromises()
    const peerA = FakePeerConnection.instances.at(-1)!
    await emitLoadedData(wrapper, peerA, 'A相机')

    await cardButton(wrapper, 'B相机', '开始预览').trigger('click')
    await flushPromises()
    latest(requests, 'POST').deliverHeaders()
    latest(requests, 'POST').textBody.resolve('answer-sdp')
    await flushPromises()
    const peerB = FakePeerConnection.instances.at(-1)!
    await emitLoadedData(wrapper, peerB, 'B相机')
    expect(cameraCard(wrapper, 'A相机').text()).toContain('播放中')
    expect(cameraCard(wrapper, 'B相机').text()).toContain('播放中')

    // camera-a 原生连接失败：只有这一路进入重试，camera-b 不受影响
    peerA.changeConnectionState('failed')
    await flushPromises()
    expect(cameraCard(wrapper, 'A相机').text()).toContain('重试中')
    expect(cameraCard(wrapper, 'B相机').text()).toContain('播放中')
    expect(peerA.closed).toBe(true)
    expect(peerB.closed).toBe(false)

    await vi.advanceTimersByTimeAsync(1000)
    latest(requests, 'POST').deliverHeaders()
    latest(requests, 'POST').textBody.resolve('answer-sdp')
    await flushPromises()
    await emitLoadedData(wrapper, FakePeerConnection.instances.at(-1)!, 'A相机')
    expect(cameraCard(wrapper, 'A相机').text()).toContain('播放中')
    expect(cameraCard(wrapper, 'B相机').text()).toContain('播放中')
    expect(peerB.closed).toBe(false)

    // 只发出 WHEP 信令，不调用判定/健康相关控制面接口
    for (const call of calls) {
      expect(call.url).toContain('/whep')
      expect(call.credentials).toBe('omit')
    }
    expect(api.createConnector).not.toHaveBeenCalled()
    expect(api.editConnector).not.toHaveBeenCalled()
  })
})

describe('相机媒体：停止、切换、卸载释放预览', () => {
  it('预览信令头未到达时停止，连接被释放且迟到响应不复活', async () => {
    api.readCameraMedia.mockResolvedValue(pageOf([cameraMedia('camera-1', '一号相机')]))
    const { requests } = installFetch()
    cameraViewerSession()
    const wrapper = await mountDevices()

    await clickButton(wrapper, '开始预览')
    await flushPromises()
    const peer = FakePeerConnection.instances[0]!
    await clickButton(wrapper, '停止')
    await flushPromises()
    expect(peer.closed).toBe(true)

    const whep = latest(requests, 'POST')
    whep.deliverHeaders()
    whep.textBody.resolve('answer-sdp')
    await flushPromises()
    expect(FakePeerConnection.instances).toHaveLength(1)
    expect(wrapper.text()).toContain('已停止')
    expect(wrapper.text()).not.toContain('重试中')
  })

  it('预览信令体迟到到达时已停止，不复活且不重试', async () => {
    api.readCameraMedia.mockResolvedValue(pageOf([cameraMedia('camera-1', '一号相机')]))
    const { requests } = installFetch()
    cameraViewerSession()
    const wrapper = await mountDevices()

    await clickButton(wrapper, '开始预览')
    await flushPromises()
    const peer = FakePeerConnection.instances[0]!
    const whep = latest(requests, 'POST')
    whep.deliverHeaders()
    await flushPromises()

    await clickButton(wrapper, '停止')
    await flushPromises()
    expect(peer.closed).toBe(true)
    expect(whep.signal?.aborted).toBe(true)

    whep.textBody.resolve('answer-sdp')
    await flushPromises()
    expect(FakePeerConnection.instances).toHaveLength(1)
    expect(wrapper.text()).toContain('已停止')
    expect(wrapper.text()).not.toContain('重试中')
  })

  it('预览信令体迟到到达时已切换到详情，不复活已停止的一路', async () => {
    api.readCameraMedia.mockResolvedValue(
      pageOf([cameraMedia('camera-a', 'A相机'), cameraMedia('camera-b', 'B相机')]),
    )
    const { requests } = installFetch()
    cameraViewerSession()
    const wrapper = await mountDevices()

    await cardButton(wrapper, 'A相机', '开始预览').trigger('click')
    await flushPromises()
    const peerA = FakePeerConnection.instances[0]!
    const whep = latest(requests, 'POST')
    whep.deliverHeaders()
    await flushPromises()

    await cardButton(wrapper, 'B相机', '详情').trigger('click')
    await flushPromises()
    expect(peerA.closed).toBe(true)
    expect(whep.signal?.aborted).toBe(true)

    whep.textBody.resolve('answer-sdp')
    await flushPromises()
    expect(FakePeerConnection.instances).toHaveLength(1)
  })

  it('预览信令体迟到到达时已卸载，不复活且不抛错', async () => {
    api.readCameraMedia.mockResolvedValue(pageOf([cameraMedia('camera-1', '一号相机')]))
    const { requests } = installFetch()
    cameraViewerSession()
    const wrapper = await mountDevices()

    await clickButton(wrapper, '开始预览')
    await flushPromises()
    const peer = FakePeerConnection.instances[0]!
    const whep = latest(requests, 'POST')
    whep.deliverHeaders()
    await flushPromises()

    wrapper.unmount()
    expect(peer.closed).toBe(true)
    expect(whep.signal?.aborted).toBe(true)

    whep.textBody.resolve('answer-sdp')
    await flushPromises()
    expect(FakePeerConnection.instances).toHaveLength(1)
  })
})

describe('相机媒体：停止、切换、卸载释放回看', () => {
  it('回看列表请求未完成时按停止，迟到列表响应不生成回放', async () => {
    api.readCameraMedia.mockResolvedValue(pageOf([cameraMedia('camera-1', '一号相机')]))
    const { requests } = installFetch()
    cameraViewerSession()
    const wrapper = await mountDevices()

    await clickButton(wrapper, '查询片段')
    await flushPromises()
    const list = latest(requests, 'GET')
    list.deliverHeaders()
    await flushPromises()

    await clickButton(wrapper, '停止')
    await flushPromises()
    expect(list.signal?.aborted).toBe(true)

    list.jsonBody.resolve([{ start: '2026-09-12T01:00:00Z', duration: 60 }])
    await flushPromises()
    expect(wrapper.find('.media-card__recording').exists()).toBe(false)
    expect(wrapper.findAll('.media-card__intervals li')).toHaveLength(0)
  })

  it('回看列表请求未完成时切换到详情，迟到列表响应不复活', async () => {
    api.readCameraMedia.mockResolvedValue(
      pageOf([cameraMedia('camera-a', 'A相机'), cameraMedia('camera-b', 'B相机')]),
    )
    const { requests } = installFetch()
    cameraViewerSession()
    const wrapper = await mountDevices()

    await cardButton(wrapper, 'A相机', '查询片段').trigger('click')
    await flushPromises()
    const list = latest(requests, 'GET')
    list.deliverHeaders()
    await flushPromises()

    await cardButton(wrapper, 'B相机', '详情').trigger('click')
    await flushPromises()
    expect(list.signal?.aborted).toBe(true)

    list.jsonBody.resolve([{ start: '2026-09-12T01:00:00Z', duration: 60 }])
    await flushPromises()
    await clickButton(wrapper, '返回多路')
    await flushPromises()
    expect(cameraCard(wrapper, 'A相机').find('.media-card__recording').exists()).toBe(false)
    expect(cameraCard(wrapper, 'A相机').findAll('.media-card__intervals li')).toHaveLength(0)
  })

  it('回看列表请求未完成时卸载，迟到列表响应不复活且不抛错', async () => {
    api.readCameraMedia.mockResolvedValue(pageOf([cameraMedia('camera-1', '一号相机')]))
    const { requests } = installFetch()
    cameraViewerSession()
    const wrapper = await mountDevices()

    await clickButton(wrapper, '查询片段')
    await flushPromises()
    const list = latest(requests, 'GET')
    list.deliverHeaders()
    await flushPromises()

    wrapper.unmount()
    expect(list.signal?.aborted).toBe(true)
    list.jsonBody.resolve([{ start: '2026-09-12T01:00:00Z', duration: 60 }])
    await flushPromises()
  })
})

describe('相机媒体：已建立播放后的撤权释放', () => {
  it('已建立预览后 session=null 关闭原生连接', async () => {
    api.readCameraMedia.mockResolvedValue(pageOf([cameraMedia('camera-1', '一号相机')]))
    const { requests } = installFetch()
    cameraViewerSession()
    const wrapper = await mountDevices()

    await clickButton(wrapper, '开始预览')
    await flushPromises()
    latest(requests, 'POST').deliverHeaders()
    latest(requests, 'POST').textBody.resolve('answer-sdp')
    await flushPromises()
    const peer = FakePeerConnection.instances[0]!
    await emitLoadedData(wrapper, peer, '一号相机')
    expect(wrapper.text()).toContain('播放中')

    useSessionStore().current = null
    await flushPromises()
    expect(peer.closed).toBe(true)
    expect(wrapper.find('.media-panel').exists()).toBe(false)
  })

  it('已建立预览后 permissions=[] 关闭原生连接', async () => {
    api.readCameraMedia.mockResolvedValue(pageOf([cameraMedia('camera-1', '一号相机')]))
    const { requests } = installFetch()
    cameraViewerSession()
    const wrapper = await mountDevices()

    await clickButton(wrapper, '开始预览')
    await flushPromises()
    latest(requests, 'POST').deliverHeaders()
    latest(requests, 'POST').textBody.resolve('answer-sdp')
    await flushPromises()
    const peer = FakePeerConnection.instances[0]!
    await emitLoadedData(wrapper, peer, '一号相机')
    expect(wrapper.text()).toContain('播放中')

    cameraViewerSession([])
    await flushPromises()
    expect(peer.closed).toBe(true)
    expect(wrapper.find('.media-panel').exists()).toBe(false)
  })

  it('已建立录像回放后撤权释放原生媒体元素', async () => {
    api.readCameraMedia.mockResolvedValue(pageOf([cameraMedia('camera-1', '一号相机')]))
    const { requests } = installFetch()
    cameraViewerSession()
    const wrapper = await mountDevices()

    await clickButton(wrapper, '查询片段')
    await flushPromises()
    const list = latest(requests, 'GET')
    list.deliverHeaders()
    list.jsonBody.resolve([{ start: '2026-09-12T01:00:00Z', duration: 60 }])
    await flushPromises()
    const recording = wrapper.find('video[aria-label="一号相机录像回放"]')
    expect(recording.exists()).toBe(true)
    const element = recording.element as HTMLVideoElement

    useSessionStore().current = null
    await flushPromises()
    // 从 DOM 移除不足以释放媒体：产品必须暂停、清源并 load()
    expect(element.getAttribute('src')).toBeNull()
    expect(pauseSpy).toHaveBeenCalled()
    expect(loadSpy).toHaveBeenCalled()

    // 恢复权限不复活回放
    cameraViewerSession()
    await flushPromises()
    expect(wrapper.find('.media-panel').exists()).toBe(true)
    expect(wrapper.find('.media-card__recording').exists()).toBe(false)
    expect(wrapper.findAll('.media-card__intervals li')).toHaveLength(0)
  })

  it('已播放卡片上未完成的回看请求在撤权后迟到到达也不复活', async () => {
    api.readCameraMedia.mockResolvedValue(pageOf([cameraMedia('camera-1', '一号相机')]))
    const { requests } = installFetch()
    cameraViewerSession()
    const wrapper = await mountDevices()

    // 先建立预览播放
    await clickButton(wrapper, '开始预览')
    await flushPromises()
    latest(requests, 'POST').deliverHeaders()
    latest(requests, 'POST').textBody.resolve('answer-sdp')
    await flushPromises()
    const peer = FakePeerConnection.instances[0]!
    await emitLoadedData(wrapper, peer, '一号相机')
    expect(wrapper.text()).toContain('播放中')

    // 同一卡片上的回看请求未完成：响应头到达，body 挂起
    await clickButton(wrapper, '查询片段')
    await flushPromises()
    const list = latest(requests, 'GET')
    list.deliverHeaders()
    await flushPromises()

    // 撤权：连接与待决请求一起释放
    useSessionStore().current = null
    await flushPromises()
    expect(peer.closed).toBe(true)
    expect(list.signal?.aborted).toBe(true)

    // 迟到的列表 body 到达，不复活
    list.jsonBody.resolve([{ start: '2026-09-12T01:00:00Z', duration: 60 }])
    await flushPromises()

    // 恢复权限：面板出现但不新建连接、不重试、不复活回放
    cameraViewerSession()
    await flushPromises()
    expect(wrapper.find('.media-panel').exists()).toBe(true)
    expect(wrapper.find('.media-card__recording').exists()).toBe(false)
    expect(wrapper.findAll('.media-card__intervals li')).toHaveLength(0)
    expect(FakePeerConnection.instances).toHaveLength(1)
    expect(wrapper.text()).not.toContain('重试中')
  })

  it('卸载时释放已建立的录像回放媒体元素', async () => {
    api.readCameraMedia.mockResolvedValue(pageOf([cameraMedia('camera-1', '一号相机')]))
    const { requests } = installFetch()
    cameraViewerSession()
    const wrapper = await mountDevices()

    await clickButton(wrapper, '查询片段')
    await flushPromises()
    const list = latest(requests, 'GET')
    list.deliverHeaders()
    list.jsonBody.resolve([{ start: '2026-09-12T01:00:00Z', duration: 60 }])
    await flushPromises()
    const element = wrapper.find('video[aria-label="一号相机录像回放"]').element as HTMLVideoElement

    wrapper.unmount()
    expect(element.getAttribute('src')).toBeNull()
    expect(pauseSpy).toHaveBeenCalled()
    expect(loadSpy).toHaveBeenCalled()
  })
})
