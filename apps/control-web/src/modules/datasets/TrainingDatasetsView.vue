<script setup lang="ts">
import { ElButton, ElMessage, ElTag } from 'element-plus'
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRoute } from 'vue-router'

import {
  ControlPlaneError,
  createAnnotationContext,
  confirmVideoUpload,
  createTrainingDataset,
  listAnnotations,
  listDatasetActionListVersions,
  readAnnotationContext,
  readDatasetMembers,
  readJob,
  readTrainingDatasets,
  registerDatasetActionList,
  requestVideoUpload,
  retryVideoUpload,
  type AnnotationContextView,
  type DatasetActionListHistory,
  type AnnotationHistoryView,
  type AnnotationSubmissionView,
  type DatasetRetry,
  type DatasetUploadRequest,
  type FieldError,
  type TrainingDataset,
  type TrainingDatasetMember,
} from '@/api/controlPlane'
import { useSessionStore } from '@/session/store'

import { ObjectUploadError, uploadVideoObject } from './upload'

const DATASET_VIEW_PERMISSION = 'dataset.dataset.view'
const DATASET_EDIT_PERMISSION = 'dataset.dataset.edit'
const DATASET_IMPORT_PERMISSION = 'dataset.dataset.import'
const DATASET_PAGE_SIZE = 50
const MEMBER_PAGE_SIZE = 50
const PENDING_JOB_STATUSES = ['pending', 'enqueued', 'running']
const ANNOTATION_PREPARATION_POLL_MS = 1000
const ANNOTATION_PREPARATION_MAX_POLLS = 300
const PENDING_UPLOAD_STORAGE_KEY = 'sop.pending-training-upload'

interface PendingUploadRecord {
  datasetId: string
  memberId: string
  attemptId: string
  idempotencyKey: string
  originalFilename: string
  source: string
  declaredSize: number
  declaredSha256: string
}

function isPendingUploadRecord(value: unknown): value is PendingUploadRecord {
  if (typeof value !== 'object' || value === null) return false
  const record = value as Record<string, unknown>
  return (
    typeof record.datasetId === 'string' &&
    typeof record.memberId === 'string' &&
    typeof record.attemptId === 'string' &&
    typeof record.idempotencyKey === 'string' &&
    typeof record.originalFilename === 'string' &&
    typeof record.source === 'string' &&
    typeof record.declaredSize === 'number' &&
    Number.isInteger(record.declaredSize) &&
    record.declaredSize > 0 &&
    typeof record.declaredSha256 === 'string'
  )
}

function readPendingUpload(): PendingUploadRecord | null {
  if (typeof window === 'undefined') return null
  try {
    const serialized = window.sessionStorage.getItem(PENDING_UPLOAD_STORAGE_KEY)
    if (serialized === null) return null
    const parsed: unknown = JSON.parse(serialized)
    return isPendingUploadRecord(parsed) ? parsed : null
  } catch {
    return null
  }
}

const session = useSessionStore()
const route = useRoute()
const mayView = computed(() => session.may(DATASET_VIEW_PERMISSION))
const mayEdit = computed(() => session.may(DATASET_EDIT_PERMISSION))
const mayImport = computed(() => session.may(DATASET_IMPORT_PERMISSION))
const mayAnnotate = computed(() => mayView.value && mayEdit.value)

const datasets = ref<TrainingDataset[]>([])
const datasetPageNumber = ref(1)
const datasetTotal = ref(0)
const members = ref<TrainingDatasetMember[]>([])
const memberPageNumber = ref(1)
const memberTotal = ref(0)
const selectedDatasetId = ref('')
const knownDatasetId = ref('')
const datasetName = ref('')
const source = ref('')
const selectedFile = ref<File | null>(null)
const loadingDatasets = ref(true)
const loadingMembers = ref(false)
const failure = ref<FailureNotice | null>(null)
const fieldErrors = ref<FieldError[]>([])
const transferProgress = ref<number | null>(null)
const transferPhase = ref<TransferPhase>('idle')
const uploadRequest = ref<DatasetUploadRequest | null>(null)
const activeMemberId = ref('')
const activeAttemptId = ref('')
const pendingUpload = ref<PendingUploadRecord | null>(readPendingUpload())
const activeIdempotencyKey = ref('')
const uploadNeedsRenewal = ref(false)
const activeJobId = ref('')
const jobStatuses = ref<Record<string, string>>({})
const jobFailures = ref<Record<string, string | null>>({})
const pollingTimer = ref<number | null>(null)
const annotationMemberId = ref('')
const annotationContext = ref<AnnotationContextView | null>(null)
const loadingAnnotationContext = ref(false)
const annotationHistoryMemberId = ref('')
const annotationHistory = ref<AnnotationHistoryView | null>(null)
const loadingAnnotationHistory = ref(false)
const actionListInput = ref('')
const actionListHistory = ref<DatasetActionListHistory | null>(null)
const loadingActionListHistory = ref(false)

interface FailureNotice {
  message: string
  code: string | null
  recovery: string | null
}

type TransferPhase =
  | 'idle'
  | 'requesting'
  | 'awaiting_file'
  | 'uploading'
  | 'awaiting_confirmation'
  | 'validating'
  | 'failed'

const selectedDataset = computed(
  () => datasets.value.find((dataset) => dataset.id === selectedDatasetId.value) ?? null,
)
const activeMember = computed(
  () =>
    members.value.find((member) => member.id === activeMemberId.value) ??
    (uploadRequest.value?.member.id === activeMemberId.value ? uploadRequest.value.member : null),
)
const hasPendingMembers = computed(() => {
  if (mayView.value) {
    return members.value.some((member) =>
      ['pending_validation', 'validating'].includes(member.status),
    )
  }
  if (!mayImport.value || !activeJobId.value) return false
  const status = jobStatuses.value[activeJobId.value]
  return status === undefined || PENDING_JOB_STATUSES.includes(status)
})
const datasetPageCount = computed(() =>
  Math.max(1, Math.ceil(datasetTotal.value / DATASET_PAGE_SIZE)),
)
const memberPageCount = computed(() => Math.max(1, Math.ceil(memberTotal.value / MEMBER_PAGE_SIZE)))

function resetFailure(): void {
  failure.value = null
  fieldErrors.value = []
}

function markUploadFailed(): void {
  if (transferPhase.value === 'requesting' || transferPhase.value === 'uploading') {
    transferPhase.value = 'failed'
    uploadNeedsRenewal.value = activeIdempotencyKey.value !== ''
  }
}

function recordFailure(error: unknown): void {
  if (error instanceof ControlPlaneError) {
    failure.value = {
      message: error.detail ?? error.message,
      code: error.errorCode,
      recovery: error.status === 401 ? '重新登录' : null,
    }
    fieldErrors.value = error.fieldErrors
    return
  }
  if (error instanceof ObjectUploadError) {
    failure.value = {
      message: error.message,
      code: 'OBJECT_UPLOAD_FAILED',
      recovery: '重试上传',
    }
    fieldErrors.value = []
    return
  }
  failure.value = {
    message: error instanceof Error ? error.message : '请求未能完成，请稍后重试',
    code: null,
    recovery: null,
  }
  fieldErrors.value = []
}

function statusLabel(status: string): string {
  switch (status) {
    case 'pending_upload':
      return '待上传'
    case 'pending_validation':
      return '待校验'
    case 'validating':
      return '校验中'
    case 'registered':
      return '已登记'
    case 'failed':
      return '失败'
    default:
      return `未知状态（${status}）`
  }
}

function statusTag(status: string): 'success' | 'warning' | 'danger' | 'info' {
  switch (status) {
    case 'registered':
      return 'success'
    case 'failed':
      return 'danger'
    case 'pending_upload':
      return 'warning'
    default:
      return 'info'
  }
}

function jobStatusLabel(status: string | undefined): string {
  if (status === undefined) return '任务状态待读取'
  switch (status) {
    case 'pending':
      return '任务待投递'
    case 'enqueued':
      return '任务已排队'
    case 'running':
      return '任务执行中'
    case 'succeeded':
      return '任务已完成'
    case 'failed':
      return '任务失败'
    default:
      return `未知任务状态（${status}）`
  }
}

function recoveryLabel(action: string | null): string {
  switch (action) {
    case 'retry_upload':
      return '重新上传'
    case 'retry_validation':
      return '重新校验'
    default:
      return action === null ? '' : `未知恢复动作（${action}）`
  }
}

function centerPhaseLabel(): string {
  if (transferPhase.value === 'requesting') return '正在申请直传授权…'
  if (transferPhase.value === 'uploading') return '尚未通知中心校验'
  if (transferPhase.value === 'awaiting_confirmation') return '等待通知中心校验'
  if (transferPhase.value === 'validating') return '校验任务已提交'
  if (activeMember.value !== null) return `中心校验：${statusLabel(activeMember.value.status)}`
  return '尚未开始'
}

function formatBytes(value: number | null): string {
  if (value === null) return '—'
  if (value < 1024) return `${value} B`
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KiB`
  return `${(value / (1024 * 1024)).toFixed(1)} MiB`
}

function formatDuration(value: number | null): string {
  return value === null ? '—' : `${value.toFixed(2)} 秒`
}

function formatTime(value: string): string {
  const parsed = new Date(value)
  return Number.isNaN(parsed.valueOf())
    ? value
    : parsed.toLocaleString('zh-CN', { dateStyle: 'medium', timeStyle: 'short' })
}

function selectFile(event: Event): void {
  const input = event.target as HTMLInputElement
  selectedFile.value = input.files?.[0] ?? null
  transferProgress.value = null
  if (transferPhase.value === 'failed') {
    transferPhase.value = 'idle'
  }
  resetFailure()
}

async function loadDatasets(pageNumber = 1): Promise<void> {
  if (!mayView.value) {
    loadingDatasets.value = false
    return
  }
  loadingDatasets.value = true
  const previousSelectedId = selectedDatasetId.value
  try {
    const page =
      pageNumber === 1
        ? await readTrainingDatasets()
        : await readTrainingDatasets(pageNumber, DATASET_PAGE_SIZE)
    datasets.value = page.items
    datasetPageNumber.value = page.page
    datasetTotal.value = page.total
    if (!previousSelectedId) {
      const requested = typeof route.query.dataset === 'string' ? route.query.dataset : ''
      selectedDatasetId.value = datasets.value.some((dataset) => dataset.id === requested)
        ? requested
        : (datasets.value[0]?.id ?? '')
    }
    if (selectedDatasetId.value && selectedDatasetId.value !== previousSelectedId) {
      memberPageNumber.value = 1
      await loadMembers(1)
    }
  } catch (error) {
    recordFailure(error)
  } finally {
    loadingDatasets.value = false
  }
}

async function loadMembers(pageNumber = 1): Promise<void> {
  if (!mayView.value || !selectedDatasetId.value) {
    members.value = []
    memberTotal.value = 0
    return
  }
  loadingMembers.value = true
  try {
    const page =
      pageNumber === 1
        ? await readDatasetMembers(selectedDatasetId.value)
        : await readDatasetMembers(selectedDatasetId.value, pageNumber, MEMBER_PAGE_SIZE)
    members.value = page.items
    memberPageNumber.value = page.page
    memberTotal.value = page.total
    syncPolling()
  } catch (error) {
    recordFailure(error)
  } finally {
    loadingMembers.value = false
  }
}

function changeDatasetPage(pageNumber: number): void {
  if (pageNumber < 1 || pageNumber > datasetPageCount.value) return
  void loadDatasets(pageNumber)
}

function changeMemberPage(pageNumber: number): void {
  if (pageNumber < 1 || pageNumber > memberPageCount.value) return
  void loadMembers(pageNumber)
}

function chooseDataset(): void {
  activeMemberId.value = ''
  activeAttemptId.value = ''
  activeIdempotencyKey.value = ''
  uploadNeedsRenewal.value = false
  uploadRequest.value = null
  transferProgress.value = null
  transferPhase.value = 'idle'
  memberPageNumber.value = 1
  void loadMembers(1)
}

function selectDataset(datasetId: string): void {
  selectedDatasetId.value = datasetId
  actionListHistory.value = null
  actionListInput.value = ''
  chooseDataset()
}

async function readActionListHistory(): Promise<void> {
  if (!mayView.value || !selectedDatasetId.value) return
  resetFailure()
  loadingActionListHistory.value = true
  try {
    actionListHistory.value = await listDatasetActionListVersions(selectedDatasetId.value)
  } catch (error) {
    recordFailure(error)
  } finally {
    loadingActionListHistory.value = false
  }
}

async function saveActionList(): Promise<void> {
  if (!mayEdit.value || !selectedDatasetId.value) return
  resetFailure()
  const actions = actionListInput.value
    .split(/\r?\n/)
    .map((value) => value.trim())
    .filter((value) => value.length > 0)
  if (actions.length === 0) {
    failure.value = {
      message: '请输入至少一条动作描述',
      code: 'ACTION_LIST_INVALID',
      recovery: null,
    }
    return
  }
  try {
    const revision = await registerDatasetActionList(selectedDatasetId.value, actions)
    actionListInput.value = ''
    actionListHistory.value = {
      items: [...(actionListHistory.value?.items ?? []), revision],
    }
  } catch (error) {
    recordFailure(error)
  }
}

async function createDataset(): Promise<void> {
  resetFailure()
  const name = datasetName.value.trim()
  if (!name) {
    failure.value = {
      message: '请输入训练数据集名称',
      code: 'DATASET_NAME_INVALID',
      recovery: null,
    }
    return
  }
  try {
    const created = await createTrainingDataset(name)
    datasets.value = [created, ...datasets.value].slice(0, DATASET_PAGE_SIZE)
    datasetPageNumber.value = 1
    datasetTotal.value += 1
    selectedDatasetId.value = created.id
    knownDatasetId.value = created.id
    datasetName.value = ''
    members.value = []
    memberPageNumber.value = 1
    memberTotal.value = 0
    ElMessage.success('训练数据集已创建')
  } catch (error) {
    recordFailure(error)
  }
}

function activeDatasetId(): string {
  const preferred = mayView.value ? selectedDatasetId.value : knownDatasetId.value
  return (preferred || knownDatasetId.value || selectedDatasetId.value).trim()
}

async function sha256(file: File): Promise<string> {
  if (globalThis.crypto?.subtle === undefined) {
    throw new Error('当前浏览器不支持文件摘要计算，请使用支持 Web Crypto 的浏览器')
  }
  const digest = await globalThis.crypto.subtle.digest('SHA-256', await file.arrayBuffer())
  return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, '0')).join('')
}

function newIdempotencyKey(): string {
  return globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random().toString(16).slice(2)}`
}

function resumableIdempotencyKey(
  datasetId: string,
  file: File,
  sourceValue: string,
  declaredSha256: string,
): string | null {
  const record = pendingUpload.value
  if (
    record === null ||
    record.datasetId !== datasetId ||
    record.originalFilename !== file.name ||
    record.source !== sourceValue ||
    record.declaredSize !== file.size ||
    record.declaredSha256 !== declaredSha256
  ) {
    return null
  }
  return record.idempotencyKey
}

function rememberPendingUpload(
  datasetId: string,
  idempotencyKey: string,
  result: DatasetUploadRequest,
): void {
  if (result.upload === null) {
    clearPendingUpload(result.attempt.id)
    return
  }
  const record: PendingUploadRecord = {
    datasetId,
    memberId: result.member.id,
    attemptId: result.attempt.id,
    idempotencyKey,
    originalFilename: result.member.original_filename,
    source: result.member.source,
    declaredSize: result.member.declared_size,
    declaredSha256: result.member.declared_sha256,
  }
  pendingUpload.value = record
  try {
    window.sessionStorage.setItem(PENDING_UPLOAD_STORAGE_KEY, JSON.stringify(record))
  } catch {
    // 会话存储不可用时仍保留当前页面内的恢复状态。
  }
}

function clearPendingUpload(attemptId?: string): void {
  if (attemptId !== undefined && pendingUpload.value?.attemptId !== attemptId) return
  pendingUpload.value = null
  try {
    window.sessionStorage.removeItem(PENDING_UPLOAD_STORAGE_KEY)
  } catch {
    // 会话存储不可用不应阻断已完成的上传。
  }
}

function applyMember(member: TrainingDatasetMember): void {
  knownDatasetId.value = member.dataset_id
  const index = members.value.findIndex((item) => item.id === member.id)
  if (index === -1) {
    members.value = [member, ...members.value].slice(0, MEMBER_PAGE_SIZE)
    memberTotal.value += 1
  } else {
    members.value.splice(index, 1, member)
  }
  activeMemberId.value = member.id
  activeAttemptId.value = member.current_attempt_id
  activeJobId.value = member.validation_job_id ?? ''
  syncPolling()
}

function memberWithJob(
  member: TrainingDatasetMember,
  job: { id: string } | null,
): TrainingDatasetMember {
  if (job === null || member.validation_job_id === job.id) return member
  return { ...member, validation_job_id: job.id }
}

function rememberUpload(result: DatasetUploadRequest): void {
  uploadRequest.value = result
  activeMemberId.value = result.member.id
  activeAttemptId.value = result.attempt.id
  applyMember(result.member)
}

async function transferAndConfirm(file: File, result: DatasetUploadRequest): Promise<void> {
  if (result.upload === null) {
    throw new Error('中心没有返回本次上传的直传说明')
  }
  transferProgress.value = 0
  transferPhase.value = 'uploading'
  await uploadVideoObject(result.upload, file, (progress) => {
    transferProgress.value = progress
  })
  transferProgress.value = 100
  transferPhase.value = 'awaiting_confirmation'
  // 对象已传输后不再保留旧的签名写权限；确认成功前仍保留非敏感恢复记录。
  uploadRequest.value = { ...result, upload: null }
  await confirmCurrentUpload()
}

async function requestAndUpload(): Promise<void> {
  const file = selectedFile.value
  const datasetId = activeDatasetId()
  if (file === null) {
    failure.value = { message: '请选择一个视频文件', code: 'FILE_REQUIRED', recovery: null }
    return
  }
  if (!datasetId) {
    failure.value = { message: '请选择或填写训练数据集', code: 'DATASET_REQUIRED', recovery: null }
    return
  }
  if (!source.value.trim()) {
    failure.value = { message: '请输入视频来源', code: 'SOURCE_INVALID', recovery: null }
    return
  }

  resetFailure()
  transferPhase.value = 'requesting'
  try {
    const sourceValue = source.value.trim()
    const declaredSha256 = await sha256(file)
    const idempotencyKey =
      resumableIdempotencyKey(datasetId, file, sourceValue, declaredSha256) ?? newIdempotencyKey()
    activeIdempotencyKey.value = idempotencyKey
    uploadNeedsRenewal.value = false
    const result = await requestVideoUpload(
      datasetId,
      {
        original_filename: file.name,
        source: sourceValue,
        declared_size: file.size,
        declared_sha256: declaredSha256,
      },
      idempotencyKey,
    )
    rememberUpload(result)
    rememberPendingUpload(datasetId, idempotencyKey, result)
    await transferAndConfirm(file, result)
  } catch (error) {
    markUploadFailed()
    recordFailure(error)
  }
}

async function confirmCurrentUpload(): Promise<void> {
  const datasetId = activeDatasetId()
  if (!datasetId || !activeMemberId.value || !activeAttemptId.value) {
    throw new Error('缺少当前视频或上传尝试身份')
  }
  const result = await confirmVideoUpload(datasetId, activeMemberId.value, activeAttemptId.value)
  applyMember(memberWithJob(result.member, result.job))
  // 只有中心确认请求成功后才删除刷新恢复记录。
  clearPendingUpload(activeAttemptId.value)
  if (result.job !== null) {
    activeJobId.value = result.job.id
    jobStatuses.value = { ...jobStatuses.value, [result.job.id]: result.job.status }
    jobFailures.value = { ...jobFailures.value, [result.job.id]: result.job.failure_code }
    transferPhase.value = 'validating'
  } else {
    transferPhase.value = 'idle'
  }
}

async function confirmAfterTransfer(): Promise<void> {
  resetFailure()
  try {
    await confirmCurrentUpload()
  } catch (error) {
    recordFailure(error)
  }
}

async function retryMember(member: TrainingDatasetMember): Promise<void> {
  if (!mayImport.value || !member.recovery_action) return
  const datasetId = activeDatasetId()
  if (!datasetId) return
  resetFailure()
  try {
    const mode = member.recovery_action === 'retry_validation' ? 'retry_validation' : 'retry_upload'
    const idempotencyKey = mode === 'retry_upload' ? newIdempotencyKey() : undefined
    const result =
      idempotencyKey === undefined
        ? await retryVideoUpload(datasetId, member.id, mode)
        : await retryVideoUpload(datasetId, member.id, mode, idempotencyKey)
    applyRetry(result, idempotencyKey)
    if (mode === 'retry_upload') {
      if (idempotencyKey !== undefined) rememberPendingUpload(datasetId, idempotencyKey, result)
      selectedFile.value = null
      transferProgress.value = null
      transferPhase.value = 'awaiting_file'
      return
    }
    transferPhase.value = result.job === null ? 'idle' : 'validating'
  } catch (error) {
    recordFailure(error)
  }
}

function annotationStatusLabel(status: string): string {
  switch (status) {
    case 'pending':
      return '等待切片'
    case 'running':
      return '切片中'
    case 'succeeded':
      return '已完成'
    case 'failed':
      return '失败'
    default:
      return `未知状态（${status}）`
  }
}

function annotationStatusTag(status: string): 'success' | 'warning' | 'danger' | 'info' {
  switch (status) {
    case 'succeeded':
      return 'success'
    case 'failed':
      return 'danger'
    case 'pending':
    case 'running':
      return 'warning'
    default:
      return 'info'
  }
}

async function enterAnnotation(member: TrainingDatasetMember): Promise<void> {
  if (!mayAnnotate.value || member.status !== 'registered') return
  resetFailure()
  annotationHistoryMemberId.value = ''
  annotationHistory.value = null
  annotationMemberId.value = member.id
  annotationContext.value = null
  loadingAnnotationContext.value = true
  try {
    const created = await createAnnotationContext(selectedDatasetId.value, member.id)
    annotationContext.value = await waitForAnnotationContext(created)
  } catch (error) {
    annotationMemberId.value = ''
    recordFailure(error)
  } finally {
    loadingAnnotationContext.value = false
  }
}

async function waitForAnnotationContext(
  created: AnnotationContextView,
): Promise<AnnotationContextView> {
  if (created.preparation_status === 'succeeded') return created
  if (created.preparation_status === 'failed') {
    throw new Error(created.preparation_failure_detail ?? '标注媒体准备失败')
  }
  if (!['pending', 'running'].includes(created.preparation_status)) {
    throw new Error(`未知标注准备状态（${created.preparation_status}）`)
  }
  for (let poll = 0; poll < ANNOTATION_PREPARATION_MAX_POLLS; poll += 1) {
    await new Promise<void>((resolve) => window.setTimeout(resolve, ANNOTATION_PREPARATION_POLL_MS))
    const current = await readAnnotationContext(created.context_token)
    if (current.preparation_status === 'succeeded') return current
    if (current.preparation_status === 'failed') {
      throw new Error(current.preparation_failure_detail ?? '标注媒体准备失败')
    }
    if (!['pending', 'running'].includes(current.preparation_status)) {
      throw new Error(`未知标注准备状态（${current.preparation_status}）`)
    }
  }
  throw new Error('标注媒体准备超时，请稍后重新进入')
}

function leaveAnnotation(): void {
  annotationMemberId.value = ''
  annotationContext.value = null
}

function annotationLaunchUrl(contextToken: string): string {
  return `/annotation/?context=${encodeURIComponent(contextToken)}`
}

async function readAnnotationHistory(member: TrainingDatasetMember): Promise<void> {
  if (!mayView.value || member.status !== 'registered') return
  resetFailure()
  loadingAnnotationHistory.value = true
  annotationHistoryMemberId.value = member.id
  annotationHistory.value = null
  try {
    annotationHistory.value = await listAnnotations(selectedDatasetId.value, member.id)
  } catch (error) {
    annotationHistoryMemberId.value = ''
    recordFailure(error)
  } finally {
    loadingAnnotationHistory.value = false
  }
}

function annotationClipUrl(
  submission: AnnotationSubmissionView,
  executionId: string,
  clipIndex: number,
): string {
  return `/api/annotation/api/v1/annotation-submissions/${submission.id}/executions/${executionId}/clips/${clipIndex}/download`
}

function annotationArchiveUrl(submission: AnnotationSubmissionView, executionId: string): string {
  return `/api/annotation/api/v1/annotation-submissions/${submission.id}/executions/${executionId}/download-all`
}

function applyRetry(result: DatasetRetry, idempotencyKey?: string): void {
  const member = memberWithJob(result.member, result.job)
  applyMember(member)
  clearPendingUpload()
  activeIdempotencyKey.value = idempotencyKey ?? ''
  uploadNeedsRenewal.value = false
  activeAttemptId.value = result.attempt.id
  activeJobId.value = result.job?.id ?? member.validation_job_id ?? ''
  if (result.job !== null) {
    jobStatuses.value = { ...jobStatuses.value, [result.job.id]: result.job.status }
    jobFailures.value = { ...jobFailures.value, [result.job.id]: result.job.failure_code }
  }
  uploadRequest.value =
    result.upload === null
      ? null
      : {
          member,
          attempt: result.attempt,
          upload: result.upload,
        }
  if (result.job !== null) {
    transferPhase.value = 'validating'
  }
}

async function uploadRetriedFile(): Promise<void> {
  const file = selectedFile.value
  const request = uploadRequest.value
  const datasetId = activeDatasetId()
  if (file === null || request === null) {
    failure.value = { message: '请选择要重新上传的视频文件', code: 'FILE_REQUIRED', recovery: null }
    return
  }
  resetFailure()
  try {
    if (activeIdempotencyKey.value === '' || !uploadNeedsRenewal.value) {
      await transferAndConfirm(file, request)
      return
    }
    const renewed = await requestVideoUpload(
      datasetId,
      {
        original_filename: request.member.original_filename,
        source: request.member.source,
        declared_size: request.member.declared_size,
        declared_sha256: request.member.declared_sha256,
      },
      activeIdempotencyKey.value,
    )
    rememberUpload(renewed)
    uploadNeedsRenewal.value = false
    await transferAndConfirm(file, renewed)
  } catch (error) {
    markUploadFailed()
    recordFailure(error)
  }
}

function submitUpload(): void {
  if (
    (transferPhase.value === 'failed' || transferPhase.value === 'awaiting_file') &&
    uploadRequest.value !== null &&
    uploadRequest.value.upload !== null
  ) {
    void uploadRetriedFile()
    return
  }
  if (transferPhase.value === 'awaiting_confirmation') {
    void confirmAfterTransfer()
    return
  }
  void requestAndUpload()
}

function clearPolling(): void {
  if (pollingTimer.value !== null) {
    window.clearInterval(pollingTimer.value)
    pollingTimer.value = null
  }
}

function syncPolling(): void {
  if ((!mayView.value && !mayImport.value) || !hasPendingMembers.value || !activeDatasetId()) {
    clearPolling()
    return
  }
  if (pollingTimer.value !== null) return
  pollingTimer.value = window.setInterval(() => {
    void refreshPendingMembers()
  }, 2000)
}

async function refreshPendingMembers(): Promise<void> {
  if ((!mayView.value && !mayImport.value) || !activeDatasetId()) return
  try {
    const jobIds = Array.from(
      new Set(
        members.value
          .map((member) => member.validation_job_id)
          .filter((jobId): jobId is string => jobId !== null),
      ),
    )
    const jobs = await Promise.all(
      jobIds.map(async (jobId) => ({ jobId, job: await readJob(jobId) })),
    )
    if (jobs.length > 0) {
      jobStatuses.value = {
        ...jobStatuses.value,
        ...Object.fromEntries(jobs.map(({ jobId, job }) => [jobId, job.status])),
      }
      jobFailures.value = {
        ...jobFailures.value,
        ...Object.fromEntries(jobs.map(({ jobId, job }) => [jobId, job.failure_code])),
      }
    }
    if (!mayView.value) {
      const activeJob = jobs.find(({ jobId }) => jobId === activeJobId.value)?.job
      if (activeJob?.status === 'succeeded') transferPhase.value = 'idle'
      if (activeJob?.status === 'failed') transferPhase.value = 'failed'
      syncPolling()
      return
    }
    const page =
      memberPageNumber.value === 1
        ? await readDatasetMembers(selectedDatasetId.value)
        : await readDatasetMembers(
            selectedDatasetId.value,
            memberPageNumber.value,
            MEMBER_PAGE_SIZE,
          )
    members.value = page.items
    memberPageNumber.value = page.page
    memberTotal.value = page.total
    const active = activeMember.value
    if (active?.validation_job_id) activeJobId.value = active.validation_job_id
    syncPolling()
  } catch (error) {
    if (error instanceof ControlPlaneError && error.status === 401) {
      clearPolling()
    }
    recordFailure(error)
  }
}

onMounted(() => {
  void loadDatasets()
})
onUnmounted(clearPolling)
</script>

<template>
  <section class="datasets" aria-labelledby="datasets-heading">
    <header class="datasets__header">
      <div>
        <p class="datasets__eyebrow">资产中心 / 训练数据</p>
        <h1 id="datasets-heading" class="datasets__heading">训练数据集</h1>
        <p class="datasets__intro">
          视频逐个直传对象存储。传输进度与中心校验分开显示；只有真实对象和媒体事实通过后才会登记。
        </p>
      </div>
      <div class="datasets__facts" aria-label="数据集规则">
        <strong>不经中心中继</strong>
        <span>上传、校验、登记三步独立留痕</span>
      </div>
    </header>

    <div v-if="failure" class="datasets__failure" role="alert">
      <strong>{{ failure.message }}</strong>
      <span v-if="failure.code">错误码：{{ failure.code }}</span>
      <span v-if="failure.recovery">建议动作：{{ failure.recovery }}</span>
    </div>
    <ul v-if="fieldErrors.length" class="datasets__field-errors" aria-label="字段错误">
      <li v-for="error in fieldErrors" :key="`${error.field}:${error.message}`">
        {{ error.field }}：{{ error.message }}
      </li>
    </ul>

    <div v-if="mayImport" class="datasets__actions">
      <form class="datasets__card" aria-label="创建训练数据集" @submit.prevent="createDataset">
        <h2>创建数据集</h2>
        <p>空数据集也是合法状态，之后可以逐个加入视频。</p>
        <label for="dataset-name">
          数据集名称
          <input
            id="dataset-name"
            v-model="datasetName"
            name="dataset-name"
            type="text"
            maxlength="255"
            autocomplete="off"
          />
        </label>
        <ElButton type="primary" native-type="submit">创建数据集</ElButton>
      </form>

      <form class="datasets__card" aria-label="上传训练视频" @submit.prevent="submitUpload">
        <h2>加入一个视频</h2>
        <p>文件先发往本次申请的 MinIO 目标，再通知中心校验。</p>
        <template v-if="mayView">
          <label for="dataset-select">
            目标训练数据集
            <select
              id="dataset-select"
              v-model="selectedDatasetId"
              name="dataset-select"
              @change="chooseDataset"
            >
              <option value="">请选择数据集</option>
              <option v-for="dataset in datasets" :key="dataset.id" :value="dataset.id">
                {{ dataset.name }}
              </option>
            </select>
          </label>
        </template>
        <template v-else>
          <label for="known-dataset-id">
            已知数据集 ID
            <input
              id="known-dataset-id"
              v-model="knownDatasetId"
              name="known-dataset-id"
              type="text"
              autocomplete="off"
            />
          </label>
        </template>
        <label for="video-source">
          视频来源
          <input
            id="video-source"
            v-model="source"
            name="video-source"
            type="text"
            maxlength="255"
          />
        </label>
        <label for="dataset-video">
          视频文件
          <input
            id="dataset-video"
            name="dataset-video"
            type="file"
            accept="video/*,.mp4,.mov,.mkv,.webm"
            @change="selectFile"
          />
        </label>
        <span v-if="selectedFile" class="datasets__file-name">已选择：{{ selectedFile.name }}</span>
        <ElButton
          type="primary"
          native-type="submit"
          :disabled="
            transferPhase === 'requesting' ||
            transferPhase === 'uploading' ||
            transferPhase === 'validating'
          "
        >
          {{
            transferPhase === 'failed' && uploadRequest?.upload !== null
              ? '重试上传'
              : transferPhase === 'awaiting_file'
                ? '开始重新上传'
                : transferPhase === 'awaiting_confirmation'
                  ? '通知中心校验'
                  : '上传视频'
          }}
        </ElButton>
        <p
          v-if="transferPhase !== 'idle'"
          class="datasets__transfer-state"
          role="status"
          aria-live="polite"
        >
          {{ centerPhaseLabel() }}
          <span v-if="transferProgress !== null"> · 本地传输 {{ transferProgress }}%</span>
        </p>
        <p
          v-if="
            pendingUpload &&
            pendingUpload.datasetId === activeDatasetId() &&
            transferPhase === 'idle'
          "
          class="datasets__transfer-state"
          role="status"
        >
          已保留视频身份 {{ pendingUpload.memberId }}；选择相同文件后会继续本次上传。
        </p>
        <ElButton
          v-if="transferPhase === 'awaiting_confirmation'"
          link
          type="warning"
          @click="confirmAfterTransfer"
        >
          重试通知中心
        </ElButton>
      </form>
    </div>

    <section
      v-if="mayView && selectedDatasetId && mayEdit"
      class="datasets__action-list"
      aria-labelledby="action-list-heading"
    >
      <div class="datasets__section-head">
        <div>
          <h2 id="action-list-heading">动作列表</h2>
          <p>每次登记追加一个版本；旧版本只读，不会重新解释已经保存的标注。</p>
        </div>
        <ElButton
          link
          type="primary"
          :disabled="loadingActionListHistory"
          @click="readActionListHistory"
        >
          {{ loadingActionListHistory ? '正在读取…' : '读取版本历史' }}
        </ElButton>
      </div>
      <form
        aria-label="登记动作列表"
        class="datasets__action-list-form"
        @submit.prevent="saveActionList"
      >
        <label for="dataset-action-list">
          动作描述（每行一条，例如 (1) 取料）
          <textarea
            id="dataset-action-list"
            v-model="actionListInput"
            name="dataset-action-list"
            rows="4"
            autocomplete="off"
          />
        </label>
        <ElButton type="primary" native-type="submit">登记新版本</ElButton>
      </form>
      <ol v-if="actionListHistory" class="datasets__action-list-history">
        <li v-for="revision in actionListHistory.items" :key="revision.revision">
          <strong>版本 {{ revision.revision }}</strong>
          <span>{{ revision.actions.join('、') }}</span>
        </li>
        <li v-if="actionListHistory.items.length === 0" class="datasets__muted">
          尚未登记动作列表。
        </li>
      </ol>
    </section>

    <section v-if="mayView" class="datasets__catalog" aria-labelledby="catalog-heading">
      <div class="datasets__section-head">
        <div>
          <h2 id="catalog-heading">数据集</h2>
          <p>数据集没有整体成功状态；每个成员的事实和失败各自保留。</p>
        </div>
        <span v-if="loadingDatasets" class="datasets__muted">正在加载…</span>
        <span v-else
          >第 {{ datasetPageNumber }} / {{ datasetPageCount }} 页，共
          {{ datasetTotal }} 个数据集</span
        >
      </div>
      <table v-if="!loadingDatasets" class="datasets__table">
        <caption class="datasets__caption">
          训练数据集列表
        </caption>
        <thead>
          <tr>
            <th scope="col">名称</th>
            <th scope="col">创建时间</th>
            <th scope="col">更新时间</th>
            <th scope="col">当前选择</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="dataset in datasets" :key="dataset.id">
            <th scope="row">
              {{ dataset.name }}
              <small>{{ dataset.id }}</small>
            </th>
            <td>{{ formatTime(dataset.created_at) }}</td>
            <td>{{ formatTime(dataset.updated_at) }}</td>
            <td>
              <ElTag v-if="selectedDatasetId === dataset.id" type="success" disable-transitions>
                当前数据集
              </ElTag>
              <ElButton v-else link type="primary" @click="selectDataset(dataset.id)">
                查看成员
              </ElButton>
            </td>
          </tr>
          <tr v-if="datasets.length === 0">
            <td colspan="4" class="datasets__empty">还没有数据集。</td>
          </tr>
        </tbody>
      </table>
      <nav
        v-if="datasetTotal > DATASET_PAGE_SIZE"
        class="datasets__pagination"
        aria-label="训练数据集分页"
      >
        <ElButton
          link
          type="primary"
          :disabled="datasetPageNumber <= 1"
          @click="changeDatasetPage(datasetPageNumber - 1)"
        >
          上一页
        </ElButton>
        <span>第 {{ datasetPageNumber }} / {{ datasetPageCount }} 页</span>
        <ElButton
          link
          type="primary"
          :disabled="datasetPageNumber >= datasetPageCount"
          @click="changeDatasetPage(datasetPageNumber + 1)"
        >
          下一页
        </ElButton>
      </nav>
    </section>

    <section
      v-if="mayView && selectedDatasetId"
      class="datasets__members"
      aria-labelledby="members-heading"
    >
      <div class="datasets__section-head">
        <div>
          <h2 id="members-heading">{{ selectedDataset?.name ?? '当前数据集' }} / 视频成员</h2>
          <p>
            当前数据集：<code>{{ selectedDatasetId }}</code>
          </p>
        </div>
        <span
          >第 {{ memberPageNumber }} / {{ memberPageCount }} 页，共 {{ memberTotal }} 个视频</span
        >
      </div>
      <p v-if="loadingMembers" class="datasets__loading">正在加载视频状态…</p>
      <table v-else class="datasets__table datasets__table--members">
        <caption class="datasets__caption">
          视频登记与中心校验结果
        </caption>
        <thead>
          <tr>
            <th scope="col">文件与来源</th>
            <th scope="col">状态</th>
            <th scope="col">声明 / 实际大小</th>
            <th scope="col">声明 / 实际 sha256</th>
            <th scope="col">实际媒体</th>
            <th scope="col">动作标注</th>
            <th scope="col">恢复</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="member in members" :key="member.id">
            <th scope="row">
              <strong>{{ member.original_filename }}</strong>
              <small>{{ member.source }}</small>
              <small>视频 ID：{{ member.id }}</small>
              <small>尝试：{{ member.current_attempt_id }}</small>
            </th>
            <td>
              <ElTag :type="statusTag(member.status)" disable-transitions>
                {{ statusLabel(member.status) }}
              </ElTag>
              <small v-if="member.validation_job_id">
                任务：{{ member.validation_job_id }} ·
                {{ jobStatusLabel(jobStatuses[member.validation_job_id]) }}
              </small>
              <small v-if="member.failure_code" class="datasets__failure-code">
                {{ member.failure_code }}
              </small>
              <small v-if="member.failure_detail">{{ member.failure_detail }}</small>
            </td>
            <td>
              <small>声明：{{ formatBytes(member.declared_size) }}</small>
              <small>实际：{{ formatBytes(member.actual_size) }}</small>
            </td>
            <td>
              <code class="datasets__digest">声明：{{ member.declared_sha256 }}</code>
              <code v-if="member.actual_sha256" class="datasets__digest">
                实际：{{ member.actual_sha256 }}
              </code>
              <span v-else class="datasets__muted">实际摘要待校验</span>
            </td>
            <td>
              <small>时长：{{ formatDuration(member.duration_seconds) }}</small>
              <small>编码：{{ member.codec ?? '—' }}</small>
              <small>容器：{{ member.container ?? '—' }}</small>
            </td>
            <td>
              <ElButton
                v-if="mayAnnotate && member.status === 'registered'"
                link
                type="primary"
                :disabled="loadingAnnotationContext"
                @click="enterAnnotation(member)"
              >
                进入标注
              </ElButton>
              <ElButton
                v-if="mayView && member.status === 'registered'"
                link
                type="success"
                :disabled="loadingAnnotationHistory"
                @click="readAnnotationHistory(member)"
              >
                查看记录
              </ElButton>
              <span v-if="member.status !== 'registered'" class="datasets__muted"
                >校验完成后可用</span
              >
            </td>
            <td>
              <ElButton
                v-if="mayImport && member.recovery_action === 'retry_upload'"
                link
                type="warning"
                @click="retryMember(member)"
              >
                {{ recoveryLabel(member.recovery_action) }}
              </ElButton>
              <ElButton
                v-else-if="mayImport && member.recovery_action === 'retry_validation'"
                link
                type="primary"
                @click="retryMember(member)"
              >
                {{ recoveryLabel(member.recovery_action) }}
              </ElButton>
              <span v-else-if="member.recovery_action" class="datasets__muted">
                {{ recoveryLabel(member.recovery_action) }}
              </span>
              <span v-else class="datasets__muted">—</span>
            </td>
          </tr>
          <tr v-if="members.length === 0">
            <td colspan="7" class="datasets__empty">这个数据集还没有视频成员。</td>
          </tr>
        </tbody>
      </table>
      <nav
        v-if="memberTotal > MEMBER_PAGE_SIZE"
        class="datasets__pagination"
        aria-label="视频成员分页"
      >
        <ElButton
          link
          type="primary"
          :disabled="memberPageNumber <= 1"
          @click="changeMemberPage(memberPageNumber - 1)"
        >
          上一页
        </ElButton>
        <span>第 {{ memberPageNumber }} / {{ memberPageCount }} 页</span>
        <ElButton
          link
          type="primary"
          :disabled="memberPageNumber >= memberPageCount"
          @click="changeMemberPage(memberPageNumber + 1)"
        >
          下一页
        </ElButton>
      </nav>

      <section
        v-if="annotationMemberId"
        class="datasets__annotation-editor"
        aria-labelledby="annotation-editor-heading"
      >
        <div class="datasets__section-head">
          <div>
            <h2 id="annotation-editor-heading">动作标注</h2>
            <p>
              目标视频：<code>{{ annotationMemberId }}</code
              >；进入标注不会重新上传视频。
            </p>
          </div>
          <ElButton link type="info" @click="leaveAnnotation">关闭标注</ElButton>
        </div>
        <p v-if="loadingAnnotationContext" class="datasets__loading">
          正在准备视频、读取动作列表和历史时间段…
        </p>
        <div v-else-if="annotationContext" class="datasets__annotation-launch">
          <p>视频已准备完成，标注控件在独立的 NVIDIA React 界面中打开。</p>
          <a
            class="datasets__annotation-launch-link"
            :href="annotationLaunchUrl(annotationContext.context_token)"
          >
            进入 NVIDIA React 标注界面
          </a>
        </div>
      </section>

      <section
        v-if="annotationHistoryMemberId"
        class="datasets__annotation-history"
        aria-labelledby="annotation-history-heading"
      >
        <div class="datasets__section-head">
          <div>
            <h2 id="annotation-history-heading">已保存动作标注</h2>
            <p>原始时间段、动作列表修订、源对象摘要和切片执行状态均来自中心记录。</p>
          </div>
          <span v-if="loadingAnnotationHistory" class="datasets__muted">正在读取…</span>
        </div>
        <p v-if="annotationHistory && annotationHistory.items.length === 0" class="datasets__empty">
          这个视频尚未标注。
        </p>
        <article
          v-for="submission in annotationHistory?.items ?? []"
          :key="submission.id"
          class="datasets__annotation-record"
        >
          <h3>提交 {{ submission.id }}</h3>
          <p>
            标注者：{{ submission.created_by }} · {{ formatTime(submission.created_at) }} ·
            动作列表修订：{{ submission.action_list_revision }} · 模式：{{ submission.mode }}
          </p>
          <p>
            源对象代次：<code>{{ submission.source_object_version_id }}</code> · sha256：<code>{{
              submission.source_sha256
            }}</code>
          </p>
          <ol>
            <li v-for="(segment, index) in submission.segments" :key="`${submission.id}-${index}`">
              {{ segment.action_description || `动作 ${segment.action_index + 1}` }}：
              {{ segment.start.toFixed(2) }}s – {{ segment.end.toFixed(2) }}s
            </li>
          </ol>
          <div v-for="execution in submission.executions" :key="execution.id">
            <p>
              执行第 {{ execution.generation }} 次：
              <ElTag :type="annotationStatusTag(execution.status)" disable-transitions>
                {{ annotationStatusLabel(execution.status) }}
              </ElTag>
              <span v-if="execution.failure_detail">{{ execution.failure_detail }}</span>
            </p>
            <template v-if="execution.status === 'succeeded'">
              <a
                :href="annotationArchiveUrl(submission, execution.id)"
                target="_blank"
                rel="noreferrer"
              >
                下载全部切片
              </a>
              <ul>
                <li v-for="(_clip, index) in execution.clips" :key="`${execution.id}-${index}`">
                  <a
                    :href="annotationClipUrl(submission, execution.id, index)"
                    target="_blank"
                    rel="noreferrer"
                  >
                    打开第 {{ index + 1 }} 个切片
                  </a>
                </li>
              </ul>
            </template>
          </div>
        </article>
      </section>
    </section>

    <section
      v-if="mayImport && !mayView && activeMember"
      class="datasets__import-only"
      aria-live="polite"
    >
      <h2>本次加入结果</h2>
      <p>
        已创建视频身份 <code>{{ activeMember.id }}</code
        >，上传记录状态：{{ statusLabel(activeMember.status) }}。
      </p>
      <p v-if="activeJobId">
        校验任务：{{ activeJobId }} ·
        {{ jobStatusLabel(jobStatuses[activeJobId]) }}（页面无查看权限，不会读取其他数据集列表）。
      </p>
      <p v-if="activeJobId && jobFailures[activeJobId]" class="datasets__failure-code">
        校验失败码：{{ jobFailures[activeJobId] }}
      </p>
    </section>

    <p v-if="!mayView && !mayImport" class="datasets__empty-state">
      当前账户没有训练数据集查看或导入权限。
    </p>
  </section>
</template>

<style scoped>
.datasets {
  --dataset-ink: #17222f;
  --dataset-paper: #f7f4ec;
  --dataset-teal: #4d8b84;
  --dataset-amber: #d6a13d;
  --dataset-rule: #d9d4c7;
  color: var(--dataset-ink);
}

.datasets__header,
.datasets__section-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 1.5rem;
}

.datasets__header {
  margin-bottom: 1.5rem;
}

.datasets__eyebrow {
  margin: 0 0 0.5rem;
  color: var(--dataset-teal);
  font-size: 0.8rem;
}

.datasets__heading {
  margin: 0 0 0.5rem;
  font-family: Georgia, 'Times New Roman', serif;
  font-size: 1.8rem;
  font-weight: 600;
  letter-spacing: -0.03em;
}

.datasets__intro {
  max-width: 44rem;
  margin: 0;
  color: #5f6872;
  line-height: 1.6;
}

.datasets__facts {
  display: grid;
  gap: 0.3rem;
  min-width: 15rem;
  padding: 0.9rem 1rem;
  border-left: 4px solid var(--dataset-amber);
  background: var(--dataset-paper);
}

.datasets__facts span,
.datasets__card p,
.datasets__section-head p,
.datasets__loading,
.datasets__muted,
.datasets__empty-state,
.datasets__import-only p {
  color: #68717c;
}

.datasets__actions {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 1rem;
  margin-bottom: 1.5rem;
}

.datasets__card,
.datasets__action-list,
.datasets__catalog,
.datasets__members,
.datasets__import-only {
  border-top: 3px solid var(--dataset-ink);
  background: #fff;
}

.datasets__card {
  display: grid;
  gap: 0.6rem;
  padding: 1rem 1.15rem;
}

.datasets__action-list {
  margin-bottom: 1.5rem;
  border-top: 3px solid var(--dataset-amber);
}

.datasets__action-list-form {
  display: grid;
  gap: 0.6rem;
  padding: 1rem 1.15rem;
}

.datasets__action-list-form label {
  display: grid;
  gap: 0.35rem;
  font-weight: 600;
}

.datasets__action-list-form textarea {
  width: 100%;
  padding: 0.5rem;
  border: 1px solid #bfc6cd;
  border-radius: 4px;
  font: inherit;
  resize: vertical;
}

.datasets__action-list-history {
  display: grid;
  gap: 0.5rem;
  margin: 0;
  padding: 0 1.15rem 1rem 2.5rem;
}

.datasets__action-list-history li {
  display: flex;
  flex-wrap: wrap;
  gap: 0.5rem;
}

.datasets__card:nth-child(2),
.datasets__members {
  border-top-color: var(--dataset-teal);
}

.datasets__card h2,
.datasets__catalog h2,
.datasets__members h2,
.datasets__import-only h2 {
  margin: 0;
  font-size: 1.05rem;
}

.datasets__card p {
  margin: 0 0 0.25rem;
  line-height: 1.45;
}

.datasets__card label {
  margin-top: 0.25rem;
  font-weight: 600;
}

.datasets__card input,
.datasets__card select {
  min-height: 2.25rem;
  padding: 0.35rem 0.5rem;
  border: 1px solid #bfc6cd;
  border-radius: 4px;
  background: #fff;
  font: inherit;
}

.datasets__file-name,
.datasets__transfer-state {
  overflow-wrap: anywhere;
  color: #4d5965;
  line-height: 1.45;
}

.datasets__failure {
  display: grid;
  gap: 0.2rem;
  margin: 0 0 0.75rem;
  padding: 0.75rem 0.9rem;
  border-left: 3px solid var(--el-color-danger);
  background: var(--el-color-danger-light-9);
  color: var(--el-color-danger);
}

.datasets__field-errors {
  margin: 0 0 1rem;
  padding: 0.65rem 0.9rem 0.65rem 2rem;
  border: 1px solid #e6caca;
  background: #fff8f8;
  color: var(--el-color-danger);
}

.datasets__section-head {
  align-items: baseline;
  padding: 1rem 1.15rem;
  background: var(--dataset-paper);
}

.datasets__section-head p {
  margin: 0.25rem 0 0;
}

.datasets__pagination {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 0.75rem;
  padding: 0.65rem 1.15rem;
  border-top: 1px solid var(--dataset-rule);
  color: #68717c;
}

.datasets__catalog,
.datasets__members {
  margin-bottom: 1.5rem;
}

.datasets__table {
  width: 100%;
  border-collapse: collapse;
  table-layout: fixed;
}

.datasets__table th,
.datasets__table td {
  padding: 0.75rem 0.85rem;
  border-top: 1px solid var(--dataset-rule);
  text-align: left;
  vertical-align: top;
  overflow-wrap: anywhere;
}

.datasets__table th {
  font-weight: 600;
}

.datasets__table td small,
.datasets__table th small {
  display: block;
  margin-top: 0.25rem;
  color: #68717c;
  font-weight: 400;
  line-height: 1.35;
}

.datasets__table--members {
  font-size: 0.9rem;
}

.datasets__table--members th:nth-child(1) {
  width: 18%;
}

.datasets__table--members th:nth-child(2) {
  width: 15%;
}

.datasets__table--members th:nth-child(3) {
  width: 12%;
}

.datasets__table--members th:nth-child(4) {
  width: 24%;
}

.datasets__table--members th:nth-child(5) {
  width: 12%;
}

.datasets__table--members th:nth-child(6) {
  width: 13%;
}

.datasets__table--members th:nth-child(7) {
  width: 10%;
}

.datasets__annotation-editor,
.datasets__annotation-history {
  margin: 1rem 1.15rem;
  border: 1px solid var(--dataset-rule);
  border-top: 3px solid var(--dataset-teal);
  background: #fff;
}

.datasets__annotation-history {
  border-top-color: var(--dataset-amber);
}

.datasets__annotation-record {
  padding: 1rem 1.15rem;
  border-top: 1px solid var(--dataset-rule);
}

.datasets__annotation-record h3 {
  margin: 0 0 0.4rem;
  font-size: 0.95rem;
}

.datasets__annotation-record p {
  margin: 0.35rem 0;
  color: #68717c;
  line-height: 1.45;
}

.datasets__annotation-record ol,
.datasets__annotation-record ul {
  margin: 0.6rem 0;
}

.datasets__caption {
  position: absolute;
  width: 1px;
  height: 1px;
  overflow: hidden;
  clip: rect(0 0 0 0);
  white-space: nowrap;
}

.datasets__digest {
  display: block;
  max-width: 19rem;
  margin-top: 0.25rem;
  overflow-wrap: anywhere;
  font-size: 0.75rem;
  font-weight: 400;
}

.datasets__failure-code {
  color: var(--el-color-danger) !important;
  font-weight: 600 !important;
}

.datasets__empty,
.datasets__loading,
.datasets__empty-state,
.datasets__import-only {
  padding: 1rem 1.15rem;
}

.datasets__empty-state {
  border-left: 3px solid var(--dataset-amber);
  background: var(--dataset-paper);
}

.datasets__import-only {
  margin-top: 1.5rem;
}

.datasets__import-only h2 {
  margin-bottom: 0.5rem;
}

.datasets code {
  overflow-wrap: anywhere;
}

.datasets :focus-visible {
  outline: 2px solid var(--el-color-primary);
  outline-offset: 2px;
}

@media (max-width: 1100px) {
  .datasets__actions {
    grid-template-columns: 1fr;
  }

  .datasets__header,
  .datasets__section-head {
    flex-direction: column;
  }
}
</style>
