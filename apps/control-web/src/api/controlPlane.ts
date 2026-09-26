/**
 * 面向应用的生成 OpenAPI SDK 适配器。
 *
 * 路径、方法、请求体、响应体和错误形状都来自 `src/api/generated/`。本文件只承载
 * OpenAPI 文档无法表达的浏览器策略：同源 Cookie、CSRF 双提交请求头，以及 §5.15 要求的
 * 面向操作员的未知错误降级。
 */

import { client } from '@/api/generated/client.gen'
import {
  bindTemplateVersion as generatedBindTemplateVersion,
  createAnnotationContext as generatedCreateAnnotationContext,
  downloadDatasetArtifact as generatedDownloadDatasetArtifact,
  createConnector as generatedCreateConnector,
  editCamera as generatedEditCamera,
  editInferenceHost as generatedEditInferenceHost,
  exportInferenceHostMediaConfiguration as generatedExportInferenceHostMediaConfiguration,
  listCameraMedia as generatedListCameraMedia,
  readCameraMedia as generatedReadCameraMedia,
  createPoint as generatedCreatePoint,
  createRole as generatedCreateRole,
  createTrainingDataset as generatedCreateTrainingDataset,
  createUser as generatedCreateUser,
  deleteConnector as generatedDeleteConnector,
  downloadTemplateImport as generatedDownloadTemplateImport,
  downloadTemplateVersionArtifact as generatedDownloadTemplateVersionArtifact,
  deletePoint as generatedDeletePoint,
  deleteRole as generatedDeleteRole,
  deleteUser as generatedDeleteUser,
  editConnector as generatedEditConnector,
  editPoint as generatedEditPoint,
  editRole as generatedEditRole,
  editTemplateDraft as generatedEditTemplateDraft,
  editUser as generatedEditUser,
  endSession as generatedEndSession,
  confirmVideoUpload as generatedConfirmVideoUpload,
  enqueueConnectorConnectionTest as generatedEnqueueConnectorConnectionTest,
  importTemplateDraft as generatedImportTemplateDraft,
  listAnnotations as generatedListAnnotations,
  listDatasetActionListVersions as generatedListDatasetActionListVersions,
  listDatasetArtifacts as generatedListDatasetArtifacts,
  listDatasetUsageChecks as generatedListDatasetUsageChecks,
  listVlmCandidates as generatedListVlmCandidates,
  listConnectors as generatedListConnectors,
  listDatasetMembers as generatedListDatasetMembers,
  listInferenceHosts as generatedListInferenceHosts,
  listPermissions as generatedListPermissions,
  listPoints as generatedListPoints,
  listRoles as generatedListRoles,
  listStations as generatedListStations,
  listTemplateDrafts as generatedListTemplateDrafts,
  listTemplateImports as generatedListTemplateImports,
  listTemplateVersions as generatedListTemplateVersions,
  listTrainingDatasets as generatedListTrainingDatasets,
  listUsers as generatedListUsers,
  openSession as generatedOpenSession,
  publishTemplateVersion as generatedPublishTemplateVersion,
  readAnnotation as generatedReadAnnotation,
  readAnnotationContext as generatedReadAnnotationContext,
  readConnector as generatedReadConnector,
  readDatasetMember as generatedReadDatasetMember,
  readDeviceCommand as generatedReadDeviceCommand,
  readJob as generatedReadJob,
  readPoint as generatedReadPoint,
  readSession as generatedReadSession,
  readStationTemplateConfiguration as generatedReadStationTemplateConfiguration,
  readTrainingDataset as generatedReadTrainingDataset,
  readDatasetArtifact as generatedReadDatasetArtifact,
  readDatasetUsageCheck as generatedReadDatasetUsageCheck,
  readTemplateDraft as generatedReadTemplateDraft,
  readVlmCandidate as generatedReadVlmCandidate,
  readTemplateImport as generatedReadTemplateImport,
  readTemplateVersion as generatedReadTemplateVersion,
  registerDatasetActionList as generatedRegisterDatasetActionList,
  registerVlmCandidate as generatedRegisterVlmCandidate,
  requestDatasetArtifact as generatedRequestDatasetArtifact,
  requestDatasetUsageCheck as generatedRequestDatasetUsageCheck,
  requestVideoUpload as generatedRequestVideoUpload,
  resetUserPassword as generatedResetUserPassword,
  retryAnnotation as generatedRetryAnnotation,
  retryVideoUpload as generatedRetryVideoUpload,
  setConnectorStatus as generatedSetConnectorStatus,
  setPointStatus as generatedSetPointStatus,
  setUserRoles as generatedSetUserRoles,
  setUserStatus as generatedSetUserStatus,
  submitAnnotation as generatedSubmitAnnotation,
  updateConnectorCapability as generatedUpdateConnectorCapability,
  updateStationRuntimeParameters as generatedUpdateStationRuntimeParameters,
  validatePointBinding as generatedValidatePointBinding,
  validateTemplateBinding as generatedValidateTemplateBinding,
  type ActionListHistoryView,
  type CameraConfiguration,
  type CameraMediaView,
  type CameraView,
  type ActionListInput,
  type ArtifactAcceptedView,
  type ArtifactInput,
  type ArtifactView,
  type ActionListView,
  type AnnotationAcceptedView,
  type AnnotationContextInput,
  type AnnotationContextView,
  type AnnotationHistoryView,
  type AnnotationMode,
  type AnnotationSegmentInput as GeneratedAnnotationSegmentInput,
  type AnnotationSubmissionInput,
  type AnnotationSubmissionView,
  type BindingValidationRequest,
  type BindingValidationView,
  type RuntimeParametersUpdateInput,
  type StationTemplateConfigurationView,
  type TemplateBindingInput,
  type TemplateBindingPreviewView,
  type ConfirmationView,
  type ConnectorPlacement,
  type ConnectorView,
  type CreateRoleData,
  type CreateTrainingDatasetData,
  type CreateUserData,
  type DatasetMemberView,
  type DatasetView,
  type DeviceStatus,
  type DownloadTemplateImportResponse,
  type DownloadTemplateVersionArtifactResponse,
  type EditRoleData,
  type EditUserData,
  type FactorySopJobAdaptersRoutesJobView,
  type ItemPageArtifactView,
  type ItemPageCameraMediaView,
  type ItemPageConnectorView,
  type ItemPageDatasetMemberView,
  type ItemPageDatasetView,
  type ItemPageInferenceHostView,
  type ItemPagePointView,
  type ItemPageUsageCheckView,
  type ItemPageVlmCandidateView,
  type ItemPageRoleView,
  type ItemPageStationView,
  type ItemPageStr,
  type ItemPageTemplateDraftView,
  type ItemPageTemplateImportView,
  type ItemPageTemplateVersionView,
  type ItemPageUserView,
  type ImportTemplateDraftData,
  type ListPointsData,
  type HostConfiguration,
  type HostMediaConfigurationView,
  type InferenceHostView,
  type OpenSessionData,
  type PendingCommandView,
  type PointConfiguration,
  type PointView,
  type ProblemDocument,
  type RegisterVlmCandidateInput,
  type RequestVideoUploadData,
  type RetryMode,
  type RetryView,
  type ResetUserPasswordData,
  type RoleView,
  type SessionView,
  type SetUserRolesData,
  type UpdateConnectorCapabilityData,
  type StatusChanged,
  type TemplateArtifactName,
  type TemplateDraftConfiguration,
  type TemplateDraftView,
  type TemplateImportResultView,
  type TemplateImportView,
  type TemplateVersionView,
  type UploadInstructionsView,
  type UploadRequestView,
  type UsageCheckAcceptedView,
  type UsageCheckInput,
  type UsageCheckView,
  type VlmCandidateView,
  type UserStatus,
  type UserView,
} from '@/api/generated'

export type {
  ActionListHistoryView,
  ActionListInput,
  CameraConfiguration,
  CameraMediaView,
  ActionListView,
  ArtifactAcceptedView,
  ArtifactInput,
  ArtifactView,
  AnnotationAcceptedView,
  AnnotationContextInput,
  AnnotationContextView,
  AnnotationHistoryView,
  AnnotationMode,
  AnnotationSubmissionInput,
  AnnotationSubmissionView,
  BackendConfigurationStatusView,
  BindingValidationRequest,
  BindingValidationView,
  ConfirmationView,
  ConnectorPlacement,
  HostConfiguration,
  HostMediaConfigurationView,
  ConnectorView,
  DatasetMemberView,
  DatasetView,
  DeviceStatus,
  DownloadTemplateVersionArtifactResponse,
  InferenceHostView,
  ItemPageCameraMediaView,
  ItemPageConnectorView,
  ItemPageDatasetMemberView,
  ItemPageDatasetView,
  ItemPageInferenceHostView,
  ItemPagePointView,
  ItemPageStationView,
  PendingCommandView,
  PointConfiguration,
  PointView,
  RegisterVlmCandidateInput,
  RetryMode,
  RetryView,
  RoleView,
  RuntimeParameterMode,
  RuntimeParametersInput,
  RuntimeParametersUpdateInput,
  RuntimeParametersView,
  SessionView,
  StationTemplateConfigurationView,
  StationView,
  StatusChanged,
  TemplateArtifactName,
  TemplateBindingInput,
  TemplateBindingPreviewView,
  TemplateDraftConfiguration,
  TemplateDraftView,
  TemplateImportResultView,
  TemplateImportView,
  TemplateVersionView,
  ItemPageTemplateVersionView,
  UploadInstructionsView,
  UploadRequestView,
  UsageCheckAcceptedView,
  UsageCheckInput,
  UsageCheckView,
  VlmCandidateView,
  UserView,
} from '@/api/generated'

export type AnnotationSegmentInput = GeneratedAnnotationSegmentInput

const CSRF_COOKIE = 'sop_csrf'
/** 双提交 CSRF 头名；流式上传等非生成客户端复用同一常量。 */
export const CSRF_HEADER = 'x-csrf-token'
const MODIFYING_METHODS = new Set(['POST', 'PUT', 'PATCH', 'DELETE'])
const GENERIC_MESSAGE = '请求未能完成，请稍后重试'

/** 一项被拒绝的输入，便于表单把消息放到对应控件旁。 */
export interface FieldError {
  field: string
  message: string
}

export class ControlPlaneError extends Error {
  readonly errorCode: string
  readonly status: number
  readonly fieldErrors: FieldError[]
  /** 后端组合出的具体原因，例如登录名已占用或权限会导致系统失去管理员。
   * `message` 是可展示的标题，`detail` 是标题下的说明；任一项可能缺失，页面显示
   * `detail ?? message`。 */
  readonly detail: string | null

  constructor(options: {
    message: string
    errorCode: string
    status: number
    fieldErrors?: FieldError[]
    detail?: string | null
    cause?: unknown
  }) {
    super(options.message, options.cause === undefined ? undefined : { cause: options.cause })
    this.name = 'ControlPlaneError'
    this.errorCode = options.errorCode
    this.status = options.status
    this.fieldErrors = options.fieldErrors ?? []
    this.detail = options.detail ?? null
  }
}

/**
 * 由会话 store 注册的钩子：后端返回 401 时调用，统一清除已被撤销的身份缓存，避免每个
 * 页面各自处理。由 store 注册而不是在此处直接导入，以免形成循环依赖。
 */
let unauthorizedHandler: (() => void) | null = null

export function setUnauthorizedHandler(handler: (() => void) | null): void {
  unauthorizedHandler = handler
}

client.setConfig({ baseUrl: window.location.origin, credentials: 'same-origin' })
client.interceptors.request.use((request) => {
  if (request.method === 'GET' || request.method === 'HEAD') {
    return new Request(request, { cache: 'no-store' })
  }
  if (!MODIFYING_METHODS.has(request.method)) {
    return request
  }
  const token = csrfToken()
  if (token === null) {
    return request
  }
  const headers = new Headers(request.headers)
  headers.set(CSRF_HEADER, token)
  return new Request(request, { headers })
})

/** 从会话 Cookie 读出双提交 CSRF 令牌；没有会话时返回 null。 */
export function csrfToken(): string | null {
  const match = document.cookie.split('; ').find((entry) => entry.startsWith(`${CSRF_COOKIE}=`))
  return match ? decodeURIComponent(match.slice(CSRF_COOKIE.length + 1)) : null
}

interface GeneratedResult<T> {
  data?: T
  error?: unknown
  response?: Response
}

export interface OverviewSection {
  status: string
  data: Record<string, unknown>
  detail?: string | null
}

export interface OverviewDocument {
  device: OverviewSection
  template: OverviewSection
  dataset: OverviewSection
  monitor: OverviewSection
}

export async function readOverview(): Promise<OverviewDocument> {
  let response: Response
  try {
    response = await fetch('/api/v1/overview', {
      credentials: 'same-origin',
      headers: { Accept: 'application/json' },
      cache: 'no-store',
    })
  } catch (error) {
    throw controlPlaneError(error, undefined)
  }
  let body: unknown = null
  try {
    body = await response.json()
  } catch {
    body = null
  }
  if (!response.ok) {
    throw controlPlaneError(body, response)
  }
  if (typeof body !== 'object' || body === null) {
    throw new ControlPlaneError({
      message: GENERIC_MESSAGE,
      errorCode: 'UNKNOWN',
      status: response.status,
    })
  }
  return parseOverview(body)
}

function parseOverview(value: object): OverviewDocument {
  const record = value as Record<string, unknown>
  return {
    device: parseOverviewSection(record.device),
    template: parseOverviewSection(record.template),
    dataset: parseOverviewSection(record.dataset),
    monitor: parseOverviewSection(record.monitor),
  }
}

function parseOverviewSection(value: unknown): OverviewSection {
  if (typeof value !== 'object' || value === null) {
    return {
      status: 'unavailable',
      data: {},
      detail: '概览模块响应格式不可用',
    }
  }
  const record = value as Record<string, unknown>
  return {
    status: typeof record.status === 'string' && record.status ? record.status : 'unknown',
    data:
      typeof record.data === 'object' && record.data !== null
        ? (record.data as Record<string, unknown>)
        : {},
    detail: typeof record.detail === 'string' ? record.detail : null,
  }
}

async function execute<T>(request: Promise<GeneratedResult<T>>): Promise<T> {
  const result = await request
  if (result.error !== undefined) {
    const error = controlPlaneError(result.error, result.response)
    // 登录后收到 401 表示身份已被撤销，而不是表单填写错误；钩子会让会话 store 统一清除身份。
    if (error.status === 401) {
      unauthorizedHandler?.()
    }
    throw error
  }
  if (result.response?.status === 204) {
    return undefined as T
  }
  if (result.data === undefined) {
    throw new ControlPlaneError({
      message: GENERIC_MESSAGE,
      errorCode: 'UNKNOWN',
      status: result.response?.status ?? 0,
    })
  }
  return result.data
}

function controlPlaneError(error: unknown, response: Response | undefined): ControlPlaneError {
  if (response === undefined) {
    return new ControlPlaneError({
      message: '无法连接服务器，请检查网络后重试',
      errorCode: 'NETWORK_UNREACHABLE',
      status: 0,
      cause: error,
    })
  }
  const candidate =
    typeof error === 'object' && error !== null ? (error as Partial<ProblemDocument>) : null
  const problem =
    candidate !== null &&
    typeof candidate.title === 'string' &&
    typeof candidate.error_code === 'string'
      ? (candidate as ProblemDocument)
      : null
  return new ControlPlaneError({
    message: problem?.title ?? GENERIC_MESSAGE,
    errorCode: problem?.error_code ?? 'UNKNOWN',
    status: response.status,
    fieldErrors: problem?.field_errors ?? [],
    detail: problem?.detail ?? null,
  })
}

export function openSession(credentials: OpenSessionData['body']): Promise<SessionView> {
  return execute(generatedOpenSession({ body: credentials }))
}

export function readSession(): Promise<SessionView> {
  return execute(generatedReadSession())
}

export function endSession(): Promise<void> {
  return execute(generatedEndSession())
}

export type TrainingDataset = DatasetView
export type TrainingDatasetMember = DatasetMemberView
export type TrainingDatasetPage = ItemPageDatasetView
export type TrainingDatasetMemberPage = ItemPageDatasetMemberView
export type DatasetUploadInstructions = UploadInstructionsView
export type DatasetUploadRequest = UploadRequestView
export type DatasetConfirmation = ConfirmationView
export type DatasetRetry = RetryView
export type DatasetJob = FactorySopJobAdaptersRoutesJobView
export type DatasetCreateInput = CreateTrainingDatasetData['body']
export type DatasetUploadInput = RequestVideoUploadData['body']

export function readTrainingDatasets(page = 1, pageSize = 50): Promise<TrainingDatasetPage> {
  return execute(generatedListTrainingDatasets({ query: { page, page_size: pageSize } }))
}

export function createTrainingDataset(name: DatasetCreateInput['name']): Promise<TrainingDataset> {
  return execute(generatedCreateTrainingDataset({ body: { name } }))
}

export function readTrainingDataset(datasetId: string): Promise<TrainingDataset> {
  return execute(generatedReadTrainingDataset({ path: { dataset_id: datasetId } }))
}

export function readDatasetMembers(
  datasetId: string,
  page = 1,
  pageSize = 50,
): Promise<TrainingDatasetMemberPage> {
  return execute(
    generatedListDatasetMembers({
      path: { dataset_id: datasetId },
      query: { page, page_size: pageSize },
    }),
  )
}

export function readDatasetMember(
  datasetId: string,
  memberId: string,
): Promise<TrainingDatasetMember> {
  return execute(
    generatedReadDatasetMember({ path: { dataset_id: datasetId, member_id: memberId } }),
  )
}

export function requestVideoUpload(
  datasetId: string,
  submitted: DatasetUploadInput,
  idempotencyKey?: string,
): Promise<DatasetUploadRequest> {
  return execute(
    generatedRequestVideoUpload({
      path: { dataset_id: datasetId },
      body: submitted,
      headers: idempotencyKey === undefined ? undefined : { 'Idempotency-Key': idempotencyKey },
    }),
  )
}

export function confirmVideoUpload(
  datasetId: string,
  memberId: string,
  attemptId: string,
): Promise<DatasetConfirmation> {
  return execute(
    generatedConfirmVideoUpload({
      path: { dataset_id: datasetId, member_id: memberId },
      body: { attempt_id: attemptId },
    }),
  )
}

export function retryVideoUpload(
  datasetId: string,
  memberId: string,
  mode: RetryMode,
  idempotencyKey?: string,
): Promise<DatasetRetry> {
  return execute(
    generatedRetryVideoUpload({
      path: { dataset_id: datasetId, member_id: memberId },
      body: { mode },
      headers: idempotencyKey === undefined ? undefined : { 'Idempotency-Key': idempotencyKey },
    }),
  )
}

export function readJob(jobId: string): Promise<DatasetJob> {
  return execute(generatedReadJob({ path: { job_id: jobId } }))
}

export type DatasetAnnotationMode = AnnotationMode
export type DatasetActionList = ActionListView
export type DatasetActionListHistory = ActionListHistoryView
export type DatasetActionListInput = ActionListInput

export function registerDatasetActionList(
  datasetId: string,
  actions: string[],
): Promise<DatasetActionList> {
  return execute(
    generatedRegisterDatasetActionList({ path: { dataset_id: datasetId }, body: { actions } }),
  )
}

export function listDatasetActionListVersions(
  datasetId: string,
): Promise<DatasetActionListHistory> {
  return execute(generatedListDatasetActionListVersions({ path: { dataset_id: datasetId } }))
}

export function createAnnotationContext(
  datasetId: string,
  memberId: string,
  submitted: AnnotationContextInput = {},
): Promise<AnnotationContextView> {
  return execute(
    generatedCreateAnnotationContext({
      path: { dataset_id: datasetId, member_id: memberId },
      body: submitted,
    }),
  )
}

export function readAnnotationContext(contextToken: string): Promise<AnnotationContextView> {
  return execute(generatedReadAnnotationContext({ path: { context_token: contextToken } }))
}

export function submitAnnotation(
  datasetId: string,
  memberId: string,
  submitted: AnnotationSubmissionInput,
  idempotencyKey: string,
  annotationRevision: number,
): Promise<AnnotationAcceptedView> {
  return execute(
    generatedSubmitAnnotation({
      path: { dataset_id: datasetId, member_id: memberId },
      headers: { 'Idempotency-Key': idempotencyKey, 'If-Match': annotationRevision },
      body: submitted,
    }),
  )
}

export function listAnnotations(
  datasetId: string,
  memberId: string,
): Promise<AnnotationHistoryView> {
  return execute(generatedListAnnotations({ path: { dataset_id: datasetId, member_id: memberId } }))
}

export function readAnnotation(
  datasetId: string,
  memberId: string,
  submissionId: string,
): Promise<AnnotationSubmissionView> {
  return execute(
    generatedReadAnnotation({
      path: { dataset_id: datasetId, member_id: memberId, submission_id: submissionId },
    }),
  )
}

export function retryAnnotation(
  datasetId: string,
  memberId: string,
  submissionId: string,
): Promise<AnnotationAcceptedView> {
  return execute(
    generatedRetryAnnotation({
      path: { dataset_id: datasetId, member_id: memberId, submission_id: submissionId },
    }),
  )
}

export type DatasetVlmCandidate = VlmCandidateView
export type DatasetUsageCheck = UsageCheckView
export type DatasetArtifact = ArtifactView

export function registerVlmCandidate(
  datasetId: string,
  submitted: RegisterVlmCandidateInput,
  revision: number,
): Promise<DatasetVlmCandidate> {
  return execute(
    generatedRegisterVlmCandidate({
      path: { dataset_id: datasetId },
      headers: { 'If-Match': revision },
      body: submitted,
    }),
  )
}

export function listVlmCandidates(
  datasetId: string,
  page = 1,
  pageSize = 50,
): Promise<ItemPageVlmCandidateView> {
  return execute(
    generatedListVlmCandidates({
      path: { dataset_id: datasetId },
      query: { page, page_size: pageSize },
    }),
  )
}

export function readVlmCandidate(
  datasetId: string,
  candidateId: string,
): Promise<DatasetVlmCandidate> {
  return execute(
    generatedReadVlmCandidate({ path: { dataset_id: datasetId, candidate_id: candidateId } }),
  )
}

export function requestDatasetUsageCheck(
  datasetId: string,
  submitted: UsageCheckInput,
): Promise<UsageCheckAcceptedView> {
  return execute(
    generatedRequestDatasetUsageCheck({ path: { dataset_id: datasetId }, body: submitted }),
  )
}

export function listDatasetUsageChecks(
  datasetId: string,
  page = 1,
  pageSize = 50,
): Promise<ItemPageUsageCheckView> {
  return execute(
    generatedListDatasetUsageChecks({
      path: { dataset_id: datasetId },
      query: { page, page_size: pageSize },
    }),
  )
}

export function readDatasetUsageCheck(
  datasetId: string,
  checkId: string,
): Promise<DatasetUsageCheck> {
  return execute(
    generatedReadDatasetUsageCheck({ path: { dataset_id: datasetId, check_id: checkId } }),
  )
}

export function requestDatasetArtifact(
  datasetId: string,
  submitted: ArtifactInput,
): Promise<ArtifactAcceptedView> {
  return execute(
    generatedRequestDatasetArtifact({ path: { dataset_id: datasetId }, body: submitted }),
  )
}

export function listDatasetArtifacts(
  datasetId: string,
  page = 1,
  pageSize = 50,
): Promise<ItemPageArtifactView> {
  return execute(
    generatedListDatasetArtifacts({
      path: { dataset_id: datasetId },
      query: { page, page_size: pageSize },
    }),
  )
}

export function readDatasetArtifact(
  datasetId: string,
  artifactId: string,
): Promise<DatasetArtifact> {
  return execute(
    generatedReadDatasetArtifact({ path: { dataset_id: datasetId, artifact_id: artifactId } }),
  )
}

export function downloadDatasetArtifact(datasetId: string, artifactId: string): Promise<Blob> {
  return execute(
    generatedDownloadDatasetArtifact({
      path: { dataset_id: datasetId, artifact_id: artifactId },
      parseAs: 'blob',
    }) as Promise<GeneratedResult<Blob>>,
  )
}

// ——— 模板草稿：原始导入、列表、读取和 If-Match 编辑均走生成客户端。 ———

export type TemplateImportFile = ImportTemplateDraftData['body']

export function importTemplateDraft(
  document: TemplateImportFile,
  filename: string,
): Promise<TemplateImportResultView> {
  return execute(
    generatedImportTemplateDraft({
      body: document,
      query: { filename },
      headers: {
        'Content-Type': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
      },
    }),
  )
}

export function readTemplateDrafts(): Promise<ItemPageTemplateDraftView> {
  return execute(generatedListTemplateDrafts())
}

export function readTemplateDraft(draftId: string): Promise<TemplateDraftView> {
  return execute(generatedReadTemplateDraft({ path: { draft_id: draftId } }))
}

export function editTemplateDraft(
  draftId: string,
  submitted: TemplateDraftConfiguration,
  revision: number,
): Promise<TemplateDraftView> {
  return execute(
    generatedEditTemplateDraft({
      path: { draft_id: draftId },
      headers: { 'If-Match': revision },
      body: submitted,
    }),
  )
}

export function downloadTemplateImport(importId: string): Promise<DownloadTemplateImportResponse> {
  return execute(generatedDownloadTemplateImport({ path: { import_id: importId } }))
}

export function publishTemplateVersion(
  draftId: string,
  revision: number,
): Promise<TemplateVersionView> {
  return execute(
    generatedPublishTemplateVersion({
      path: { draft_id: draftId },
      headers: { 'If-Match': revision },
    }),
  )
}

export function readTemplateVersions(): Promise<ItemPageTemplateVersionView> {
  return execute(generatedListTemplateVersions())
}

export function readTemplateVersion(versionId: string): Promise<TemplateVersionView> {
  return execute(generatedReadTemplateVersion({ path: { version_id: versionId } }))
}

export function downloadTemplateVersionArtifact(
  versionId: string,
  name: TemplateArtifactName,
): Promise<DownloadTemplateVersionArtifactResponse> {
  return execute(
    generatedDownloadTemplateVersionArtifact({
      path: { version_id: versionId, name },
      parseAs: 'blob',
    }),
  )
}

export function readTemplateImports(): Promise<ItemPageTemplateImportView> {
  return execute(generatedListTemplateImports())
}

export function readTemplateImport(importId: string): Promise<TemplateImportView> {
  return execute(generatedReadTemplateImport({ path: { import_id: importId } }))
}

// ——— 工位与设备：页面只通过生成客户端访问控制面。 ———

export function readConnectors(): Promise<ItemPageConnectorView> {
  return execute(generatedListConnectors())
}

export function readCameraMedia(page = 1, pageSize = 50): Promise<ItemPageCameraMediaView> {
  return execute(generatedListCameraMedia({ query: { page, page_size: pageSize } }))
}

export function readCameraMediaDetail(cameraId: string): Promise<CameraMediaView> {
  return execute(generatedReadCameraMedia({ path: { camera_id: cameraId } }))
}

export function readConnector(connectorId: string): Promise<ConnectorView> {
  return execute(generatedReadConnector({ path: { connector_id: connectorId } }))
}

export function readInferenceHosts(): Promise<ItemPageInferenceHostView> {
  return execute(generatedListInferenceHosts())
}

export function editCamera(
  cameraId: string,
  submitted: CameraConfiguration,
  revision: number,
): Promise<CameraView> {
  return execute(
    generatedEditCamera({
      path: { camera_id: cameraId },
      headers: { 'If-Match': revision },
      body: submitted,
    }),
  )
}

export function editInferenceHost(
  hostId: string,
  submitted: HostConfiguration,
  revision: number,
): Promise<InferenceHostView> {
  return execute(
    generatedEditInferenceHost({
      path: { host_id: hostId },
      headers: { 'If-Match': revision },
      body: submitted,
    }),
  )
}

export function exportInferenceHostMediaConfiguration(
  hostId: string,
): Promise<HostMediaConfigurationView> {
  return execute(generatedExportInferenceHostMediaConfiguration({ path: { host_id: hostId } }))
}

export function readStations(): Promise<ItemPageStationView> {
  return execute(generatedListStations())
}

export function validateTemplateBinding(
  request: TemplateBindingInput,
): Promise<TemplateBindingPreviewView> {
  return execute(generatedValidateTemplateBinding({ body: request }))
}

export function bindTemplateVersion(
  request: TemplateBindingInput,
  stationRevision: number,
): Promise<StationTemplateConfigurationView> {
  return execute(
    generatedBindTemplateVersion({
      headers: { 'If-Match': stationRevision },
      body: request,
    }),
  )
}

export function readStationTemplateConfiguration(
  stationId: string,
): Promise<StationTemplateConfigurationView> {
  return execute(generatedReadStationTemplateConfiguration({ path: { station_id: stationId } }))
}

export function updateStationRuntimeParameters(
  stationId: string,
  request: RuntimeParametersUpdateInput,
  stationRevision: number,
): Promise<StationTemplateConfigurationView> {
  return execute(
    generatedUpdateStationRuntimeParameters({
      path: { station_id: stationId },
      headers: { 'If-Match': stationRevision },
      body: request,
    }),
  )
}

export function createConnector(submitted: ConnectorPlacement): Promise<ConnectorView> {
  return execute(generatedCreateConnector({ body: submitted }))
}

export function editConnector(
  connectorId: string,
  submitted: ConnectorPlacement,
  revision: number,
): Promise<ConnectorView> {
  return execute(
    generatedEditConnector({
      path: { connector_id: connectorId },
      headers: { 'If-Match': revision },
      body: submitted,
    }),
  )
}

/** 将一次连接测试写入中心持久化命令队列；调用方必须复用同一幂等键安全重试。 */
export function enqueueConnectorConnectionTest(
  connectorId: string,
  idempotencyKey: string,
): Promise<PendingCommandView> {
  return execute(
    generatedEnqueueConnectorConnectionTest({
      path: { connector_id: connectorId },
      headers: { 'Idempotency-Key': idempotencyKey },
    }),
  )
}

/** 读取不含领取令牌的命令状态，供操作员等待真实结果或明确拒绝。 */
export function readDeviceCommand(commandId: string): Promise<PendingCommandView> {
  return execute(generatedReadDeviceCommand({ path: { command_id: commandId } }))
}

export function setConnectorStatus(
  connectorId: string,
  status: DeviceStatus,
  revision: number,
): Promise<ConnectorView> {
  return execute(
    generatedSetConnectorStatus({
      path: { connector_id: connectorId },
      headers: { 'If-Match': revision },
      body: { status },
    }),
  )
}

export function deleteConnector(connectorId: string, revision: number): Promise<void> {
  return execute(
    generatedDeleteConnector({
      path: { connector_id: connectorId },
      headers: { 'If-Match': revision },
    }),
  )
}

// ——— 点位与能力：点位列表、能力声明和绑定预检均走生成客户端。 ———

export type PointListQuery = NonNullable<ListPointsData['query']>

export type ConnectorCapability = UpdateConnectorCapabilityData['body']

export function readPoints(query: PointListQuery = {}): Promise<ItemPagePointView> {
  return execute(generatedListPoints({ query }))
}

export function readPoint(pointId: string): Promise<PointView> {
  return execute(generatedReadPoint({ path: { point_id: pointId } }))
}

export function createPoint(submitted: PointConfiguration): Promise<PointView> {
  return execute(generatedCreatePoint({ body: submitted }))
}

export function editPoint(
  pointId: string,
  submitted: PointConfiguration,
  revision: number,
): Promise<PointView> {
  return execute(
    generatedEditPoint({
      path: { point_id: pointId },
      headers: { 'If-Match': revision },
      body: submitted,
    }),
  )
}

export function setPointStatus(
  pointId: string,
  status: DeviceStatus,
  revision: number,
): Promise<PointView> {
  return execute(
    generatedSetPointStatus({
      path: { point_id: pointId },
      headers: { 'If-Match': revision },
      body: { status },
    }),
  )
}

export function deletePoint(pointId: string, revision: number): Promise<void> {
  return execute(
    generatedDeletePoint({
      path: { point_id: pointId },
      headers: { 'If-Match': revision },
    }),
  )
}

export function updateConnectorCapability(
  connectorId: string,
  capability: ConnectorCapability,
  revision: number,
): Promise<ConnectorView> {
  return execute(
    generatedUpdateConnectorCapability({
      path: { connector_id: connectorId },
      headers: { 'If-Match': revision },
      body: capability,
    }),
  )
}

export function validatePointBinding(
  request: BindingValidationRequest,
): Promise<BindingValidationView> {
  return execute(generatedValidatePointBinding({ body: request }))
}

// ——— 用户与权限（C2.2）：管理操作仅薄封装生成 SDK。 ———
// 每项操作都在 OpenAPI 元数据中声明权限；真正的强制检查位于用例，因此这里的列表只决定
// 页面可以提供什么，不决定后端允许什么。

export function readUsers(): Promise<ItemPageUserView> {
  return execute(generatedListUsers())
}

export function createUser(submitted: CreateUserData['body']): Promise<UserView> {
  return execute(generatedCreateUser({ body: submitted }))
}

export function editUser(userId: string, submitted: EditUserData['body']): Promise<UserView> {
  return execute(generatedEditUser({ path: { user_id: userId }, body: submitted }))
}

export function resetUserPassword(
  userId: string,
  password: ResetUserPasswordData['body']['password'],
): Promise<void> {
  return execute(generatedResetUserPassword({ path: { user_id: userId }, body: { password } }))
}

export function setUserStatus(userId: string, status: UserStatus): Promise<StatusChanged> {
  return execute(generatedSetUserStatus({ path: { user_id: userId }, body: { status } }))
}

export function setUserRoles(
  userId: string,
  roleIds: SetUserRolesData['body']['role_ids'],
): Promise<UserView> {
  return execute(generatedSetUserRoles({ path: { user_id: userId }, body: { role_ids: roleIds } }))
}

export function deleteUser(userId: string): Promise<void> {
  return execute(generatedDeleteUser({ path: { user_id: userId } }))
}

export function readRoles(): Promise<ItemPageRoleView> {
  return execute(generatedListRoles())
}

/** 角色可以包含的权限。渲染为角色表单的复选框，后端未注册的权限不会被提供，新增权限也会
 * 自动出现，无需修改页面。 */
export function readPermissionCatalogue(): Promise<ItemPageStr> {
  return execute(generatedListPermissions())
}

export function createRole(submitted: CreateRoleData['body']): Promise<RoleView> {
  return execute(generatedCreateRole({ body: submitted }))
}

export function editRole(roleId: string, submitted: EditRoleData['body']): Promise<RoleView> {
  return execute(generatedEditRole({ path: { role_id: roleId }, body: submitted }))
}

export function deleteRole(roleId: string): Promise<void> {
  return execute(generatedDeleteRole({ path: { role_id: roleId } }))
}
