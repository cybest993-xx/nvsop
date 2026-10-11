import { expect, test, type Locator, type Page } from '@playwright/test'

import { cameraMedia } from '../support/cameraMedia'

interface MediaProbeResult {
  currentTime: number
  readyState: number
}

interface SyntheticPeerHandle {
  closed: boolean
  fail: () => void
}

declare global {
  interface Window {
    __syntheticPeers: SyntheticPeerHandle[]
  }
}

test('SYS-34-03/08 — real browser decodes a direct MediaMTX WHEP frame', async ({ page }) => {
  const mediaAddress = process.env.NVSOP_MEDIA_WEBRTC_ADDRESS
  const mediaPath = process.env.NVSOP_MEDIA_EXPECTED_PATH
  if (!mediaAddress || !mediaPath) {
    test.skip(true, '需要显式提供真实 MediaMTX WebRTC 地址和稳定 path')
    return
  }

  const base = new URL(mediaAddress)
  expect(['http:', 'https:']).toContain(base.protocol)
  await page.goto(base.toString())

  const result = await page.evaluate(
    async ({ address, path }): Promise<MediaProbeResult> => {
      const endpoint = new URL(address.endsWith('/') ? address : `${address}/`)
      endpoint.pathname = `${endpoint.pathname.replace(/\/$/, '')}/${encodeURIComponent(path)}/whep`
      const peer = new RTCPeerConnection()
      const video = document.createElement('video')
      video.autoplay = true
      video.muted = true
      video.playsInline = true
      document.body.append(video)
      try {
        peer.addTransceiver('video', { direction: 'recvonly' })
        const firstFrame = new Promise<void>((resolve, reject) => {
          const timer = window.setTimeout(
            () =>
              reject(
                new Error(
                  `首帧等待超时: readyState=${video.readyState}, networkState=${video.networkState}, currentTime=${video.currentTime}`,
                ),
              ),
            15_000,
          )
          const finish = () => {
            window.clearTimeout(timer)
            resolve()
          }
          video.addEventListener('loadeddata', finish, { once: true })
        })
        peer.ontrack = (event) => {
          video.srcObject = event.streams[0] ?? new MediaStream([event.track])
        }
        const offer = await peer.createOffer()
        await peer.setLocalDescription(offer)
        if (peer.iceGatheringState !== 'complete') {
          await new Promise<void>((resolve, reject) => {
            const timer = window.setTimeout(() => reject(new Error('ICE 候选收集超时')), 5_000)
            const onState = () => {
              if (peer.iceGatheringState === 'complete') {
                window.clearTimeout(timer)
                peer.removeEventListener('icegatheringstatechange', onState)
                resolve()
              }
            }
            peer.addEventListener('icegatheringstatechange', onState)
          })
        }
        const response = await fetch(endpoint, {
          method: 'POST',
          headers: { Accept: 'application/sdp', 'Content-Type': 'application/sdp' },
          body: peer.localDescription?.sdp ?? offer.sdp,
          credentials: 'omit',
          redirect: 'error',
        })
        const answer = await response.text()
        if (!response.ok) throw new Error(`WHEP HTTP ${response.status}: ${answer}`)
        await peer.setRemoteDescription({ type: 'answer', sdp: answer })
        await video.play()
        await firstFrame
        return { currentTime: video.currentTime, readyState: video.readyState }
      } finally {
        peer.close()
        video.remove()
      }
    },
    { address: base.toString(), path: mediaPath },
  )

  expect(result.readyState).toBeGreaterThanOrEqual(2)
  expect(result.currentTime).toBeGreaterThanOrEqual(0)
})

// ——— SYS-34-09/15：真实产品 UI 的原生媒体 sink ———

const CAMERA_VIEWER_SESSION = {
  user_id: '018f0000-0000-7000-8000-000000003415',
  login_name: 'camera.viewer',
  display_name: '相机查看者',
  expires_at: '2027-09-14T09:00:00Z',
  permissions: ['device.camera.view'],
}

function pageOf<T>(items: T[]) {
  return { items, page: 1, page_size: 50, total: items.length }
}

/**
 * 在浏览器原生 adapter seam 注入合成视频：
 * - `RTCPeerConnection` 替身在协商后把同一 canvas `captureStream()` 作为 track 交付，并按创建顺序
 *   记录实例，供用例触发原生 `connectionstatechange`；
 * - `HTMLMediaElement.prototype.src` 在回放 `/get` 地址上挂同一 MediaStream 到 `srcObject`。
 *
 * 两者都接到真实 HTMLMediaElement 原生 sink，因此 `loadeddata`/`readyState`/`videoWidth`/`currentTime`
 * 是真实解码结果，不是伪造的 play promise。替身不清理媒体元素：释放由产品代码负责，用例据元素引用核验。
 */
async function installSyntheticMediaSink(page: Page): Promise<void> {
  await page.addInitScript(() => {
    const canvas = document.createElement('canvas')
    canvas.width = 320
    canvas.height = 240
    const context = canvas.getContext('2d')
    let frame = 0
    window.setInterval(() => {
      frame += 1
      if (context) {
        context.fillStyle = frame % 2 === 0 ? '#ff0000' : '#0000ff'
        context.fillRect(0, 0, canvas.width, canvas.height)
      }
    }, 100)
    const stream = canvas.captureStream(10)
    window.__syntheticPeers = []

    class SyntheticPeerConnection extends EventTarget {
      ontrack: ((event: { streams: MediaStream[]; track: MediaStreamTrack }) => void) | null = null
      connectionState = 'new'
      iceGatheringState = 'complete'
      localDescription: { type: string; sdp: string } | null = null
      closed = false
      // 注入脚本会被序列化执行，不能用 `#private` 字段（转译辅助函数不在作用域内）。
      connectionStateHandler: ((event: Event) => void) | null = null

      constructor() {
        super()
        window.__syntheticPeers.push(this)
      }

      get onconnectionstatechange(): ((event: Event) => void) | null {
        return this.connectionStateHandler
      }

      set onconnectionstatechange(handler: ((event: Event) => void) | null) {
        if (this.connectionStateHandler) {
          this.removeEventListener('connectionstatechange', this.connectionStateHandler)
        }
        this.connectionStateHandler = handler
        if (handler) this.addEventListener('connectionstatechange', handler)
      }

      addTransceiver(): void {}
      createOffer(): Promise<{ type: string; sdp: string }> {
        return Promise.resolve({ type: 'offer', sdp: 'offer-sdp' })
      }
      setLocalDescription(description: { sdp?: string }): Promise<void> {
        this.localDescription = { type: 'offer', sdp: description.sdp ?? '' }
        return Promise.resolve()
      }
      setRemoteDescription(): Promise<void> {
        const track = stream.getVideoTracks()[0]
        window.setTimeout(() => {
          if (track) this.ontrack?.({ streams: [stream], track })
        }, 0)
        return Promise.resolve()
      }
      close(): void {
        this.closed = true
      }
      fail(): void {
        this.connectionState = 'failed'
        this.dispatchEvent(new Event('connectionstatechange'))
      }
    }
    ;(window as unknown as { RTCPeerConnection: unknown }).RTCPeerConnection =
      SyntheticPeerConnection

    const descriptor = Object.getOwnPropertyDescriptor(HTMLMediaElement.prototype, 'src')
    Object.defineProperty(HTMLMediaElement.prototype, 'src', {
      configurable: true,
      get(this: HTMLMediaElement) {
        return descriptor?.get?.call(this)
      },
      set(this: HTMLMediaElement, value: string) {
        if (typeof value === 'string' && value.includes('/get')) {
          this.srcObject = stream
        } else {
          descriptor?.set?.call(this, value)
        }
      },
    })
  })
}

async function mockControlPlane(
  page: Page,
  cameras: unknown[],
  whepFailures: number,
): Promise<void> {
  await page.route('**/api/v1/**', async (route) => {
    const path = new URL(route.request().url()).pathname
    if (path === '/api/v1/auth/session') {
      await route.fulfill({ status: 200, json: CAMERA_VIEWER_SESSION })
      return
    }
    if (path === '/api/v1/cameras/media') {
      await route.fulfill({ status: 200, json: pageOf(cameras) })
      return
    }
    await route.fulfill({ status: 200, json: pageOf([]) })
  })

  let whepCalls = 0
  await page.route('**/whep', async (route) => {
    whepCalls += 1
    if (whepCalls <= whepFailures) {
      await route.fulfill({ status: 500, contentType: 'text/plain', body: 'synthetic failure' })
      return
    }
    await route.fulfill({ status: 200, contentType: 'application/sdp', body: 'answer-sdp' })
  })

  // 回放区间由本次查询实际请求的 UTC 起止推导，保证落在有效录像窗口内。
  await page.route('**/list**', async (route) => {
    const url = new URL(route.request().url())
    const start = url.searchParams.get('start')
    const end = url.searchParams.get('end')
    if (!start || !end) {
      await route.fulfill({ status: 400, contentType: 'text/plain', body: 'missing range' })
      return
    }
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify([{ start, duration: (Date.parse(end) - Date.parse(start)) / 1000 }]),
    })
  })
}

function cameraCard(page: Page, name: string): Locator {
  return page
    .locator('article.media-card')
    .filter({ has: page.getByRole('heading', { name, exact: true }) })
}

/** 键盘把焦点移到目标控件、确认 :focus-visible，再用 Enter 激活。 */
async function keyboardFocus(page: Page, locator: Locator): Promise<void> {
  await locator.focus()
  await page.keyboard.press('Tab')
  await page.keyboard.press('Shift+Tab')
  await expect(locator).toBeFocused()
  expect(await locator.evaluate((element) => element.matches(':focus-visible'))).toBe(true)
}

async function keyboardActivate(page: Page, locator: Locator): Promise<void> {
  await keyboardFocus(page, locator)
  await page.keyboard.press('Enter')
}

async function openCameraMediaPage(page: Page): Promise<void> {
  await page.goto('/devices')
  // 首次访问触发 Vite 按需编译，给冷启动留出余量。
  await expect(page.getByRole('heading', { name: '相机预览与录像' })).toBeVisible({
    timeout: 30_000,
  })
}

test('SYS-34-15 — 键盘在目标卡片内完成预览重试/停止/详看/回看并真实出帧', async ({ page }) => {
  test.skip(
    Boolean(process.env.NVSOP_MEDIA_WEBRTC_ADDRESS),
    'WHEP 夹具环境只提供 MediaMTX，产品 UI 由独立命令覆盖',
  )
  await installSyntheticMediaSink(page)
  await mockControlPlane(
    page,
    [cameraMedia('camera-a', 'A相机'), cameraMedia('camera-b', 'B相机')],
    3,
  )
  await openCameraMediaPage(page)

  const cardA = cameraCard(page, 'A相机')
  await expect(cardA).toBeVisible()

  // 1. 预览失败：自动重试耗尽后到达首帧失败终点，再由键盘手动重试同一路
  await keyboardActivate(page, cardA.getByRole('button', { name: '开始预览' }))
  await expect(cardA.getByText('重试中')).toBeVisible()
  await expect(cardA.getByText('首帧失败')).toBeVisible({ timeout: 20_000 })

  await keyboardActivate(page, cardA.getByRole('button', { name: '开始预览' }))
  await expect(cardA.getByText('播放中')).toBeVisible({ timeout: 20_000 })
  const preview = cardA.locator('video[aria-label="A相机实时预览"]')
  await expect
    .poll(() => preview.evaluate((element) => (element as HTMLVideoElement).readyState))
    .toBeGreaterThanOrEqual(2)
  await expect
    .poll(() => preview.evaluate((element) => (element as HTMLVideoElement).videoWidth))
    .toBeGreaterThan(0)
  await expect
    .poll(() => preview.evaluate((element) => (element as HTMLVideoElement).currentTime))
    .toBeGreaterThan(0)

  // 2. 停止预览
  await keyboardActivate(page, cardA.getByRole('button', { name: '停止' }))
  await expect(cardA.getByText('已停止')).toBeVisible()

  // 3. 详看并返回多路
  await keyboardActivate(page, cardA.getByRole('button', { name: '详情' }))
  await expect(page.locator('article.media-card')).toHaveCount(1)
  await keyboardActivate(page, page.getByRole('button', { name: '返回多路' }))
  await expect(page.locator('article.media-card')).toHaveCount(2)

  // 4. 查询有效录像并回看播放/停止
  await keyboardActivate(page, cardA.getByRole('button', { name: '查询片段' }))
  const recording = cardA.locator('video[aria-label="A相机录像回放"]')
  await expect(recording).toBeVisible()
  await keyboardFocus(page, recording)
  await page.keyboard.press('Space')
  await expect
    .poll(() => recording.evaluate((element) => (element as HTMLVideoElement).paused))
    .toBe(false)
  await expect
    .poll(() => recording.evaluate((element) => (element as HTMLVideoElement).readyState))
    .toBeGreaterThanOrEqual(2)
  await expect
    .poll(() => recording.evaluate((element) => (element as HTMLVideoElement).videoWidth))
    .toBeGreaterThan(0)
  await expect
    .poll(() => recording.evaluate((element) => (element as HTMLVideoElement).currentTime))
    .toBeGreaterThan(0)
  const recordingHandle = await recording.elementHandle()
  if (!recordingHandle) throw new Error('回放 video 句柄缺失')

  // 停止回看：产品必须暂停并释放已建立的媒体元素，而不是只从 DOM 移除
  await keyboardActivate(page, cardA.getByRole('button', { name: '停止' }))
  await expect
    .poll(async () => {
      return recordingHandle.evaluate((element) => {
        const video = element as HTMLVideoElement
        return { paused: video.paused, srcObject: video.srcObject !== null }
      })
    })
    .toEqual({ paused: true, srcObject: false })
})

test('SYS-34-09 — 两路真实出帧后一路原生断流恢复，另一路保持且不触发判定或健康事件', async ({
  page,
}) => {
  test.skip(
    Boolean(process.env.NVSOP_MEDIA_WEBRTC_ADDRESS),
    'WHEP 夹具环境只提供 MediaMTX，产品 UI 由独立命令覆盖',
  )
  await installSyntheticMediaSink(page)
  await mockControlPlane(
    page,
    [cameraMedia('camera-a', 'A相机'), cameraMedia('camera-b', 'B相机')],
    0,
  )
  const requests: string[] = []
  page.on('request', (request) => requests.push(request.url()))
  await openCameraMediaPage(page)

  const cardA = cameraCard(page, 'A相机')
  const cardB = cameraCard(page, 'B相机')
  await keyboardActivate(page, cardA.getByRole('button', { name: '开始预览' }))
  await expect(cardA.getByText('播放中')).toBeVisible({ timeout: 20_000 })
  await keyboardActivate(page, cardB.getByRole('button', { name: '开始预览' }))
  await expect(cardB.getByText('播放中')).toBeVisible({ timeout: 20_000 })

  const videoA = cardA.locator('video[aria-label="A相机实时预览"]')
  const videoB = cardB.locator('video[aria-label="B相机实时预览"]')
  await expect
    .poll(() => videoA.evaluate((element) => (element as HTMLVideoElement).currentTime))
    .toBeGreaterThan(0)
  await expect
    .poll(() => videoB.evaluate((element) => (element as HTMLVideoElement).currentTime))
    .toBeGreaterThan(0)
  const timeBefore = await videoB.evaluate((element) => (element as HTMLVideoElement).currentTime)

  // camera-a 原生连接失败：只有这一路关闭并重试
  await page.evaluate(() => window.__syntheticPeers[0]!.fail())
  await expect(cardA.getByText('重试中')).toBeVisible()
  expect(await page.evaluate(() => window.__syntheticPeers[0]!.closed)).toBe(true)
  await expect(cardA.getByText('播放中')).toBeVisible({ timeout: 20_000 })

  // camera-b 保持同一 peer 且继续出帧
  await expect(cardB.getByText('播放中')).toBeVisible()
  expect(await page.evaluate(() => window.__syntheticPeers[1]!.closed)).toBe(false)
  await expect
    .poll(() => videoB.evaluate((element) => (element as HTMLVideoElement).currentTime))
    .toBeGreaterThan(timeBefore)

  // 媒体故障不产生判定/健康请求
  expect(requests.some((url) => /monitor|health/.test(url))).toBe(false)
})

test('SYS-34-10 — 离开页面释放已建立的回放媒体元素', async ({ page }) => {
  test.skip(
    Boolean(process.env.NVSOP_MEDIA_WEBRTC_ADDRESS),
    'WHEP 夹具环境只提供 MediaMTX，产品 UI 由独立命令覆盖',
  )
  await installSyntheticMediaSink(page)
  await mockControlPlane(page, [cameraMedia('camera-a', 'A相机')], 0)
  await openCameraMediaPage(page)

  const cardA = cameraCard(page, 'A相机')
  await keyboardActivate(page, cardA.getByRole('button', { name: '查询片段' }))
  const recording = cardA.locator('video[aria-label="A相机录像回放"]')
  await expect(recording).toBeVisible()
  await recording.focus()
  await page.keyboard.press('Space')
  await expect
    .poll(() => recording.evaluate((element) => (element as HTMLVideoElement).paused))
    .toBe(false)
  await expect
    .poll(() => recording.evaluate((element) => (element as HTMLVideoElement).currentTime))
    .toBeGreaterThan(0)
  const recordingHandle = await recording.elementHandle()
  if (!recordingHandle) throw new Error('回放 video 句柄缺失')

  // SPA 离开设备页：组件卸载必须暂停并释放媒体元素，而不是只从 DOM 移除
  await page.getByRole('link', { name: '概览' }).click()
  await expect(page.getByRole('heading', { name: '概览' })).toBeVisible()
  await expect
    .poll(async () => {
      return recordingHandle.evaluate((element) => {
        const video = element as HTMLVideoElement
        return { paused: video.paused, srcObject: video.srcObject !== null }
      })
    })
    .toEqual({ paused: true, srcObject: false })
})
