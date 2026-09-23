import ElementPlus from 'element-plus'
import { createPinia, setActivePinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createRouter, createWebHistory } from 'vue-router'

import { ControlPlaneError } from '@/api/controlPlane'
import TrainingDatasetsView from '@/modules/datasets/TrainingDatasetsView.vue'
import { useSessionStore } from '@/session/store'

const api = vi.hoisted(() => ({
  readTrainingDatasets: vi.fn(),
  readDatasetMembers: vi.fn(),
  listDatasetActionListVersions: vi.fn(),
  listDatasetArtifacts: vi.fn(),
  listDatasetUsageChecks: vi.fn(),
  listVlmCandidates: vi.fn(),
  registerDatasetActionList: vi.fn(),
  registerVlmCandidate: vi.fn(),
  createTrainingDataset: vi.fn(),
  requestDatasetArtifact: vi.fn(),
  requestDatasetUsageCheck: vi.fn(),
  downloadDatasetArtifact: vi.fn(),
  requestVideoUpload: vi.fn(),
  confirmVideoUpload: vi.fn(),
  retryVideoUpload: vi.fn(),
  readJob: vi.fn(),
  createAnnotationContext: vi.fn(),
  readAnnotationContext: vi.fn(),
  listAnnotations: vi.fn(),
}))
const upload = vi.hoisted(() => vi.fn())

vi.mock('@/api/controlPlane', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/controlPlane')>()),
  ...api,
}))

vi.mock('@/modules/datasets/upload', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/modules/datasets/upload')>()),
  uploadVideoObject: upload,
}))

const DATASET = {
  id: 'dataset-1',
  name: '装配视频集',
  created_by: 'operator-1',
  updated_by: 'operator-1',
  created_at: '2026-09-08T01:00:00Z',
  updated_at: '2026-09-08T01:00:00Z',
}

const ATTEMPT = {
  id: 'attempt-1',
  member_id: 'member-1',
  status: 'pending_upload',
  declared_size: 11,
  declared_sha256: 'a'.repeat(64),
  expires_at: '2026-09-08T09:00:00Z',
  object_version_id: null,
}

const UPLOAD = {
  method: 'POST',
  url: 'https://minio.example.test/factory-sop',
  fields: { key: 'training-datasets/dataset-1/member-1/attempt-1/video', policy: 'signed' },
  headers: {},
  expires_at: '2026-09-08T09:00:00Z',
  max_bytes: 1000,
  object_key: 'training-datasets/dataset-1/member-1/attempt-1/video',
}

const MEMBER_REGISTERED = {
  id: 'member-1',
  dataset_id: DATASET.id,
  original_filename: 'line-1.mp4',
  source: 'camera-A12',
  declared_size: 11,
  declared_sha256: 'a'.repeat(64),
  current_attempt_id: ATTEMPT.id,
  status: 'registered',
  actual_size: 11,
  actual_sha256: 'a'.repeat(64),
  duration_seconds: 2.5,
  codec: 'h264',
  container: 'mov,mp4,m4a,3gp,3g2,mj2',
  validation_job_id: null,
  failure_code: null,
  failure_detail: null,
  recovery_action: null,
  created_by: 'operator-1',
  updated_by: 'operator-1',
  created_at: '2026-09-08T01:00:00Z',
  updated_at: '2026-09-08T01:01:00Z',
}

const MEMBER_PENDING = {
  ...MEMBER_REGISTERED,
  status: 'pending_validation',
  actual_size: null,
  actual_sha256: null,
  duration_seconds: null,
  codec: null,
  container: null,
  validation_job_id: 'job-1',
}

const MEMBER_FAILED_VALIDATION = {
  ...MEMBER_REGISTERED,
  status: 'failed',
  actual_size: 11,
  actual_sha256: 'a'.repeat(64),
  failure_code: 'MEDIA_PROBE_UNAVAILABLE',
  failure_detail: 'ffprobe 暂时不可用',
  recovery_action: 'retry_validation',
  validation_job_id: 'job-1',
}

const ANNOTATION_CONTEXT = {
  context_token: 'signed-context',
  dataset_id: DATASET.id,
  member_id: 'member-1',
  action_list_revision: 1,
  annotation_revision: 0,
  source_object_version_id: 'object-version-1',
  source_sha256: 'a'.repeat(64),
  derived_video_size: 11,
  derived_video_sha256: 'b'.repeat(64),
  derived_video_duration_seconds: 2.5,
  preparation_job_id: 'preparation-job-1',
  preparation_status: 'succeeded',
  preparation_failure_code: null,
  preparation_failure_detail: null,
  original_filename: 'line-1.mp4',
  source: 'camera-A12',
  duration_seconds: 2.5,
  actions: ['(1) 取料'],
  video_url: '/annotation/media/videos/signed-context/download',
  initial_timestamps: [],
  two_operator_mode: false,
  expires_at: '2026-09-08T09:00:00Z',
  latest_submission: null,
}

const USAGE_CHECK = {
  id: 'usage-check-1',
  dataset_id: DATASET.id,
  kind: 'ddm',
  status: 'passed',
  input_digest: 'c'.repeat(64),
  input_snapshot: { videos: [] },
  summary: { video_count: 1, segment_count: 2 },
  issues: [],
  base_commit: 'base-commit',
  contract_version: 'ddm-v1',
  candidate_id: null,
  job_id: 'usage-job-1',
  created_by: 'operator-1',
  created_at: '2026-09-08T01:00:00Z',
  updated_at: '2026-09-08T01:01:00Z',
}

const USAGE_ARTIFACT = {
  id: 'artifact-1',
  dataset_id: DATASET.id,
  usage_check_id: USAGE_CHECK.id,
  kind: 'ddm',
  status: 'available',
  input_digest: USAGE_CHECK.input_digest,
  object_key: 'training-datasets/artifact-1/annotation.json',
  artifact_sha256: 'd'.repeat(64),
  artifact_size: 128,
  manifest: { artifact_format_version: 1 },
  failure_code: null,
  failure_detail: null,
  retryable: false,
  recovery_action: null,
  job_id: 'artifact-job-1',
  created_by: 'operator-1',
  created_at: '2026-09-08T01:00:00Z',
  updated_at: '2026-09-08T01:01:00Z',
}

const MEMBER_FAILED_UPLOAD = {
  ...MEMBER_REGISTERED,
  status: 'failed',
  actual_size: null,
  actual_sha256: null,
  duration_seconds: null,
  codec: null,
  container: null,
  failure_code: 'OBJECT_NOT_FOUND',
  failure_detail: '对象不存在',
  recovery_action: 'retry_upload',
  validation_job_id: null,
}

function grant(...permissions: string[]): void {
  useSessionStore().current = {
    user_id: 'operator-1',
    login_name: 'operator',
    display_name: '数据管理员',
    expires_at: '2026-09-08T13:00:00Z',
    permissions,
  }
}

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason?: unknown) => void
  const promise = new Promise<T>((next, fail) => {
    resolve = next
    reject = fail
  })
  return { promise, resolve, reject }
}

async function chooseFile(wrapper: ReturnType<typeof mount>, file: File): Promise<void> {
  const input = wrapper.find('input[name="dataset-video"]')
  Object.defineProperty(input.element, 'files', { configurable: true, value: [file] })
  await input.trigger('change')
}

async function mountDatasets() {
  const router = createRouter({
    history: createWebHistory(),
    routes: [{ path: '/training-datasets', name: 'datasets', component: { template: '<div />' } }],
  })
  await router.push('/training-datasets')
  await router.isReady()
  const wrapper = mount(TrainingDatasetsView, {
    global: { plugins: [ElementPlus, router] },
  })
  return { wrapper, router }
}

beforeEach(() => {
  setActivePinia(createPinia())
  window.sessionStorage.clear()
  vi.clearAllMocks()
  api.readTrainingDatasets.mockResolvedValue({ items: [DATASET], page: 1, page_size: 50, total: 1 })
  api.readDatasetMembers.mockResolvedValue({
    items: [MEMBER_REGISTERED],
    page: 1,
    page_size: 50,
    total: 1,
  })
  api.createTrainingDataset.mockResolvedValue(DATASET)
  api.listDatasetActionListVersions.mockResolvedValue({ items: [] })
  api.listDatasetUsageChecks.mockResolvedValue({ items: [], page: 1, page_size: 50, total: 0 })
  api.listVlmCandidates.mockResolvedValue({ items: [], page: 1, page_size: 50, total: 0 })
  api.listDatasetArtifacts.mockResolvedValue({ items: [], page: 1, page_size: 50, total: 0 })
  api.registerDatasetActionList.mockResolvedValue({
    dataset_id: DATASET.id,
    revision: 1,
    actions: ['(1) 取料'],
    created_by: 'operator-1',
    created_at: '2026-09-08T01:00:00Z',
  })
  api.createAnnotationContext.mockResolvedValue(ANNOTATION_CONTEXT)
  api.readAnnotationContext.mockResolvedValue(ANNOTATION_CONTEXT)
  api.listAnnotations.mockResolvedValue({ items: [] })
  api.readJob.mockResolvedValue({
    id: 'job-1',
    job_type: 'dataset_validation',
    status: 'running',
    member_id: 'member-1',
    attempt_id: 'attempt-1',
    failure_code: null,
    created_at: '2026-09-08T01:00:00Z',
    updated_at: '2026-09-08T01:00:00Z',
  })
  upload.mockImplementation((_instructions, _file, onProgress) =>
    Promise.resolve().then(() => onProgress(100)),
  )
  vi.stubGlobal('crypto', {
    randomUUID: () => 'idempotency-1',
    subtle: { digest: vi.fn().mockResolvedValue(new Uint8Array(32).fill(0xab).buffer) },
  })
})

afterEach(() => {
  vi.useRealTimers()
})

describe('训练数据集工作台', () => {
  it('loads real member facts for a read-only caller without rendering write controls', async () => {
    grant('dataset.dataset.view')

    const { wrapper } = await mountDatasets()
    await flushPromises()

    expect(api.readTrainingDatasets).toHaveBeenCalledOnce()
    expect(api.readDatasetMembers).toHaveBeenCalledWith(DATASET.id)
    expect(wrapper.text()).toContain('装配视频集')
    expect(wrapper.text()).toContain('line-1.mp4')
    expect(wrapper.text()).toContain('camera-A12')
    expect(wrapper.text()).toContain('已登记')
    expect(wrapper.text()).toContain('2.50 秒')
    expect(wrapper.text()).toContain('h264')
    expect(wrapper.find('form[aria-label="创建训练数据集"]').exists()).toBe(false)
    expect(wrapper.find('form[aria-label="上传训练视频"]').exists()).toBe(false)

    wrapper.unmount()
  })

  it('shows independent usage checks and starts DDM work through the generated client adapter', async () => {
    grant('dataset.dataset.view', 'dataset.dataset.edit')
    api.listDatasetUsageChecks.mockResolvedValue({
      items: [{ ...USAGE_CHECK, is_current: true }],
      page: 1,
      page_size: 50,
      total: 1,
    })
    api.listDatasetArtifacts.mockResolvedValue({
      items: [USAGE_ARTIFACT],
      page: 1,
      page_size: 50,
      total: 1,
    })
    api.requestDatasetUsageCheck.mockResolvedValue({
      check: { ...USAGE_CHECK, id: 'usage-check-2', status: 'pending' },
      job: {
        id: 'usage-job-2',
        job_type: 'dataset_usage_check',
        status: 'pending',
        dataset_id: DATASET.id,
        attempt_id: 'usage-check-2',
        failure_code: null,
        created_at: USAGE_CHECK.created_at,
        updated_at: USAGE_CHECK.updated_at,
      },
    })

    const { wrapper } = await mountDatasets()
    await flushPromises()

    expect(wrapper.text()).toContain('用途检查与制品')
    expect(wrapper.text()).toContain('DDM')
    expect(wrapper.text()).toContain('可下载')
    expect(wrapper.text()).toContain('检查时间：')
    expect(wrapper.text()).toContain(`SHA-256 ${USAGE_ARTIFACT.artifact_sha256}`)
    const runDdm = wrapper.findAll('button').find((button) => button.text() === '检查 DDM 数据')
    expect(runDdm).toBeDefined()
    await runDdm!.trigger('click')
    await flushPromises()

    expect(api.requestDatasetUsageCheck).toHaveBeenCalledWith(DATASET.id, {
      kind: 'ddm',
      candidate_id: null,
    })
    expect(api.requestDatasetArtifact).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('renders internal usage failures as maintenance work without an input-fix hint', async () => {
    grant('dataset.dataset.view')
    api.listDatasetUsageChecks.mockResolvedValue({
      items: [
        {
          ...USAGE_CHECK,
          status: 'failed',
          is_current: true,
          issues: [
            {
              code: 'USAGE_CHECK_EXECUTION_FAILED',
              detail: '用途检查执行失败',
              location: 'worker',
              retryable: false,
              recovery_action: null,
            },
          ],
        },
      ],
      page: 1,
      page_size: 50,
      total: 1,
    })

    const { wrapper } = await mountDatasets()
    await flushPromises()

    expect(wrapper.text()).toContain('需维护处理')
    expect(wrapper.text()).toContain('请联系维护者')
    expect(wrapper.text()).not.toContain('需修正输入')
    wrapper.unmount()
  })

  it('shows an artifact failure code and recovery detail instead of a blank result', async () => {
    grant('dataset.dataset.view', 'dataset.dataset.edit')
    api.listDatasetUsageChecks.mockResolvedValue({
      items: [{ ...USAGE_CHECK, is_current: true }],
      page: 1,
      page_size: 50,
      total: 1,
    })
    api.listDatasetArtifacts.mockResolvedValue({
      items: [
        {
          ...USAGE_ARTIFACT,
          status: 'failed',
          failure_code: 'STORAGE_UNAVAILABLE',
          failure_detail: '对象存储暂时不可用，请稍后重试',
        },
      ],
      page: 1,
      page_size: 50,
      total: 1,
    })

    const { wrapper } = await mountDatasets()
    await flushPromises()

    expect(wrapper.text()).toContain('生成失败')
    expect(wrapper.text()).toContain('STORAGE_UNAVAILABLE')
    expect(wrapper.text()).toContain('对象存储暂时不可用，请稍后重试')
    expect(wrapper.text()).toContain('可重试制品生成')
    wrapper.unmount()
  })

  it('renders API totals and loads later dataset and member pages on demand', async () => {
    grant('dataset.dataset.view', 'dataset.dataset.import')
    const secondDataset = { ...DATASET, id: 'dataset-2', name: '第二个数据集' }
    const secondMember = { ...MEMBER_REGISTERED, id: 'member-2', original_filename: 'line-2.mp4' }
    api.readTrainingDatasets
      .mockResolvedValueOnce({ items: [DATASET], page: 1, page_size: 50, total: 51 })
      .mockResolvedValueOnce({ items: [secondDataset], page: 2, page_size: 50, total: 51 })
    api.readDatasetMembers
      .mockResolvedValueOnce({ items: [MEMBER_REGISTERED], page: 1, page_size: 50, total: 51 })
      .mockResolvedValueOnce({ items: [secondMember], page: 2, page_size: 50, total: 51 })

    const { wrapper } = await mountDatasets()
    await flushPromises()

    expect(wrapper.text()).toContain('共 51 个数据集')
    expect(wrapper.text()).toContain('共 51 个视频')
    await wrapper.findAll('nav[aria-label="训练数据集分页"] button')[1]!.trigger('click')
    await flushPromises()
    expect(api.readTrainingDatasets).toHaveBeenLastCalledWith(2, 50)
    expect(wrapper.text()).toContain('第二个数据集')

    await wrapper.findAll('nav[aria-label="视频成员分页"] button')[1]!.trigger('click')
    await flushPromises()
    expect(api.readDatasetMembers).toHaveBeenLastCalledWith(DATASET.id, 2, 50)
    expect(wrapper.text()).toContain('line-2.mp4')

    wrapper.unmount()
  })

  it('keeps the newest dataset member request when an older response arrives last', async () => {
    grant('dataset.dataset.view')
    const secondDataset = { ...DATASET, id: 'dataset-2', name: '第二个数据集' }
    const secondMember = {
      ...MEMBER_REGISTERED,
      id: 'member-2',
      dataset_id: secondDataset.id,
      original_filename: 'line-2.mp4',
    }
    api.readTrainingDatasets.mockResolvedValue({
      items: [DATASET, secondDataset],
      page: 1,
      page_size: 50,
      total: 2,
    })

    const { wrapper } = await mountDatasets()
    await flushPromises()

    const secondMemberPage = deferred<{
      items: Array<typeof MEMBER_REGISTERED>
      page: number
      page_size: number
      total: number
    }>()
    api.readDatasetMembers.mockReset()
    api.readDatasetMembers
      .mockImplementationOnce(() => secondMemberPage.promise)
      .mockResolvedValueOnce({ items: [MEMBER_REGISTERED], page: 1, page_size: 50, total: 1 })

    const chooseSecond = wrapper.findAll('button').find((button) => button.text() === '查看成员')
    expect(chooseSecond).toBeDefined()
    await chooseSecond!.trigger('click')
    await flushPromises()

    const chooseFirst = wrapper.findAll('button').find((button) => button.text() === '查看成员')
    expect(chooseFirst).toBeDefined()
    await chooseFirst!.trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('line-1.mp4')

    secondMemberPage.resolve({ items: [secondMember], page: 1, page_size: 50, total: 1 })
    await flushPromises()

    expect(wrapper.text()).toContain('line-1.mp4')
    expect(wrapper.text()).not.toContain('line-2.mp4')
    wrapper.unmount()
  })

  it('keeps loading and error state owned by the newest member request', async () => {
    grant('dataset.dataset.view')
    const secondDataset = { ...DATASET, id: 'dataset-2', name: '第二个数据集' }
    api.readTrainingDatasets.mockResolvedValue({
      items: [DATASET, secondDataset],
      page: 1,
      page_size: 50,
      total: 2,
    })

    const { wrapper } = await mountDatasets()
    await flushPromises()

    const staleMemberPage = deferred<{
      items: Array<typeof MEMBER_REGISTERED>
      page: number
      page_size: number
      total: number
    }>()
    const currentMemberPage = deferred<{
      items: Array<typeof MEMBER_REGISTERED>
      page: number
      page_size: number
      total: number
    }>()
    api.readDatasetMembers.mockReset()
    api.readDatasetMembers
      .mockImplementationOnce(() => staleMemberPage.promise)
      .mockImplementationOnce(() => currentMemberPage.promise)

    const chooseSecond = wrapper.findAll('button').find((button) => button.text() === '查看成员')
    expect(chooseSecond).toBeDefined()
    await chooseSecond!.trigger('click')
    await flushPromises()

    const chooseFirst = wrapper.findAll('button').find((button) => button.text() === '查看成员')
    expect(chooseFirst).toBeDefined()
    await chooseFirst!.trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('正在加载视频状态…')

    staleMemberPage.reject(new Error('旧成员请求失败'))
    await flushPromises()

    expect(wrapper.text()).toContain('正在加载视频状态…')
    expect(wrapper.text()).not.toContain('旧成员请求失败')

    currentMemberPage.resolve({ items: [MEMBER_REGISTERED], page: 1, page_size: 50, total: 1 })
    await flushPromises()
    expect(wrapper.text()).toContain('line-1.mp4')
    expect(wrapper.text()).not.toContain('旧成员请求失败')
    wrapper.unmount()
  })

  it('keeps every usage pagination request bound to the dataset that started the load', async () => {
    grant('dataset.dataset.view')
    const secondDataset = { ...DATASET, id: 'dataset-2', name: '第二个数据集' }
    api.readTrainingDatasets.mockResolvedValue({
      items: [DATASET, secondDataset],
      page: 1,
      page_size: 50,
      total: 2,
    })

    const { wrapper } = await mountDatasets()
    await flushPromises()

    const secondUsagePage = deferred<{
      items: Array<typeof USAGE_CHECK>
      page: number
      page_size: number
      total: number
    }>()
    const secondCandidatePage = deferred<{
      items: []
      page: number
      page_size: number
      total: number
    }>()
    const secondArtifactPage = deferred<{
      items: []
      page: number
      page_size: number
      total: number
    }>()
    api.listDatasetUsageChecks.mockReset()
    api.listDatasetUsageChecks.mockImplementation((datasetId: string, page = 1, pageSize = 50) => {
      if (datasetId === secondDataset.id && page === 1) return secondUsagePage.promise
      return Promise.resolve({ items: [], page, page_size: pageSize, total: 0 })
    })
    api.listVlmCandidates.mockReset()
    api.listVlmCandidates.mockImplementation((datasetId: string, page = 1, pageSize = 50) => {
      if (datasetId === secondDataset.id && page === 1) return secondCandidatePage.promise
      return Promise.resolve({ items: [], page, page_size: pageSize, total: 0 })
    })
    api.listDatasetArtifacts.mockReset()
    api.listDatasetArtifacts.mockImplementation((datasetId: string, page = 1, pageSize = 50) => {
      if (datasetId === secondDataset.id && page === 1) return secondArtifactPage.promise
      return Promise.resolve({ items: [], page, page_size: pageSize, total: 0 })
    })

    const chooseSecond = wrapper.findAll('button').find((button) => button.text() === '查看成员')
    expect(chooseSecond).toBeDefined()
    await chooseSecond!.trigger('click')
    await flushPromises()

    const chooseFirst = wrapper.findAll('button').find((button) => button.text() === '查看成员')
    expect(chooseFirst).toBeDefined()
    await chooseFirst!.trigger('click')
    await flushPromises()

    secondUsagePage.resolve({
      items: [{ ...USAGE_CHECK, dataset_id: secondDataset.id }],
      page: 1,
      page_size: 50,
      total: 51,
    })
    secondCandidatePage.resolve({ items: [], page: 1, page_size: 50, total: 51 })
    secondArtifactPage.resolve({ items: [], page: 1, page_size: 50, total: 51 })
    await flushPromises()

    expect(api.listDatasetUsageChecks).toHaveBeenCalledWith(secondDataset.id, 2, 50)
    expect(api.listVlmCandidates).toHaveBeenCalledWith(secondDataset.id, 2, 50)
    expect(api.listDatasetArtifacts).toHaveBeenCalledWith(secondDataset.id, 2, 50)
    wrapper.unmount()
  })

  it('ignores an in-flight annotation response after the selected dataset changes', async () => {
    grant('dataset.dataset.view', 'dataset.dataset.edit')
    const secondDataset = { ...DATASET, id: 'dataset-2', name: '第二个数据集' }
    const staleAnnotationContext = deferred<typeof ANNOTATION_CONTEXT>()
    api.readTrainingDatasets.mockResolvedValue({
      items: [DATASET, secondDataset],
      page: 1,
      page_size: 50,
      total: 2,
    })
    api.createAnnotationContext.mockResolvedValue({
      ...ANNOTATION_CONTEXT,
      preparation_status: 'pending',
    })
    api.readAnnotationContext.mockImplementationOnce(() => staleAnnotationContext.promise)

    const { wrapper } = await mountDatasets()
    await flushPromises()
    vi.useFakeTimers()

    const enterAnnotation = wrapper.findAll('button').find((button) => button.text() === '进入标注')
    expect(enterAnnotation).toBeDefined()
    await enterAnnotation!.trigger('click')
    await flushPromises()
    await vi.advanceTimersByTimeAsync(1000)
    await flushPromises()
    expect(api.readAnnotationContext).toHaveBeenCalledOnce()

    const chooseSecond = wrapper.findAll('button').find((button) => button.text() === '查看成员')
    expect(chooseSecond).toBeDefined()
    await chooseSecond!.trigger('click')
    await flushPromises()

    staleAnnotationContext.resolve(ANNOTATION_CONTEXT)
    await flushPromises()
    await vi.advanceTimersByTimeAsync(1000)
    await flushPromises()

    expect(api.readAnnotationContext).toHaveBeenCalledOnce()
    expect(wrapper.find('h2#annotation-editor-heading').exists()).toBe(false)
    wrapper.unmount()
  })

  it('clears annotation loading after preparation fails so the user can retry', async () => {
    grant('dataset.dataset.view', 'dataset.dataset.edit')
    api.createAnnotationContext
      .mockRejectedValueOnce(new Error('标注准备请求失败'))
      .mockResolvedValueOnce(ANNOTATION_CONTEXT)

    const { wrapper } = await mountDatasets()
    await flushPromises()

    const enterAnnotation = wrapper.findAll('button').find((button) => button.text() === '进入标注')
    expect(enterAnnotation).toBeDefined()
    await enterAnnotation!.trigger('click')
    await flushPromises()

    expect(wrapper.text()).toContain('标注准备请求失败')
    expect(enterAnnotation!.attributes('disabled')).toBeUndefined()

    await enterAnnotation!.trigger('click')
    await flushPromises()

    expect(api.createAnnotationContext).toHaveBeenCalledTimes(2)
    expect(wrapper.find('h2#annotation-editor-heading').exists()).toBe(true)
    wrapper.unmount()
  })

  it('stops annotation preparation when the upload selector changes datasets', async () => {
    grant('dataset.dataset.view', 'dataset.dataset.edit', 'dataset.dataset.import')
    const secondDataset = { ...DATASET, id: 'dataset-2', name: '第二个数据集' }
    api.readTrainingDatasets.mockResolvedValue({
      items: [DATASET, secondDataset],
      page: 1,
      page_size: 50,
      total: 2,
    })
    api.createAnnotationContext.mockResolvedValue({
      ...ANNOTATION_CONTEXT,
      preparation_status: 'pending',
    })

    const { wrapper } = await mountDatasets()
    await flushPromises()
    vi.useFakeTimers()

    const enterAnnotation = wrapper.findAll('button').find((button) => button.text() === '进入标注')
    expect(enterAnnotation).toBeDefined()
    await enterAnnotation!.trigger('click')
    await flushPromises()

    await wrapper.find('select#dataset-select').setValue(secondDataset.id)
    await flushPromises()
    await vi.advanceTimersByTimeAsync(1000)
    await flushPromises()

    expect(api.readAnnotationContext).not.toHaveBeenCalled()
    expect(wrapper.find('h2#annotation-editor-heading').exists()).toBe(false)
    wrapper.unmount()
  })

  it('routes a newly created dataset through selection so stale member loading settles', async () => {
    grant('dataset.dataset.view', 'dataset.dataset.import')
    const createdDataset = { ...DATASET, id: 'dataset-created', name: '新建数据集' }
    const staleMembers = deferred<{
      items: (typeof MEMBER_REGISTERED)[]
      page: number
      page_size: number
      total: number
    }>()
    api.readDatasetMembers.mockImplementation((datasetId: string) => {
      if (datasetId === DATASET.id) return staleMembers.promise
      return Promise.resolve({ items: [], page: 1, page_size: 50, total: 0 })
    })
    api.createTrainingDataset.mockResolvedValue(createdDataset)

    const { wrapper } = await mountDatasets()
    await flushPromises()
    expect(wrapper.text()).toContain('正在加载视频状态')

    await wrapper.find('input[name="dataset-name"]').setValue(createdDataset.name)
    await wrapper.find('form[aria-label="创建训练数据集"]').trigger('submit')
    await flushPromises()

    expect(api.readDatasetMembers).toHaveBeenCalledWith(createdDataset.id)
    expect(wrapper.text()).not.toContain('正在加载视频状态')

    staleMembers.resolve({
      items: [MEMBER_REGISTERED],
      page: 1,
      page_size: 50,
      total: 1,
    })
    await flushPromises()

    expect(wrapper.text()).not.toContain(MEMBER_REGISTERED.original_filename)
    wrapper.unmount()
  })

  it('clears previous members when a newly created dataset member load fails', async () => {
    grant('dataset.dataset.view', 'dataset.dataset.import')
    const createdDataset = { ...DATASET, id: 'dataset-created', name: '新建数据集' }
    api.readDatasetMembers
      .mockResolvedValueOnce({
        items: [MEMBER_REGISTERED],
        page: 1,
        page_size: 50,
        total: 1,
      })
      .mockRejectedValueOnce(new Error('新数据集成员读取失败'))
    api.createTrainingDataset.mockResolvedValue(createdDataset)

    const { wrapper } = await mountDatasets()
    await flushPromises()
    expect(wrapper.text()).toContain(MEMBER_REGISTERED.original_filename)

    await wrapper.find('input[name="dataset-name"]').setValue(createdDataset.name)
    await wrapper.find('form[aria-label="创建训练数据集"]').trigger('submit')
    await flushPromises()

    expect(api.readDatasetMembers).toHaveBeenCalledWith(createdDataset.id)
    expect(wrapper.text()).not.toContain(MEMBER_REGISTERED.original_filename)
    expect(wrapper.text()).toContain('新数据集成员读取失败')
    wrapper.unmount()
  })

  it('stops annotation preparation polling when annotation is closed', async () => {
    grant('dataset.dataset.view', 'dataset.dataset.edit')
    api.createAnnotationContext.mockResolvedValue({
      ...ANNOTATION_CONTEXT,
      preparation_status: 'pending',
    })

    const { wrapper } = await mountDatasets()
    await flushPromises()
    vi.useFakeTimers()

    const enterAnnotation = wrapper.findAll('button').find((button) => button.text() === '进入标注')
    expect(enterAnnotation).toBeDefined()
    await enterAnnotation!.trigger('click')
    await flushPromises()

    const closeAnnotation = wrapper.findAll('button').find((button) => button.text() === '关闭标注')
    expect(closeAnnotation).toBeDefined()
    await closeAnnotation!.trigger('click')
    await vi.advanceTimersByTimeAsync(1000)
    await flushPromises()

    expect(api.readAnnotationContext).not.toHaveBeenCalled()
    expect(wrapper.find('h2#annotation-editor-heading').exists()).toBe(false)
    wrapper.unmount()
  })

  it('stops annotation preparation polling when the view unmounts', async () => {
    grant('dataset.dataset.view', 'dataset.dataset.edit')
    api.createAnnotationContext.mockResolvedValue({
      ...ANNOTATION_CONTEXT,
      preparation_status: 'pending',
    })

    const { wrapper } = await mountDatasets()
    await flushPromises()
    vi.useFakeTimers()

    const enterAnnotation = wrapper.findAll('button').find((button) => button.text() === '进入标注')
    expect(enterAnnotation).toBeDefined()
    await enterAnnotation!.trigger('click')
    await flushPromises()

    wrapper.unmount()
    await vi.advanceTimersByTimeAsync(1000)
    await flushPromises()

    expect(api.readAnnotationContext).not.toHaveBeenCalled()
  })

  it('opens the independent annotation UI after a registered member is prepared', async () => {
    grant('dataset.dataset.view', 'dataset.dataset.edit')

    const { wrapper } = await mountDatasets()
    await flushPromises()

    const enterAnnotation = wrapper.findAll('button').find((button) => button.text() === '进入标注')
    expect(enterAnnotation).toBeDefined()
    await enterAnnotation!.trigger('click')
    await flushPromises()

    expect(api.createAnnotationContext).toHaveBeenCalledWith(DATASET.id, MEMBER_REGISTERED.id)
    expect(wrapper.find('h2#annotation-editor-heading').text()).toBe('动作标注')
    expect(wrapper.find('a.datasets__annotation-launch-link').attributes('href')).toBe(
      '/annotation/?context=signed-context',
    )
    expect(wrapper.text()).toContain('独立的 NVIDIA React 界面')

    wrapper.unmount()
  })

  it('allows an editor to append an action-list revision without changing old entries', async () => {
    grant('dataset.dataset.view', 'dataset.dataset.edit')

    const { wrapper } = await mountDatasets()
    await flushPromises()

    expect(wrapper.find('form[aria-label="登记动作列表"]').exists()).toBe(true)
    await wrapper.find('textarea[name="dataset-action-list"]').setValue('(1) 取料\n(2) 安装')
    await wrapper.find('form[aria-label="登记动作列表"]').trigger('submit')
    await flushPromises()

    expect(api.registerDatasetActionList).toHaveBeenCalledWith(DATASET.id, ['(1) 取料', '(2) 安装'])
    expect(wrapper.text()).toContain('版本 1')
    expect(wrapper.text()).toContain('(1) 取料')

    await wrapper.find('.datasets__action-list button').trigger('click')
    await flushPromises()
    expect(api.listDatasetActionListVersions).toHaveBeenCalledWith(DATASET.id)
    wrapper.unmount()
  })

  it('keeps object transfer progress separate from the center validation state', async () => {
    grant('dataset.dataset.view', 'dataset.dataset.import')
    api.readDatasetMembers.mockResolvedValue({ items: [], page: 1, page_size: 50, total: 0 })
    const pendingJob = {
      id: 'job-1',
      job_type: 'dataset_validation',
      status: 'pending',
      member_id: 'member-1',
      attempt_id: 'attempt-1',
      failure_code: null,
      created_at: '2026-09-08T01:00:00Z',
      updated_at: '2026-09-08T01:00:00Z',
    }
    api.requestVideoUpload.mockResolvedValue({
      member: { ...MEMBER_PENDING, status: 'pending_upload', current_attempt_id: ATTEMPT.id },
      attempt: ATTEMPT,
      upload: UPLOAD,
    })
    api.confirmVideoUpload.mockResolvedValue({ member: MEMBER_PENDING, job: pendingJob })

    const { wrapper } = await mountDatasets()
    await flushPromises()
    await wrapper.find('input[name="video-source"]').setValue('camera-A12')
    await chooseFile(wrapper, new File(['video bytes'], 'line-1.mp4', { type: 'video/mp4' }))
    await wrapper.find('form[aria-label="上传训练视频"]').trigger('submit')
    await flushPromises()

    expect(api.requestVideoUpload).toHaveBeenCalledWith(
      DATASET.id,
      expect.objectContaining({
        original_filename: 'line-1.mp4',
        source: 'camera-A12',
        declared_size: 11,
        declared_sha256: 'ab'.repeat(32),
      }),
      'idempotency-1',
    )
    expect(upload).toHaveBeenCalledWith(UPLOAD, expect.any(File), expect.any(Function))
    expect(api.confirmVideoUpload).toHaveBeenCalledWith(DATASET.id, 'member-1', ATTEMPT.id)
    expect(wrapper.text()).toContain('本地传输 100%')
    expect(wrapper.text()).toContain('待校验')
    expect(wrapper.text()).not.toContain('已登记')
    expect(wrapper.text()).not.toContain(UPLOAD.url)

    wrapper.unmount()
  })

  it('keeps refresh recovery metadata until center confirmation succeeds', async () => {
    grant('dataset.dataset.view', 'dataset.dataset.import')
    api.readDatasetMembers.mockResolvedValue({ items: [], page: 1, page_size: 50, total: 0 })
    api.requestVideoUpload.mockResolvedValue({
      member: { ...MEMBER_PENDING, status: 'pending_upload', current_attempt_id: ATTEMPT.id },
      attempt: ATTEMPT,
      upload: UPLOAD,
    })
    api.confirmVideoUpload
      .mockRejectedValueOnce(new Error('确认请求网络中断'))
      .mockResolvedValueOnce({ member: MEMBER_PENDING, job: { id: 'job-1', status: 'pending' } })

    const { wrapper } = await mountDatasets()
    await flushPromises()
    await wrapper.find('input[name="video-source"]').setValue('camera-A12')
    await chooseFile(wrapper, new File(['video bytes'], 'line-1.mp4', { type: 'video/mp4' }))
    await wrapper.find('form[aria-label="上传训练视频"]').trigger('submit')
    await flushPromises()

    expect(window.sessionStorage.getItem('sop.pending-training-upload')).toContain('attempt-1')
    expect(wrapper.text()).toContain('重试通知中心')

    await wrapper
      .findAll('button')
      .find((button) => button.text() === '重试通知中心')!
      .trigger('click')
    await flushPromises()

    expect(api.confirmVideoUpload).toHaveBeenCalledTimes(2)
    expect(window.sessionStorage.getItem('sop.pending-training-upload')).toBeNull()
    wrapper.unmount()
  })

  it('leaves a failed upload request retryable', async () => {
    grant('dataset.dataset.view', 'dataset.dataset.import')
    api.readDatasetMembers.mockResolvedValue({ items: [], page: 1, page_size: 50, total: 0 })
    api.requestVideoUpload.mockRejectedValueOnce(new Error('申请直传失败')).mockResolvedValueOnce({
      member: { ...MEMBER_PENDING, status: 'pending_upload' },
      attempt: ATTEMPT,
      upload: UPLOAD,
    })
    api.confirmVideoUpload.mockResolvedValue({ member: MEMBER_PENDING, job: null })

    const { wrapper } = await mountDatasets()
    await flushPromises()
    await wrapper.find('input[name="video-source"]').setValue('camera-A12')
    await chooseFile(wrapper, new File(['video bytes'], 'line-1.mp4', { type: 'video/mp4' }))
    await wrapper.find('form[aria-label="上传训练视频"]').trigger('submit')
    await flushPromises()

    expect(wrapper.text()).toContain('申请直传失败')
    expect(
      wrapper.find('form[aria-label="上传训练视频"] button').attributes('disabled'),
    ).toBeUndefined()

    await wrapper.find('form[aria-label="上传训练视频"]').trigger('submit')
    await flushPromises()

    expect(api.requestVideoUpload).toHaveBeenCalledTimes(2)
    expect(api.confirmVideoUpload).toHaveBeenCalledWith(DATASET.id, 'member-1', ATTEMPT.id)
    wrapper.unmount()
  })

  it('reuses the same upload attempt after a direct transfer failure', async () => {
    grant('dataset.dataset.view', 'dataset.dataset.import')
    api.readDatasetMembers.mockResolvedValue({ items: [], page: 1, page_size: 50, total: 0 })
    api.requestVideoUpload.mockResolvedValue({
      member: {
        ...MEMBER_REGISTERED,
        status: 'pending_upload',
        declared_sha256: 'ab'.repeat(32),
      },
      attempt: { ...ATTEMPT, declared_sha256: 'ab'.repeat(32) },
      upload: UPLOAD,
    })
    api.confirmVideoUpload.mockResolvedValue({ member: MEMBER_PENDING, job: null })
    upload.mockRejectedValueOnce(new Error('对象存储网络中断'))

    const { wrapper } = await mountDatasets()
    await flushPromises()
    await wrapper.find('input[name="video-source"]').setValue('camera-A12')
    await chooseFile(wrapper, new File(['video bytes'], 'line-1.mp4', { type: 'video/mp4' }))
    await wrapper.find('form[aria-label="上传训练视频"]').trigger('submit')
    await flushPromises()

    expect(api.requestVideoUpload).toHaveBeenCalledOnce()
    expect(api.confirmVideoUpload).not.toHaveBeenCalled()
    expect(wrapper.text()).toContain('重试上传')
    wrapper.unmount()

    const remounted = await mountDatasets()
    await flushPromises()
    await remounted.wrapper.find('input[name="video-source"]').setValue('camera-A12')
    await chooseFile(
      remounted.wrapper,
      new File(['video bytes'], 'line-1.mp4', { type: 'video/mp4' }),
    )
    upload.mockImplementationOnce((_instructions, _file, onProgress) =>
      Promise.resolve().then(() => onProgress(100)),
    )
    await remounted.wrapper.find('form[aria-label="上传训练视频"]').trigger('submit')
    await flushPromises()

    expect(api.requestVideoUpload).toHaveBeenCalledTimes(2)
    expect(api.requestVideoUpload.mock.calls[1]).toEqual([
      DATASET.id,
      expect.objectContaining({
        original_filename: 'line-1.mp4',
        source: 'camera-A12',
        declared_size: 11,
        declared_sha256: 'ab'.repeat(32),
      }),
      'idempotency-1',
    ])
    expect(api.confirmVideoUpload).toHaveBeenCalledWith(DATASET.id, 'member-1', ATTEMPT.id)

    remounted.wrapper.unmount()
  })

  it('offers center-validation recovery without re-uploading the object', async () => {
    grant('dataset.dataset.view', 'dataset.dataset.import')
    api.readDatasetMembers.mockResolvedValue({
      items: [MEMBER_FAILED_VALIDATION],
      page: 1,
      page_size: 50,
      total: 1,
    })
    const retryJob = {
      id: 'job-2',
      job_type: 'dataset_validation',
      status: 'enqueued',
      member_id: 'member-1',
      attempt_id: 'attempt-1',
      failure_code: null,
      created_at: '2026-09-08T01:02:00Z',
      updated_at: '2026-09-08T01:02:00Z',
    }
    api.retryVideoUpload.mockResolvedValue({
      member: { ...MEMBER_FAILED_VALIDATION, status: 'pending_validation', recovery_action: null },
      attempt: ATTEMPT,
      upload: null,
      job: retryJob,
    })

    const { wrapper } = await mountDatasets()
    await flushPromises()
    await wrapper
      .findAll('button')
      .find((button) => button.text() === '重新校验')!
      .trigger('click')
    await flushPromises()

    expect(api.retryVideoUpload).toHaveBeenCalledWith(DATASET.id, 'member-1', 'retry_validation')
    expect(wrapper.text()).toContain('待校验')
    expect(wrapper.text()).toContain('job-2')
    expect(upload).not.toHaveBeenCalled()

    wrapper.unmount()
  })

  it('keeps a failed upload recoverable with a new signed attempt', async () => {
    grant('dataset.dataset.view', 'dataset.dataset.import')
    api.readDatasetMembers.mockResolvedValue({
      items: [MEMBER_FAILED_UPLOAD],
      page: 1,
      page_size: 50,
      total: 1,
    })
    api.retryVideoUpload.mockResolvedValue({
      member: {
        ...MEMBER_FAILED_UPLOAD,
        current_attempt_id: 'attempt-2',
        status: 'pending_upload',
      },
      attempt: { ...ATTEMPT, id: 'attempt-2' },
      upload: { ...UPLOAD, object_key: 'training-datasets/dataset-1/member-1/attempt-2/video' },
      job: null,
    })

    const { wrapper } = await mountDatasets()
    await flushPromises()
    await wrapper
      .findAll('button')
      .find((button) => button.text() === '重新上传')!
      .trigger('click')
    await flushPromises()

    expect(api.retryVideoUpload).toHaveBeenCalledWith(
      DATASET.id,
      'member-1',
      'retry_upload',
      'idempotency-1',
    )
    expect(wrapper.text()).toContain('开始重新上传')

    await chooseFile(wrapper, new File(['video bytes'], 'line-1.mp4', { type: 'video/mp4' }))
    await wrapper.find('form[aria-label="上传训练视频"]').trigger('submit')
    await flushPromises()

    expect(upload).toHaveBeenCalledWith(
      expect.objectContaining({
        object_key: 'training-datasets/dataset-1/member-1/attempt-2/video',
      }),
      expect.any(File),
      expect.any(Function),
    )
    expect(api.confirmVideoUpload).toHaveBeenCalledWith(DATASET.id, 'member-1', 'attempt-2')

    wrapper.unmount()
  })

  it('polls validation jobs and stops once member facts settle', async () => {
    grant('dataset.dataset.view', 'dataset.dataset.import')
    api.readDatasetMembers
      .mockResolvedValueOnce({ items: [MEMBER_PENDING], page: 1, page_size: 50, total: 1 })
      .mockResolvedValueOnce({ items: [MEMBER_PENDING], page: 1, page_size: 50, total: 1 })
      .mockResolvedValueOnce({ items: [MEMBER_REGISTERED], page: 1, page_size: 50, total: 1 })
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] })

    try {
      const { wrapper } = await mountDatasets()
      await flushPromises()

      expect(api.readJob).not.toHaveBeenCalled()
      await vi.advanceTimersByTimeAsync(2000)
      await flushPromises()
      expect(api.readJob).toHaveBeenCalledWith('job-1')
      expect(wrapper.text()).toContain('任务执行中')

      await vi.advanceTimersByTimeAsync(2000)
      await flushPromises()
      expect(api.readDatasetMembers).toHaveBeenCalledTimes(3)
      expect(wrapper.text()).toContain('已登记')

      const jobReads = api.readJob.mock.calls.length
      await vi.advanceTimersByTimeAsync(4000)
      await flushPromises()
      expect(api.readJob).toHaveBeenCalledTimes(jobReads)
      wrapper.unmount()
    } finally {
      vi.useRealTimers()
    }
  })

  it('does not overlap a slow usage refresh with the next polling interval', async () => {
    grant('dataset.dataset.view')
    const pendingUsageCheck = { ...USAGE_CHECK, status: 'pending' }
    const slowUsagePage = deferred<{
      items: (typeof USAGE_CHECK)[]
      page: number
      page_size: number
      total: number
    }>()
    api.listDatasetUsageChecks
      .mockResolvedValueOnce({
        items: [pendingUsageCheck],
        page: 1,
        page_size: 50,
        total: 1,
      })
      .mockImplementationOnce(() => slowUsagePage.promise)
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] })

    try {
      const { wrapper } = await mountDatasets()
      await flushPromises()
      expect(api.listDatasetUsageChecks).toHaveBeenCalledTimes(1)

      await vi.advanceTimersByTimeAsync(2000)
      await flushPromises()
      expect(api.listDatasetUsageChecks).toHaveBeenCalledTimes(2)

      await vi.advanceTimersByTimeAsync(2000)
      await flushPromises()
      expect(api.listDatasetUsageChecks).toHaveBeenCalledTimes(2)

      slowUsagePage.resolve({
        items: [USAGE_CHECK],
        page: 1,
        page_size: 50,
        total: 1,
      })
      await flushPromises()

      await vi.advanceTimersByTimeAsync(4000)
      await flushPromises()
      expect(api.listDatasetUsageChecks).toHaveBeenCalledTimes(2)
      wrapper.unmount()
    } finally {
      vi.useRealTimers()
    }
  })

  it('allows the newly selected dataset to poll while the previous refresh is still pending', async () => {
    grant('dataset.dataset.view')
    const secondDataset = { ...DATASET, id: 'dataset-2', name: '第二个数据集' }
    const pendingUsageCheck = { ...USAGE_CHECK, status: 'pending' }
    const slowPreviousUsagePage = deferred<{
      items: (typeof USAGE_CHECK)[]
      page: number
      page_size: number
      total: number
    }>()
    api.readTrainingDatasets.mockResolvedValue({
      items: [DATASET, secondDataset],
      page: 1,
      page_size: 50,
      total: 2,
    })
    api.listDatasetUsageChecks
      .mockResolvedValueOnce({
        items: [pendingUsageCheck],
        page: 1,
        page_size: 50,
        total: 1,
      })
      .mockImplementationOnce(() => slowPreviousUsagePage.promise)
      .mockResolvedValueOnce({
        items: [{ ...pendingUsageCheck, dataset_id: secondDataset.id }],
        page: 1,
        page_size: 50,
        total: 1,
      })
      .mockResolvedValueOnce({
        items: [{ ...USAGE_CHECK, dataset_id: secondDataset.id }],
        page: 1,
        page_size: 50,
        total: 1,
      })
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] })

    try {
      const { wrapper } = await mountDatasets()
      await flushPromises()

      await vi.advanceTimersByTimeAsync(2000)
      await flushPromises()
      expect(api.listDatasetUsageChecks).toHaveBeenCalledTimes(2)

      const chooseSecond = wrapper.findAll('button').find((button) => button.text() === '查看成员')
      expect(chooseSecond).toBeDefined()
      await chooseSecond!.trigger('click')
      await flushPromises()

      const secondDatasetReadsBeforePolling = api.readDatasetMembers.mock.calls.filter(
        ([datasetId]) => datasetId === secondDataset.id,
      ).length
      expect(secondDatasetReadsBeforePolling).toBe(1)

      await vi.advanceTimersByTimeAsync(2000)
      await flushPromises()

      expect(
        api.readDatasetMembers.mock.calls.filter(([datasetId]) => datasetId === secondDataset.id),
      ).toHaveLength(2)

      slowPreviousUsagePage.resolve({
        items: [USAGE_CHECK],
        page: 1,
        page_size: 50,
        total: 1,
      })
      await flushPromises()
      wrapper.unmount()
    } finally {
      vi.useRealTimers()
    }
  })

  it('polls the submitted job with import permission without reading dataset members', async () => {
    grant('dataset.dataset.import')
    api.requestVideoUpload.mockResolvedValue({
      member: { ...MEMBER_PENDING, status: 'pending_upload' },
      attempt: ATTEMPT,
      upload: UPLOAD,
    })
    api.confirmVideoUpload.mockResolvedValue({
      member: MEMBER_PENDING,
      job: {
        id: 'job-1',
        status: 'pending',
        failure_code: null,
      },
    })
    api.readJob.mockResolvedValueOnce({
      id: 'job-1',
      job_type: 'dataset_validation',
      status: 'failed',
      member_id: 'member-1',
      attempt_id: 'attempt-1',
      failure_code: 'MEDIA_PROBE_UNAVAILABLE',
      created_at: '2026-09-08T01:00:00Z',
      updated_at: '2026-09-08T01:01:00Z',
    })
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] })

    try {
      const { wrapper } = await mountDatasets()
      await flushPromises()
      await wrapper.find('input[name="known-dataset-id"]').setValue(DATASET.id)
      await wrapper.find('input[name="video-source"]').setValue('camera-A12')
      await chooseFile(wrapper, new File(['video bytes'], 'line-1.mp4', { type: 'video/mp4' }))
      await wrapper.find('form[aria-label="上传训练视频"]').trigger('submit')
      await flushPromises()
      expect(api.readTrainingDatasets).not.toHaveBeenCalled()
      expect(api.readDatasetMembers).not.toHaveBeenCalled()
      await vi.advanceTimersByTimeAsync(2000)
      await flushPromises()

      expect(api.readJob).toHaveBeenCalledWith('job-1')
      expect(api.readDatasetMembers).not.toHaveBeenCalled()
      expect(wrapper.text()).toContain('任务失败')
      expect(wrapper.text()).toContain('MEDIA_PROBE_UNAVAILABLE')

      const jobReads = api.readJob.mock.calls.length
      await vi.advanceTimersByTimeAsync(4000)
      await flushPromises()
      expect(api.readJob).toHaveBeenCalledTimes(jobReads)
      wrapper.unmount()
    } finally {
      vi.useRealTimers()
    }
  })

  it('shows unknown state values and backend error details instead of dropping the row', async () => {
    grant('dataset.dataset.view')
    api.readDatasetMembers.mockResolvedValue({
      items: [
        {
          ...MEMBER_REGISTERED,
          status: 'future_status',
          failure_code: 'SOME_FUTURE_FAILURE',
          failure_detail: '后端新增了尚未识别的状态',
        },
      ],
      page: 1,
      page_size: 50,
      total: 1,
    })

    const { wrapper } = await mountDatasets()
    await flushPromises()

    expect(wrapper.text()).toContain('未知状态（future_status）')
    expect(wrapper.text()).toContain('SOME_FUTURE_FAILURE')
    expect(wrapper.text()).toContain('后端新增了尚未识别的状态')

    wrapper.unmount()
  })

  it('renders a control-plane refusal with its stable code and field location', async () => {
    grant('dataset.dataset.view', 'dataset.dataset.import')
    api.createTrainingDataset.mockRejectedValue(
      new ControlPlaneError({
        message: '数据集名称无效',
        errorCode: 'DATASET_NAME_INVALID',
        status: 422,
        fieldErrors: [{ field: 'name', message: '不能为空' }],
      }),
    )

    const { wrapper } = await mountDatasets()
    await flushPromises()
    await wrapper.find('input[name="dataset-name"]').setValue('后端拒绝名称')
    await wrapper.find('form[aria-label="创建训练数据集"]').trigger('submit')
    await flushPromises()

    expect(api.createTrainingDataset).toHaveBeenCalledWith('后端拒绝名称')
    expect(wrapper.text()).toContain('DATASET_NAME_INVALID')
    expect(wrapper.text()).toContain('数据集名称无效')
    expect(wrapper.text()).toContain('name：不能为空')

    wrapper.unmount()
  })
})
