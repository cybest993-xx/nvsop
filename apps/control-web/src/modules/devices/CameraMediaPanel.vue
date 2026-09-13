<script setup lang="ts">
import {
  ElButton,
  ElDialog,
  ElForm,
  ElFormItem,
  ElInput,
  ElMessage,
  ElOption,
  ElSelect,
  ElTag,
} from 'element-plus'
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref } from 'vue'

import {
  ControlPlaneError,
  editCamera,
  editInferenceHost,
  exportInferenceHostMediaConfiguration,
  readCameraMedia,
  type CameraConfiguration,
  type CameraMediaView,
  type HostConfiguration,
  type InferenceHostView,
} from '@/api/controlPlane'
import { useSessionStore } from '@/session/store'

const props = defineProps<{ hosts: InferenceHostView[] }>()
const session = useSessionStore()

const mayViewCameras = computed(() => session.may('device.camera.view'))
const mayEditCameras = computed(() => session.may('device.camera.edit'))
const mayEditHosts = computed(() => session.may('device.inference_host.edit'))
const cameras = ref<CameraMediaView[]>([])
const loading = ref(false)
const failure = ref('')
const selectedCameraId = ref<string | null>(null)
const playerStatus = reactive<Record<string, PlayerStatus>>({})
const playerFailure = reactive<Record<string, string>>({})
const videoElements = new Map<string, HTMLVideoElement>()
const peerConnections = new Map<string, RTCPeerConnection>()
const playerControllers = new Map<string, AbortController>()
const firstFrameTimers = new Map<string, number>()
const retryTimers = new Map<string, ReturnType<typeof setTimeout>>()
const playerGenerations = new Map<string, number>()
const intervals = reactive<Record<string, PlaybackInterval[]>>({})
const playbackUrls = reactive<Record<string, string>>({})
const playbackFailure = reactive<Record<string, string>>({})
const playbackNotice = reactive<Record<string, string>>({})
const playbackControllers = new Map<string, AbortController>()
const playbackGenerations = new Map<string, number>()
const detailPausedCameras = new Set<string>()

const editCameraDialog = ref(false)
const editHostDialog = ref(false)
const editingCamera = ref<CameraMediaView | null>(null)
const editingHost = ref<InferenceHostView | null>(null)
const cameraDraft = reactive<CameraDraft>(newCameraDraft())
const hostDraft = reactive<HostDraft>(newHostDraft())
const rangeStart = ref(shanghaiInput(new Date(Date.now() - 60 * 60 * 1000)))
const rangeEnd = ref(shanghaiInput(new Date()))
const exportLoading = ref<string | null>(null)

const visibleCameras = computed(() => {
  if (selectedCameraId.value === null) {
    return cameras.value
  }
  return cameras.value.filter((camera) => camera.camera_id === selectedCameraId.value)
})

interface PlaybackInterval {
  start: string
  end: string
}

interface CameraDraft {
  name: string
  address: string
  main_stream_path: string
  sub_stream_path: string
  station_id: string
  host_id: string
  backend_id: string
  media_path_mode: 'passthrough' | 'cpu_transcode'
  recording_mode: 'preview_only' | 'continuous'
}

interface HostDraft {
  name: string
  address: string
  mediamtx_address: string
  mediamtx_playback_address: string
  recording_window_seconds: string
  disk_watermark_percent: string
}

type PlayerStatus =
  | 'idle'
  | 'connecting'
  | 'waiting_first_frame'
  | 'playing'
  | 'first_frame_failed'
  | 'disconnected'
  | 'autoplay_blocked'
  | 'retrying'
  | 'error'
  | 'stopped'

function newCameraDraft(camera?: CameraMediaView): CameraDraft {
  return {
    name: camera?.camera_name ?? '',
    address: camera?.camera_address ?? '',
    main_stream_path: camera?.main_stream_path ?? '',
    sub_stream_path: camera?.sub_stream_path ?? '',
    station_id: camera?.station_id ?? '',
    host_id: camera?.host_id ?? '',
    backend_id: camera?.backend_id ?? '',
    media_path_mode: camera?.media_path_mode ?? 'passthrough',
    recording_mode: camera?.recording_mode ?? 'continuous',
  }
}

function newHostDraft(host?: InferenceHostView): HostDraft {
  return {
    name: host?.name ?? '',
    address: host?.address ?? '',
    mediamtx_address: host?.mediamtx_address ?? '',
    mediamtx_playback_address: host?.mediamtx_playback_address ?? '',
    recording_window_seconds: String(host?.recording_window_seconds ?? ''),
    disk_watermark_percent: String(host?.disk_watermark_percent ?? ''),
  }
}

function setVideoRef(cameraId: string, element: unknown): void {
  if (element instanceof HTMLVideoElement) {
    videoElements.set(cameraId, element)
  } else {
    videoElements.delete(cameraId)
  }
}

function statusLabel(status: PlayerStatus | undefined): string {
  switch (status) {
    case 'connecting':
      return '连接中'
    case 'waiting_first_frame':
      return '等待首帧'
    case 'playing':
      return '播放中'
    case 'first_frame_failed':
      return '首帧失败'
    case 'disconnected':
      return '已断流'
    case 'autoplay_blocked':
      return '等待手动播放'
    case 'retrying':
      return '重试中'
    case 'error':
      return '错误'
    case 'stopped':
      return '已停止'
    default:
      return '未连接'
  }
}

function statusType(status: PlayerStatus | undefined): 'success' | 'warning' | 'danger' | 'info' {
  if (status === 'playing') return 'success'
  if (status === 'first_frame_failed' || status === 'disconnected' || status === 'error') {
    return 'danger'
  }
  if (status === 'autoplay_blocked') return 'warning'
  if (status === 'connecting' || status === 'waiting_first_frame' || status === 'retrying') {
    return 'warning'
  }
  return 'info'
}

function setStatus(cameraId: string, status: PlayerStatus, message = ''): void {
  playerStatus[cameraId] = status
  if (message) {
    playerFailure[cameraId] = message
  } else {
    delete playerFailure[cameraId]
  }
}

function nextPlayerGeneration(cameraId: string): number {
  const generation = (playerGenerations.get(cameraId) ?? 0) + 1
  playerGenerations.set(cameraId, generation)
  return generation
}

function isCurrentPlayer(cameraId: string, generation: number): boolean {
  return playerGenerations.get(cameraId) === generation
}

function whepUrl(value: string, mediaPath: string): URL {
  const url = new URL(value.endsWith('/') ? value : `${value}/`)
  url.pathname = `${url.pathname.replace(/\/$/, '')}/${encodeURIComponent(mediaPath)}/whep`
  return url
}

function waitForIceGathering(peer: RTCPeerConnection, signal: AbortSignal): Promise<void> {
  if (peer.iceGatheringState === 'complete') return Promise.resolve()
  return new Promise((resolve, reject) => {
    const timer = window.setTimeout(() => finish(new Error('ICE 候选收集超时')), 5000)
    const finish = (error?: Error) => {
      window.clearTimeout(timer)
      peer.removeEventListener('icegatheringstatechange', onState)
      signal.removeEventListener('abort', onAbort)
      if (error) reject(error)
      else resolve()
    }
    const onState = () => {
      if (peer.iceGatheringState === 'complete') finish()
    }
    const onAbort = () => finish(new Error('预览已停止'))
    peer.addEventListener('icegatheringstatechange', onState)
    signal.addEventListener('abort', onAbort, { once: true })
  })
}

function playVideo(cameraId: string): void {
  const video = videoElements.get(cameraId)
  const generation = playerGenerations.get(cameraId) ?? 0
  if (!video) return
  void video
    .play()
    .then(() => {
      if (isCurrentPlayer(cameraId, generation)) setStatus(cameraId, 'playing')
    })
    .catch(() => {
      if (isCurrentPlayer(cameraId, generation)) {
        setStatus(cameraId, 'autoplay_blocked', '浏览器阻止自动播放，请点击“播放”')
      }
    })
}

function schedulePreviewRetry(
  camera: CameraMediaView,
  attempt: number,
  message: string,
  generation: number,
  failureStatus: 'first_frame_failed' | 'disconnected' = 'first_frame_failed',
): void {
  if (!isCurrentPlayer(camera.camera_id, generation)) return
  if (retryTimers.has(camera.camera_id)) return
  const retryGeneration = nextPlayerGeneration(camera.camera_id)
  closePreview(camera.camera_id)
  if (attempt >= 2) {
    setStatus(camera.camera_id, failureStatus, message)
    return
  }
  setStatus(camera.camera_id, 'retrying', message)
  const timer = setTimeout(
    () => {
      retryTimers.delete(camera.camera_id)
      if (isCurrentPlayer(camera.camera_id, retryGeneration)) {
        void startPreview(camera, attempt + 1)
      }
    },
    attempt === 0 ? 1000 : 2500,
  )
  retryTimers.set(camera.camera_id, timer)
}

async function startPreview(camera: CameraMediaView, attempt = 0): Promise<void> {
  stopPreview(camera.camera_id)
  const generation = playerGenerations.get(camera.camera_id) ?? 0
  if (
    camera.camera_status !== 'active' ||
    camera.host_status !== 'active' ||
    camera.station_status !== 'active'
  ) {
    setStatus(camera.camera_id, 'error', '相机、工位或推理机已停用')
    return
  }
  if (!camera.mediamtx_address) {
    setStatus(camera.camera_id, 'error', '未配置 WebRTC 直连地址')
    return
  }
  if (typeof RTCPeerConnection === 'undefined') {
    setStatus(camera.camera_id, 'first_frame_failed', '当前浏览器不支持 WebRTC')
    return
  }

  const video = videoElements.get(camera.camera_id)
  if (!video) {
    setStatus(camera.camera_id, 'error', '预览播放器尚未挂载')
    return
  }
  const controller = new AbortController()
  playerControllers.set(camera.camera_id, controller)
  setStatus(camera.camera_id, attempt === 0 ? 'connecting' : 'retrying')
  let peer: RTCPeerConnection
  try {
    peer = new RTCPeerConnection()
    peerConnections.set(camera.camera_id, peer)
    peer.addTransceiver('video', { direction: 'recvonly' })
  } catch (error) {
    schedulePreviewRetry(
      camera,
      attempt,
      error instanceof Error ? error.message : 'WebRTC 连接创建失败',
      generation,
    )
    return
  }

  const firstFrame = new Promise<void>((resolve, reject) => {
    let frameReady = false
    const timer = window.setTimeout(() => finish(new Error('首帧等待超时')), 5000)
    firstFrameTimers.set(camera.camera_id, timer)
    const finish = (error?: Error) => {
      window.clearTimeout(timer)
      firstFrameTimers.delete(camera.camera_id)
      controller.signal.removeEventListener('abort', onAbort)
      video.onloadeddata = null
      video.onerror = null
      peer.ontrack = null
      if (error) reject(error)
      else resolve()
    }
    const onAbort = () => finish(new Error('预览已停止'))
    video.onerror = () => finish(new Error('视频解码失败'))
    controller.signal.addEventListener('abort', onAbort, { once: true })
    peer.ontrack = (event) => {
      if (!isCurrentPlayer(camera.camera_id, generation)) return
      video.srcObject = event.streams[0] ?? new MediaStream([event.track])
      setStatus(camera.camera_id, 'waiting_first_frame')
      video.onloadeddata = () => {
        if (!isCurrentPlayer(camera.camera_id, generation)) return
        frameReady = true
        finish()
        video.onerror = () => {
          if (isCurrentPlayer(camera.camera_id, generation)) {
            schedulePreviewRetry(camera, attempt, '视频解码失败', generation, 'disconnected')
          }
        }
        playVideo(camera.camera_id)
      }
    }
    peer.onconnectionstatechange = () => {
      if (!isCurrentPlayer(camera.camera_id, generation)) return
      if (
        peer.connectionState === 'failed' ||
        peer.connectionState === 'disconnected' ||
        peer.connectionState === 'closed'
      ) {
        if (frameReady) {
          schedulePreviewRetry(camera, attempt, '媒体连接已断开', generation, 'disconnected')
        } else {
          finish(new Error('媒体连接已断开'))
        }
      }
    }
  })

  try {
    const offer = await peer.createOffer()
    await peer.setLocalDescription(offer)
    await waitForIceGathering(peer, controller.signal)
    if (!isCurrentPlayer(camera.camera_id, generation)) return
    const endpoint = whepUrl(camera.mediamtx_address, camera.media_path)
    const response = await fetch(endpoint, {
      method: 'POST',
      headers: { 'Content-Type': 'application/sdp', Accept: 'application/sdp' },
      body: peer.localDescription?.sdp ?? offer.sdp,
      credentials: 'omit',
      redirect: 'error',
      signal: controller.signal,
    })
    if (!response.ok) {
      throw new Error(`MediaMTX 返回 HTTP ${response.status}`)
    }
    await peer.setRemoteDescription({ type: 'answer', sdp: await response.text() })
    await firstFrame
  } catch (error) {
    if (controller.signal.aborted || !isCurrentPlayer(camera.camera_id, generation)) return
    schedulePreviewRetry(
      camera,
      attempt,
      error instanceof Error ? error.message : 'WebRTC 预览失败',
      generation,
    )
  }
}

function closePreview(cameraId: string): void {
  const timer = firstFrameTimers.get(cameraId)
  if (timer) window.clearTimeout(timer)
  firstFrameTimers.delete(cameraId)
  const peer = peerConnections.get(cameraId)
  if (peer) {
    peer.ontrack = null
    peer.onconnectionstatechange = null
    peer.close()
  }
  peerConnections.delete(cameraId)
  playerControllers.get(cameraId)?.abort()
  playerControllers.delete(cameraId)
  const video = videoElements.get(cameraId)
  if (video) {
    video.onloadeddata = null
    video.onerror = null
    video.srcObject = null
  }
}

function stopPreview(cameraId: string): void {
  nextPlayerGeneration(cameraId)
  const timer = retryTimers.get(cameraId)
  if (timer) clearTimeout(timer)
  retryTimers.delete(cameraId)
  closePreview(cameraId)
  setStatus(cameraId, 'stopped')
}

function selectCamera(cameraId: string): void {
  detailPausedCameras.clear()
  for (const camera of cameras.value) {
    if (camera.camera_id !== cameraId) {
      const status = playerStatus[camera.camera_id]
      if (
        status === 'connecting' ||
        status === 'waiting_first_frame' ||
        status === 'playing' ||
        status === 'autoplay_blocked' ||
        status === 'retrying'
      ) {
        detailPausedCameras.add(camera.camera_id)
      }
      stopPreview(camera.camera_id)
      stopPlayback(camera.camera_id)
    }
  }
  selectedCameraId.value = cameraId
}

function showAllCameras(): void {
  selectedCameraId.value = null
  void nextTick().then(() => {
    for (const camera of cameras.value) {
      if (detailPausedCameras.has(camera.camera_id)) void startPreview(camera)
    }
    detailPausedCameras.clear()
  })
}

function formatShanghai(value: string): string {
  return new Intl.DateTimeFormat('zh-CN', {
    timeZone: 'Asia/Shanghai',
    dateStyle: 'short',
    timeStyle: 'medium',
  }).format(new Date(value))
}

function shanghaiInput(value: Date): string {
  const parts = new Intl.DateTimeFormat('sv-SE', {
    timeZone: 'Asia/Shanghai',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  }).formatToParts(value)
  const values = Object.fromEntries(parts.map((part) => [part.type, part.value]))
  return `${values.year}-${values.month}-${values.day}T${values.hour}:${values.minute}`
}

function shanghaiIso(value: string): string {
  const parsed = new Date(`${value}:00+08:00`)
  if (!Number.isFinite(parsed.getTime())) throw new Error('回放时间格式无效')
  return parsed.toISOString()
}

function playbackRange(): { start: string; end: string } {
  const start = shanghaiIso(rangeStart.value)
  const end = shanghaiIso(rangeEnd.value)
  if (Date.parse(start) >= Date.parse(end)) throw new Error('回放开始时间必须早于结束时间')
  return { start, end }
}

function playbackEmptyMessage(camera: CameraMediaView, start: string, end: string): string {
  const now = Date.now()
  const cutoff = now - camera.recording_window_seconds * 1000
  if (Date.parse(end) <= cutoff) return '素材已超过推理机录像窗口，已过期。'
  if (Date.parse(start) < cutoff) return '请求跨越录像窗口，较早部分已过期。'
  return '该时间范围未录像，或录像区间存在时间空洞。'
}

function playbackCoverageNotice(
  camera: CameraMediaView,
  range: { start: string; end: string },
  intervals: PlaybackInterval[],
): string {
  const requestedStart = Date.parse(range.start)
  const requestedEnd = Date.parse(range.end)
  const cutoff = Date.now() - camera.recording_window_seconds * 1000
  let cursor = requestedStart
  let hasGap = false
  const orderedIntervals = [...intervals].sort(
    (left, right) => Date.parse(left.start) - Date.parse(right.start),
  )
  for (const interval of orderedIntervals) {
    const start = Date.parse(interval.start)
    const end = Date.parse(interval.end)
    if (start > cursor) hasGap = true
    cursor = Math.max(cursor, end)
  }
  if (cursor < requestedEnd) hasGap = true
  const crossesExpiry = requestedStart < cutoff
  if (crossesExpiry && hasGap) return '请求跨越录像窗口且区间存在时间空洞，以下仅显示实际可用片段。'
  if (crossesExpiry) return '请求跨越录像窗口，以下仅显示仍在保留期内的实际片段。'
  if (hasGap) return '录像区间存在时间空洞，以下仅显示实际可用片段。'
  return ''
}

function playbackBase(value: string): URL {
  const url = new URL(value.endsWith('/') ? value : `${value}/`)
  url.pathname = `${url.pathname.replace(/\/$/, '')}/list`
  return url
}

function playbackGetUrl(
  camera: CameraMediaView,
  interval: PlaybackInterval | undefined = undefined,
): string | null {
  if (!camera.mediamtx_playback_address) return null
  const url = new URL(playbackBase(camera.mediamtx_playback_address))
  url.pathname = url.pathname.replace(/\/list$/, '/get')
  const start = interval?.start ?? shanghaiIso(rangeStart.value)
  const end = interval?.end ?? shanghaiIso(rangeEnd.value)
  if (Date.parse(start) >= Date.parse(end)) return null
  url.searchParams.set('path', camera.media_path)
  url.searchParams.set('start', start)
  url.searchParams.set('duration', String((Date.parse(end) - Date.parse(start)) / 1000))
  return url.toString()
}

function handlePlaybackError(cameraId: string): void {
  playbackFailure[cameraId] = '录像播放失败，请重试或选择其他片段。'
}

function stopPlayback(cameraId: string): void {
  playbackControllers.get(cameraId)?.abort()
  nextPlaybackGeneration(cameraId)
  playbackControllers.delete(cameraId)
  delete playbackUrls[cameraId]
  delete intervals[cameraId]
  delete playbackNotice[cameraId]
}

function setPlaybackInterval(camera: CameraMediaView, interval: PlaybackInterval): void {
  const url = playbackGetUrl(camera, interval)
  if (url) playbackUrls[camera.camera_id] = url
}

function nextPlaybackGeneration(cameraId: string): number {
  const generation = (playbackGenerations.get(cameraId) ?? 0) + 1
  playbackGenerations.set(cameraId, generation)
  return generation
}

async function queryPlayback(camera: CameraMediaView): Promise<void> {
  const cameraId = camera.camera_id
  playbackControllers.get(cameraId)?.abort()
  playbackControllers.delete(cameraId)
  const generation = nextPlaybackGeneration(cameraId)
  intervals[cameraId] = []
  delete playbackUrls[cameraId]
  delete playbackFailure[cameraId]
  delete playbackNotice[cameraId]
  let range: { start: string; end: string }
  try {
    range = playbackRange()
  } catch (error) {
    playbackFailure[cameraId] = error instanceof Error ? error.message : '回放时间无效'
    return
  }
  if (!camera.mediamtx_playback_address) {
    playbackFailure[cameraId] = '未配置原生回放地址'
    return
  }
  const controller = new AbortController()
  playbackControllers.set(cameraId, controller)
  let url: URL
  try {
    url = playbackBase(camera.mediamtx_playback_address)
    url.searchParams.set('path', camera.media_path)
    url.searchParams.set('start', range.start)
    url.searchParams.set('end', range.end)
    const response = await fetch(url, {
      credentials: 'omit',
      cache: 'no-store',
      redirect: 'error',
      signal: controller.signal,
    })
    if (response.status === 404) {
      if (playbackGenerations.get(cameraId) === generation) {
        playbackFailure[cameraId] = playbackEmptyMessage(camera, range.start, range.end)
      }
      return
    }
    if (!response.ok) throw new Error(`MediaMTX 返回 HTTP ${response.status}`)
    const raw: unknown = await response.json()
    if (!Array.isArray(raw)) throw new Error('MediaMTX 回放区间响应格式无效')
    const parsed = raw.map((item): PlaybackInterval => {
      if (!item || typeof item !== 'object') throw new Error('MediaMTX 回放区间响应格式无效')
      const start = 'start' in item && typeof item.start === 'string' ? item.start : null
      const duration =
        'duration' in item && typeof item.duration === 'number' ? item.duration : null
      if (!start || duration === null || !Number.isFinite(duration) || duration <= 0) {
        throw new Error('MediaMTX 回放区间响应格式无效')
      }
      const startMilliseconds = Date.parse(start)
      if (!Number.isFinite(startMilliseconds)) throw new Error('MediaMTX 回放区间响应格式无效')
      const end = new Date(startMilliseconds + duration * 1000)
      if (!Number.isFinite(end.getTime()) || startMilliseconds >= end.getTime()) {
        throw new Error('MediaMTX 回放区间响应格式无效')
      }
      const endValue = end.toISOString()
      return { start, end: endValue }
    })
    if (playbackGenerations.get(cameraId) !== generation) return
    intervals[cameraId] = parsed
    const notice = parsed.length ? playbackCoverageNotice(camera, range, parsed) : ''
    if (notice) playbackNotice[cameraId] = notice
    const playbackUrl = playbackGetUrl(camera, parsed[0])
    if (playbackUrl) playbackUrls[cameraId] = playbackUrl
    if (!parsed.length) {
      playbackFailure[cameraId] = playbackEmptyMessage(camera, range.start, range.end)
    }
  } catch (error) {
    if (controller.signal.aborted || playbackGenerations.get(cameraId) !== generation) return
    playbackFailure[cameraId] = error instanceof Error ? error.message : '回放查询失败'
  } finally {
    if (playbackControllers.get(cameraId) === controller) playbackControllers.delete(cameraId)
  }
}

function openCameraEdit(camera: CameraMediaView): void {
  editingCamera.value = camera
  Object.assign(cameraDraft, newCameraDraft(camera))
  editCameraDialog.value = true
}

async function submitCameraEdit(): Promise<void> {
  const camera = editingCamera.value
  if (!camera) return
  const submitted: CameraConfiguration = {
    ...cameraDraft,
    station_id: cameraDraft.station_id,
    host_id: cameraDraft.host_id,
    backend_id: cameraDraft.backend_id,
  }
  try {
    await editCamera(camera.camera_id, submitted, camera.camera_revision)
    editCameraDialog.value = false
    ElMessage.success('相机媒体配置已保存')
    await load()
  } catch (error) {
    recordFailure(error)
  }
}

function openHostEdit(host: InferenceHostView): void {
  editingHost.value = host
  Object.assign(hostDraft, newHostDraft(host))
  editHostDialog.value = true
}

async function submitHostEdit(): Promise<void> {
  const host = editingHost.value
  if (!host) return
  const recordingWindow = Number(hostDraft.recording_window_seconds)
  const watermark = Number(hostDraft.disk_watermark_percent)
  if (!Number.isInteger(recordingWindow) || recordingWindow <= 0 || !Number.isInteger(watermark)) {
    failure.value = '录像窗口和磁盘水位必须是有效整数'
    return
  }
  const submitted: HostConfiguration = {
    name: hostDraft.name.trim(),
    address: hostDraft.address.trim(),
    mediamtx_address: hostDraft.mediamtx_address.trim() || null,
    mediamtx_playback_address: hostDraft.mediamtx_playback_address.trim() || null,
    recording_window_seconds: recordingWindow,
    disk_watermark_percent: watermark,
  }
  try {
    await editInferenceHost(host.id, submitted, host.revision)
    editHostDialog.value = false
    ElMessage.success('推理机媒体配置已保存')
    await load()
  } catch (error) {
    recordFailure(error)
  }
}

async function exportHost(host: InferenceHostView): Promise<void> {
  exportLoading.value = host.id
  try {
    const configuration = await exportInferenceHostMediaConfiguration(host.id)
    const blob = new Blob([JSON.stringify(configuration, null, 2)], {
      type: 'application/json',
    })
    const link = document.createElement('a')
    link.href = URL.createObjectURL(blob)
    link.download = `nvsop-media-${host.id}.json`
    link.click()
    URL.revokeObjectURL(link.href)
  } catch (error) {
    recordFailure(error)
  } finally {
    exportLoading.value = null
  }
}

async function readAllCameraMedia(): Promise<CameraMediaView[]> {
  const firstPage = await readCameraMedia(1, 100)
  if (firstPage.items.length >= firstPage.total) return firstPage.items
  const pageCount = Math.ceil(firstPage.total / firstPage.page_size)
  const remainingPages = await Promise.all(
    Array.from({ length: pageCount - 1 }, (_, index) =>
      readCameraMedia(index + 2, firstPage.page_size),
    ),
  )
  return [firstPage, ...remainingPages].flatMap((page) => page.items)
}

function recordFailure(error: unknown): void {
  if (error instanceof ControlPlaneError) {
    failure.value = error.detail ?? error.message
  } else {
    failure.value = '请求未能完成，请稍后重试'
  }
}

async function load(): Promise<void> {
  if (!mayViewCameras.value) return
  loading.value = true
  failure.value = ''
  try {
    const nextItems = await readAllCameraMedia()
    const nextById = new Map(nextItems.map((camera) => [camera.camera_id, camera]))
    for (const camera of cameras.value) {
      const next = nextById.get(camera.camera_id)
      if (
        !next ||
        next.media_path !== camera.media_path ||
        next.mediamtx_address !== camera.mediamtx_address ||
        next.mediamtx_playback_address !== camera.mediamtx_playback_address ||
        next.camera_status !== 'active' ||
        next.host_status !== 'active' ||
        next.station_status !== 'active'
      ) {
        stopPreview(camera.camera_id)
        stopPlayback(camera.camera_id)
      }
    }
    cameras.value = nextItems
    for (const camera of cameras.value) {
      playerStatus[camera.camera_id] ??= 'idle'
    }
  } catch (error) {
    recordFailure(error)
  } finally {
    loading.value = false
  }
}

onMounted(load)
onBeforeUnmount(() => {
  for (const camera of cameras.value) {
    stopPreview(camera.camera_id)
    stopPlayback(camera.camera_id)
  }
})
</script>

<template>
  <section v-if="mayViewCameras" class="media-panel" aria-labelledby="camera-media-heading">
    <header class="media-panel__header">
      <div>
        <p class="media-panel__eyebrow">直接媒体路径</p>
        <h2 id="camera-media-heading" class="media-panel__heading">相机预览与录像</h2>
        <p class="media-panel__intro">
          浏览器直连推理机的 MediaMTX；中心只返回稳定 path 和非秘密地址，不代理视频流。
        </p>
      </div>
      <ElButton v-if="selectedCameraId" @click="showAllCameras">返回多路</ElButton>
    </header>

    <p v-if="loading" class="media-panel__message">正在加载相机媒体配置…</p>
    <p v-else-if="failure" class="media-panel__failure" role="alert">{{ failure }}</p>
    <p v-else-if="cameras.length === 0" class="media-panel__message">还没有相机媒体配置。</p>

    <div v-else class="media-panel__grid">
      <article v-for="camera in visibleCameras" :key="camera.camera_id" class="media-card">
        <div class="media-card__head">
          <div>
            <h3 class="media-card__title">{{ camera.camera_name }}</h3>
            <p class="media-card__meta">
              {{ camera.station_name }} · {{ camera.host_name }} · {{ camera.media_path }}
            </p>
          </div>
          <span class="media-card__mode">
            {{ camera.media_path_mode === 'passthrough' ? '零转码' : 'CPU 转码（额外 CPU）' }} /
            {{
              camera.recording_mode === 'continuous' ? '连续录像' : '仅预览（仅限非 SOP 调试相机）'
            }}
          </span>
        </div>

        <video
          :ref="(element) => setVideoRef(camera.camera_id, element)"
          class="media-card__video"
          muted
          playsinline
          controls
          :aria-label="`${camera.camera_name}实时预览`"
        >
          <track kind="captions" srclang="zh" label="中文" src="data:text/vtt,WEBVTT" />
        </video>
        <div class="media-card__status" aria-live="polite">
          <span>播放：</span>
          <ElTag :type="statusType(playerStatus[camera.camera_id])" disable-transitions>
            {{ statusLabel(playerStatus[camera.camera_id]) }}
          </ElTag>
          <span>部署实测：未验证</span>
          <ElButton
            v-if="playerStatus[camera.camera_id] === 'autoplay_blocked'"
            size="small"
            @click="playVideo(camera.camera_id)"
            >播放</ElButton
          >
          <span v-if="playerFailure[camera.camera_id]" class="media-card__error">
            {{ playerFailure[camera.camera_id] }}
          </span>
        </div>
        <div class="media-card__actions">
          <ElButton type="primary" @click="startPreview(camera)">开始预览</ElButton>
          <ElButton @click="stopPreview(camera.camera_id)">停止</ElButton>
          <ElButton @click="selectCamera(camera.camera_id)">详情</ElButton>
          <ElButton v-if="mayEditCameras" @click="openCameraEdit(camera)">编辑媒体策略</ElButton>
        </div>
        <div class="media-card__direct">
          <span>WebRTC：{{ camera.mediamtx_address ?? '未配置' }}</span>
          <span>回放：{{ camera.mediamtx_playback_address ?? '未配置' }}</span>
          <span>凭据：{{ camera.credentials_configured ? '已配置' : '未配置' }}</span>
        </div>

        <div class="media-card__playback">
          <h4>录像查询（Asia/Shanghai）</h4>
          <div class="media-card__range">
            <label :for="`media-range-start-${camera.camera_id}`">
              <span>开始</span>
              <input
                :id="`media-range-start-${camera.camera_id}`"
                v-model="rangeStart"
                type="datetime-local"
              />
            </label>
            <label :for="`media-range-end-${camera.camera_id}`">
              <span>结束</span>
              <input
                :id="`media-range-end-${camera.camera_id}`"
                v-model="rangeEnd"
                type="datetime-local"
              />
            </label>
            <ElButton @click="queryPlayback(camera)">查询片段</ElButton>
          </div>
          <p v-if="playbackFailure[camera.camera_id]" class="media-card__error">
            {{ playbackFailure[camera.camera_id] }}
          </p>
          <p v-if="playbackNotice[camera.camera_id]" class="media-panel__warning">
            {{ playbackNotice[camera.camera_id] }}
          </p>
          <ul v-if="intervals[camera.camera_id]?.length" class="media-card__intervals">
            <li
              v-for="interval in intervals[camera.camera_id]"
              :key="`${interval.start}-${interval.end}`"
            >
              <ElButton
                link
                type="primary"
                :aria-label="`${camera.camera_name}播放${formatShanghai(interval.start)}至${formatShanghai(interval.end)}`"
                @click="setPlaybackInterval(camera, interval)"
              >
                {{ formatShanghai(interval.start) }} – {{ formatShanghai(interval.end) }}
              </ElButton>
            </li>
          </ul>
          <p v-else class="media-card__meta">暂无可用片段；可能已过录像窗口或存在时间空洞。</p>
          <video
            v-if="playbackUrls[camera.camera_id]"
            class="media-card__recording"
            controls
            crossorigin="anonymous"
            :src="playbackUrls[camera.camera_id]"
            :aria-label="`${camera.camera_name}录像回放`"
            @error="handlePlaybackError(camera.camera_id)"
          >
            <track kind="captions" srclang="zh" label="中文" src="data:text/vtt,WEBVTT" />
          </video>
        </div>
      </article>
    </div>

    <div v-if="props.hosts.length && mayEditHosts" class="media-hosts">
      <div class="media-hosts__head">
        <div>
          <h3>推理机媒体配置</h3>
          <p class="media-panel__intro">WebRTC 与原生回放地址独立保存；录像窗口按推理机生效。</p>
        </div>
      </div>
      <table class="media-hosts__table">
        <caption class="media-panel__sr-only">
          推理机媒体配置
        </caption>
        <thead>
          <tr>
            <th scope="col">推理机</th>
            <th scope="col">WebRTC</th>
            <th scope="col">回放</th>
            <th scope="col">窗口（秒）</th>
            <th scope="col">操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="host in props.hosts" :key="host.id">
            <th scope="row">{{ host.name }}</th>
            <td>{{ host.mediamtx_address ?? '未配置' }}</td>
            <td>{{ host.mediamtx_playback_address ?? '未配置' }}</td>
            <td>{{ host.recording_window_seconds }}</td>
            <td class="media-hosts__actions">
              <ElButton link type="primary" @click="openHostEdit(host)">编辑</ElButton>
              <ElButton
                link
                type="primary"
                :loading="exportLoading === host.id"
                @click="exportHost(host)"
                >导出本机配置</ElButton
              >
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <ElDialog v-model="editCameraDialog" title="编辑相机媒体策略" width="38rem">
      <ElForm label-position="top" @submit.prevent="submitCameraEdit">
        <ElFormItem label="相机名称"><ElInput v-model="cameraDraft.name" /></ElFormItem>
        <div class="media-form-grid">
          <ElFormItem label="媒体路径">
            <ElSelect v-model="cameraDraft.media_path_mode" class="media-form-grid__full">
              <ElOption label="零转码（passthrough）" value="passthrough" />
              <ElOption label="CPU 转码" value="cpu_transcode" />
            </ElSelect>
          </ElFormItem>
          <ElFormItem label="录像模式">
            <ElSelect v-model="cameraDraft.recording_mode" class="media-form-grid__full">
              <ElOption label="仅预览" value="preview_only" />
              <ElOption label="连续录像" value="continuous" />
            </ElSelect>
          </ElFormItem>
        </div>
      </ElForm>
      <template #footer>
        <ElButton @click="editCameraDialog = false">取消</ElButton>
        <ElButton type="primary" @click="submitCameraEdit">保存</ElButton>
      </template>
    </ElDialog>

    <ElDialog v-model="editHostDialog" title="编辑推理机媒体配置" width="40rem">
      <ElForm label-position="top" @submit.prevent="submitHostEdit">
        <div class="media-form-grid">
          <ElFormItem label="名称"><ElInput v-model="hostDraft.name" /></ElFormItem>
          <ElFormItem label="推理机地址"><ElInput v-model="hostDraft.address" /></ElFormItem>
          <ElFormItem label="WebRTC 直连地址"
            ><ElInput v-model="hostDraft.mediamtx_address"
          /></ElFormItem>
          <ElFormItem label="原生回放地址"
            ><ElInput v-model="hostDraft.mediamtx_playback_address"
          /></ElFormItem>
          <ElFormItem label="录像窗口（秒）"
            ><ElInput v-model="hostDraft.recording_window_seconds" type="number"
          /></ElFormItem>
          <p
            v-if="
              editingHost &&
              Number(hostDraft.recording_window_seconds) < editingHost.recording_window_seconds
            "
            class="media-panel__warning"
          >
            缩短窗口前必须在推理机本地根据实际分段生成影响快照并明确确认；本次中心保存不会伪造已应用状态。
          </p>
          <ElFormItem label="磁盘水位（百分比）"
            ><ElInput v-model="hostDraft.disk_watermark_percent" type="number"
          /></ElFormItem>
        </div>
      </ElForm>
      <template #footer>
        <ElButton @click="editHostDialog = false">取消</ElButton>
        <ElButton type="primary" @click="submitHostEdit">保存</ElButton>
      </template>
    </ElDialog>
  </section>
</template>

<style scoped>
.media-panel {
  margin-top: 2rem;
  padding: 1.25rem;
  border: 1px solid var(--el-border-color-lighter);
  background: var(--el-bg-color);
}

.media-panel__header,
.media-hosts__head,
.media-card__head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 1rem;
}

.media-panel__eyebrow {
  margin: 0 0 0.35rem;
  color: var(--el-color-primary);
  font-size: 0.8rem;
}

.media-panel__heading,
.media-card__title,
.media-hosts h3 {
  margin: 0;
}

.media-panel__intro,
.media-panel__message,
.media-card__meta {
  color: var(--el-text-color-secondary);
}

.media-panel__failure,
.media-card__error {
  color: var(--el-color-danger);
}

.media-panel__warning {
  grid-column: 1 / -1;
  margin: 0;
  color: var(--el-color-warning-dark-2);
}

.media-panel__grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(22rem, 1fr));
  gap: 1rem;
  margin-top: 1rem;
}

.media-card {
  min-width: 0;
  padding: 1rem;
  border: 1px solid var(--el-border-color-lighter);
}

.media-card__mode {
  color: var(--el-color-primary);
  font-size: 0.8rem;
  white-space: nowrap;
}

.media-card__video,
.media-card__recording {
  display: block;
  width: 100%;
  min-height: 12rem;
  margin-top: 0.85rem;
  background: #111;
  object-fit: contain;
}

.media-card__status,
.media-card__actions,
.media-card__direct,
.media-card__range {
  display: flex;
  flex-wrap: wrap;
  gap: 0.5rem;
  align-items: center;
  margin-top: 0.75rem;
}

.media-card__direct {
  flex-direction: column;
  align-items: flex-start;
  color: var(--el-text-color-secondary);
  font-size: 0.8rem;
  overflow-wrap: anywhere;
}

.media-card__playback {
  margin-top: 1rem;
  padding-top: 0.85rem;
  border-top: 1px solid var(--el-border-color-lighter);
}

.media-card__playback h4 {
  margin: 0;
}

.media-card__range label {
  display: grid;
  gap: 0.2rem;
  color: var(--el-text-color-secondary);
  font-size: 0.8rem;
}

.media-card__range input {
  min-width: 12rem;
  padding: 0.35rem;
  border: 1px solid var(--el-border-color);
  border-radius: 4px;
}

.media-card__intervals {
  margin: 0.75rem 0 0;
  padding-left: 1.25rem;
  color: var(--el-text-color-secondary);
  font-size: 0.85rem;
}

.media-hosts {
  margin-top: 1.5rem;
  overflow: auto;
}

.media-hosts__table {
  width: 100%;
  min-width: 48rem;
  margin-top: 0.75rem;
  border-collapse: collapse;
}

.media-hosts__table th,
.media-hosts__table td {
  padding: 0.65rem;
  border-bottom: 1px solid var(--el-border-color-lighter);
  text-align: left;
  overflow-wrap: anywhere;
}

.media-hosts__actions {
  white-space: nowrap;
}

.media-form-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 0 1rem;
}

.media-form-grid__full {
  width: 100%;
}

.media-panel__sr-only {
  position: absolute;
  width: 1px;
  height: 1px;
  overflow: hidden;
  clip: rect(0 0 0 0);
  white-space: nowrap;
}

@media (max-width: 720px) {
  .media-panel__header,
  .media-card__head {
    flex-direction: column;
  }

  .media-form-grid {
    grid-template-columns: 1fr;
  }
}

.media-panel :focus-visible {
  outline: 2px solid var(--el-color-primary);
  outline-offset: 2px;
}
</style>
