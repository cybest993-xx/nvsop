import { expect, test, type Page } from '@playwright/test'

/**
 * SYS-30-01 — 训练视频经中心授权入口逐个流式上传，覆盖登录授权、流式 PUT 与失败态。
 *
 * 页面跑的是真实组件与真实生成客户端；控制面 API 按其已发布契约打桩（列表信封、双提交
 * CSRF Cookie、problem+json 失败）。后端对同一批用例的真实行为由
 * `apps/control-api/tests/integration/test_dataset_real_media.py` 对着真实 PostgreSQL、
 * Redis、ffprobe 与中心本地训练素材卷证明。
 */

const DATASET_ID = '018f0000-0000-7000-8000-00000000d301'
const MEMBER_ID = '018f0000-0000-7000-8000-00000000d302'
const ATTEMPT_ID = '018f0000-0000-7000-8000-00000000d303'
const JOB_ID = '018f0000-0000-7000-8000-00000000d304'
const UPLOAD_PATH = `/api/v1/training-datasets/${DATASET_ID}/members/${MEMBER_ID}/attempts/${ATTEMPT_ID}/content`
const VIDEO_BYTES = Buffer.from('training video bytes for the browser upload path')

const SESSION = {
  user_id: '018f0000-0000-7000-8000-00000000d201',
  login_name: 'zhang.operator',
  display_name: '张操作员',
  expires_at: '2026-09-07T13:00:00Z',
  permissions: ['dataset.dataset.view', 'dataset.dataset.edit', 'dataset.dataset.import'],
}

const CREDENTIALS = { login_name: 'zhang.operator', password: 'first-shift-key' } // pragma: allowlist secret

const DATASET = {
  id: DATASET_ID,
  name: '装配训练集',
  created_by: SESSION.user_id,
  updated_by: SESSION.user_id,
  created_at: '2026-09-08T01:00:00Z',
  updated_at: '2026-09-08T01:00:00Z',
}

function envelope<T>(items: T[]) {
  return { items, page: 1, page_size: 50, total: items.length }
}

function member(status: string, failureCode: string | null = null) {
  return {
    id: MEMBER_ID,
    dataset_id: DATASET_ID,
    original_filename: 'line-1.mp4',
    source: 'camera-A12',
    declared_size: VIDEO_BYTES.length,
    declared_sha256: null,
    current_attempt_id: ATTEMPT_ID,
    status,
    actual_size: null,
    actual_sha256: null,
    duration_seconds: null,
    codec: null,
    container: null,
    validation_job_id: status === 'pending_validation' ? JOB_ID : null,
    failure_code: failureCode,
    failure_detail: failureCode === null ? null : '上传失败',
    recovery_action: failureCode === null ? null : 'upload',
    created_by: SESSION.user_id,
    updated_by: SESSION.user_id,
    created_at: '2026-09-08T01:00:00Z',
    updated_at: '2026-09-08T01:01:00Z',
  }
}

const ATTEMPT = {
  id: ATTEMPT_ID,
  member_id: MEMBER_ID,
  status: 'pending_upload',
  declared_size: VIDEO_BYTES.length,
  declared_sha256: null,
  expires_at: '2026-09-08T09:00:00Z',
  object_version_id: null,
}

const UPLOAD = {
  method: 'PUT',
  url: UPLOAD_PATH,
  fields: {},
  headers: { 'Content-Type': 'application/octet-stream' },
  expires_at: '2026-09-08T09:00:00Z',
  max_bytes: 8 * 1024 ** 3,
  object_key: `training-datasets/${DATASET_ID}/members/${MEMBER_ID}/attempts/${ATTEMPT_ID}/video`,
}

/** 登录、列表与上传申请都按发布契约打桩；PUT 的状态由每个场景决定。 */
async function mockControlPlane(page: Page, putStatus: number) {
  const requests: { url: string; method: string; body: string; csrf: string | null }[] = []
  let signedIn = false

  // 未在本场景用到的读接口返回空信封，避免页面等不到响应。
  await page.route(/\/api\/v1\/.*/, async (route) => {
    if (route.request().method() !== 'GET') {
      await route.fulfill({
        status: 404,
        contentType: 'application/problem+json',
        body: JSON.stringify({ title: '未打桩的写接口', error_code: 'NOT_FOUND' }),
      })
      return
    }
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(envelope([])),
    })
  })
  await page.route('**/api/v1/auth/session', async (route) => {
    if (route.request().method() === 'POST') {
      signedIn = true
      await route.fulfill({
        status: 201,
        contentType: 'application/json',
        headers: {
          'set-cookie':
            'sop_session=mocked-session; Path=/; HttpOnly; SameSite=Strict\n' +
            'sop_csrf=mocked-csrf; Path=/; SameSite=Strict',
        },
        body: JSON.stringify(SESSION),
      })
      return
    }
    if (!signedIn) {
      await route.fulfill({
        status: 401,
        contentType: 'application/problem+json',
        body: JSON.stringify({ title: '请先登录', error_code: 'AUTHENTICATION_REQUIRED' }),
      })
      return
    }
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(SESSION),
    })
  })
  await page.route('**/api/v1/auth/permissions', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(envelope(SESSION.permissions)),
    })
  })
  await page.route(
    new RegExp(`/api/v1/training-datasets/${DATASET_ID}/members(\\?.*)?$`),
    async (route) => {
      if (route.request().method() === 'POST') {
        await route.fulfill({
          status: 201,
          contentType: 'application/json',
          body: JSON.stringify({
            member: member('pending_upload'),
            attempt: ATTEMPT,
            upload: UPLOAD,
          }),
        })
        return
      }
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(envelope([])),
      })
    },
  )
  await page.route(new RegExp('/api/v1/training-datasets(\\?.*)?$'), async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(envelope([DATASET])),
    })
  })
  await page.route(new RegExp(`/members/${MEMBER_ID}/confirm$`), async (route) => {
    await route.fulfill({
      status: 202,
      contentType: 'application/json',
      body: JSON.stringify({
        member: member('pending_validation'),
        job: { id: JOB_ID, status: 'queued' },
      }),
    })
  })
  await page.route(new RegExp(`/api/v1/jobs/${JOB_ID}$`), async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ id: JOB_ID, status: 'queued', job_type: 'dataset.video_validation' }),
    })
  })
  await page.route(new RegExp(`${UPLOAD_PATH}$`), async (route) => {
    const request = route.request()
    requests.push({
      url: request.url(),
      method: request.method(),
      body: request.postData() ?? '',
      csrf: request.headers()['x-csrf-token'] ?? null,
    })
    if (putStatus >= 400) {
      await route.fulfill({
        status: putStatus,
        contentType: 'application/problem+json',
        body: JSON.stringify({
          title: '上传授权已过期，请重新申请上传',
          error_code: 'STATE_CONFLICT',
        }),
      })
      return
    }
    await route.fulfill({ status: 204, body: '' })
  })
  return requests
}

async function signIn(page: Page) {
  await page.goto('/login')
  await page.getByLabel('登录名').fill(CREDENTIALS.login_name)
  await page.getByLabel('密码').fill(CREDENTIALS.password)
  await page.getByRole('button', { name: '登录' }).click()
  await expect(page.getByRole('banner')).toBeVisible()
}

async function chooseVideo(page: Page) {
  await page.goto('/training-datasets')
  await expect(page.getByRole('heading', { name: '训练数据集' })).toBeVisible()
  await page.getByLabel('目标训练数据集').selectOption(DATASET_ID)
  await page.getByLabel('视频来源').fill('camera-A12')
  await page.getByLabel('视频文件').setInputFiles({
    name: 'line-1.mp4',
    mimeType: 'video/mp4',
    buffer: VIDEO_BYTES,
  })
}

test('SYS-30-01 — 浏览器把已选文件原样流式 PUT 给中心，不预读整段视频', async ({ page }) => {
  const requests = await mockControlPlane(page, 204)
  await signIn(page)
  await chooseVideo(page)

  await page.getByRole('button', { name: '上传视频' }).click()

  await expect.poll(() => requests.length).toBe(1)
  const upload = requests.at(0)
  if (upload === undefined) throw new Error('浏览器没有发出流式上传请求')
  expect(upload.method).toBe('PUT')
  expect(upload.url).toBe(new URL(UPLOAD_PATH, page.url()).toString())
  expect(upload.body).toBe(VIDEO_BYTES.toString())
  // 双提交 CSRF 头与已发布契约一致；客户端不提交声明摘要。
  expect(upload.csrf).toBe('mocked-csrf')
  await expect(page.getByText('校验任务已提交', { exact: false })).toBeVisible()
})

test('SYS-30-01 — 中心拒绝流式上传时页面显示失败态而不是伪成功', async ({ page }) => {
  await mockControlPlane(page, 409)
  await signIn(page)
  await chooseVideo(page)

  await page.getByRole('button', { name: '上传视频' }).click()

  const failure = page.getByRole('alert')
  await expect(failure).toBeVisible()
  await expect(failure).toContainText('视频上传失败')
  await expect(failure).toContainText('409')
  await expect(page.getByRole('button', { name: '重试上传' })).toBeVisible()
})
