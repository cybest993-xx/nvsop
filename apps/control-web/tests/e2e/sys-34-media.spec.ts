import { expect, test } from '@playwright/test'

interface MediaProbeResult {
  currentTime: number
  readyState: number
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
