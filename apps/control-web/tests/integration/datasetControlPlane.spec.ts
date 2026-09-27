import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import {
  confirmVideoUpload,
  createAnnotationContext,
  createTrainingDataset,
  listAnnotations,
  readAnnotation,
  readAnnotationContext,
  readDatasetMembers,
  readJob,
  readTrainingDatasets,
  requestVideoUpload,
  retryAnnotation,
  retryVideoUpload,
  submitAnnotation,
} from '@/api/controlPlane'

const DATASET_ID = 'dataset-1'
const MEMBER_ID = 'member-1'
const ATTEMPT_ID = 'attempt-1'
const JOB_ID = 'job-1'

function respond(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  })
}

function stubFetch(body: unknown, status = 200) {
  const fetch = vi.fn((_request: Request) => Promise.resolve(respond(status, body)))
  vi.stubGlobal('fetch', fetch)
  return fetch
}

beforeEach(() => {
  document.cookie = 'sop_csrf=dataset-token'
})

afterEach(() => {
  vi.unstubAllGlobals()
  document.cookie = 'sop_csrf=; expires=Thu, 01 Jan 1970 00:00:00 GMT'
})

describe('training-dataset control-plane calls', () => {
  it('uses the generated dataset and job endpoints with their formal request shapes', async () => {
    const page = { items: [], page: 1, page_size: 50, total: 0 }
    const fetch = stubFetch(page)

    await readTrainingDatasets()
    fetch.mockResolvedValueOnce(
      respond(201, {
        id: DATASET_ID,
        name: '装配视频',
        created_by: 'u',
        updated_by: 'u',
        created_at: '2026-09-08T01:00:00Z',
        updated_at: '2026-09-08T01:00:00Z',
      }),
    )
    await createTrainingDataset('装配视频')
    fetch.mockResolvedValueOnce(respond(200, page))
    await readDatasetMembers(DATASET_ID)
    fetch.mockResolvedValueOnce(
      respond(201, {
        member: { id: MEMBER_ID },
        attempt: { id: ATTEMPT_ID },
        upload: {
          method: 'PUT',
          url: '/api/v1/training-datasets/dataset-1/members/member-1/attempts/attempt-1/content',
          fields: {},
          headers: { 'Content-Type': 'application/octet-stream' },
          expires_at: '2026-09-08T09:00:00Z',
          max_bytes: 100,
          object_key: 'training-datasets/dataset-1/member-1/video',
        },
      }),
    )
    await requestVideoUpload(
      DATASET_ID,
      {
        original_filename: 'clip.mp4',
        source: 'camera-A12',
        declared_size: 42,
        declared_sha256: 'a'.repeat(64),
      },
      'idempotency-1',
    )
    fetch.mockResolvedValueOnce(respond(202, { member: { id: MEMBER_ID }, job: { id: JOB_ID } }))
    await confirmVideoUpload(DATASET_ID, MEMBER_ID, ATTEMPT_ID)
    fetch.mockResolvedValueOnce(
      respond(202, {
        member: { id: MEMBER_ID },
        attempt: { id: 'attempt-2' },
        upload: null,
        job: { id: JOB_ID },
      }),
    )
    await retryVideoUpload(DATASET_ID, MEMBER_ID, 'retry_validation')
    fetch.mockResolvedValueOnce(
      respond(200, { id: JOB_ID, job_type: 'dataset_validation', status: 'running' }),
    )
    await readJob(JOB_ID)

    const requests = fetch.mock.calls.map(([request]) => request)
    expect(new URL(requests[0]!.url).pathname).toBe('/api/v1/training-datasets')
    expect(requests[0]!.method).toBe('GET')
    expect(requests[0]!.cache).toBe('no-store')
    expect(new URL(requests[1]!.url).pathname).toBe('/api/v1/training-datasets')
    await expect(requests[1]!.clone().json()).resolves.toEqual({ name: '装配视频' })
    expect(new URL(requests[2]!.url).pathname).toBe(
      `/api/v1/training-datasets/${DATASET_ID}/members`,
    )
    expect(new URL(requests[3]!.url).pathname).toBe(
      `/api/v1/training-datasets/${DATASET_ID}/members`,
    )
    expect(requests[3]!.headers.get('Idempotency-Key')).toBe('idempotency-1')
    await expect(requests[3]!.clone().json()).resolves.toMatchObject({
      original_filename: 'clip.mp4',
      declared_size: 42,
    })
    expect(new URL(requests[4]!.url).pathname).toBe(
      `/api/v1/training-datasets/${DATASET_ID}/members/${MEMBER_ID}/confirm`,
    )
    await expect(requests[4]!.clone().json()).resolves.toEqual({ attempt_id: ATTEMPT_ID })
    expect(new URL(requests[5]!.url).pathname).toBe(
      `/api/v1/training-datasets/${DATASET_ID}/members/${MEMBER_ID}/retry`,
    )
    await expect(requests[5]!.clone().json()).resolves.toEqual({ mode: 'retry_validation' })
    expect(new URL(requests[6]!.url).pathname).toBe(`/api/v1/jobs/${JOB_ID}`)
    expect(requests[6]!.cache).toBe('no-store')
  })

  it('uses the generated annotation context, submission, history, and retry endpoints', async () => {
    const context = {
      context_token: 'signed-context',
      dataset_id: DATASET_ID,
      member_id: MEMBER_ID,
      action_list_revision: 2,
      annotation_revision: 0,
      source_object_key: 'object-version-2',
      source_sha256: 'b'.repeat(64),
      derived_video_size: null,
      derived_video_sha256: null,
      derived_video_duration_seconds: null,
      original_filename: 'clip.mp4',
      source: 'camera-A12',
      duration_seconds: 12.5,
      actions: ['(1)拿取工件'],
      video_url: '/api/annotation/api/v1/videos/signed-context/download',
      initial_timestamps: [
        { start: 0, end: 3.5, action_index: 0, action_description: '(1)拿取工件' },
      ],
      two_operator_mode: false,
      expires_at: '2026-09-08T09:00:00Z',
      latest_submission: null,
    }
    const execution = {
      id: 'execution-1',
      submission_id: 'submission-1',
      generation: 1,
      job_id: JOB_ID,
      status: 'pending',
      clips: [],
      failure_code: null,
      failure_detail: null,
      created_at: '2026-09-08T01:00:00Z',
      updated_at: '2026-09-08T01:00:00Z',
    }
    const submission = {
      id: 'submission-1',
      dataset_id: DATASET_ID,
      member_id: MEMBER_ID,
      context_id: 'context-1',
      revision: 1,
      action_list_revision: 2,
      source_object_key: 'object-version-2',
      source_sha256: 'b'.repeat(64),
      idempotency_key: 'annotation-1',
      mode: 'single_operator',
      segments: [{ start: 0, end: 3.5, action_index: 0, action_description: '(1)拿取工件' }],
      raw_segments: [{ start: 0, end: 3.5, actionIndex: 0, actionDescription: 'client input' }],
      created_by: 'user-1',
      created_at: '2026-09-08T01:00:00Z',
      executions: [execution],
    }
    const accepted = {
      submission,
      execution,
      job: {
        id: JOB_ID,
        job_type: 'dataset_annotation',
        status: 'pending',
        member_id: MEMBER_ID,
        attempt_id: 'execution-1',
        failure_code: null,
        created_at: '2026-09-08T01:00:00Z',
        updated_at: '2026-09-08T01:00:00Z',
      },
    }
    const fetch = stubFetch(context)

    await expect(createAnnotationContext(DATASET_ID, MEMBER_ID, {})).resolves.toEqual(context)
    fetch.mockResolvedValueOnce(respond(200, context))
    await expect(readAnnotationContext('signed-context')).resolves.toEqual(context)
    fetch.mockResolvedValueOnce(respond(202, accepted))
    await expect(
      submitAnnotation(
        DATASET_ID,
        MEMBER_ID,
        {
          context_token: 'signed-context',
          mode: 'single_operator',
          segments: [{ start: 0, end: 3.5, action_index: 0, action_description: 'client input' }],
        },
        'annotation-1',
        0,
      ),
    ).resolves.toEqual(accepted)
    fetch.mockResolvedValueOnce(respond(200, { items: [submission] }))
    await expect(listAnnotations(DATASET_ID, MEMBER_ID)).resolves.toEqual({ items: [submission] })
    fetch.mockResolvedValueOnce(respond(200, submission))
    await expect(readAnnotation(DATASET_ID, MEMBER_ID, 'submission-1')).resolves.toEqual(submission)
    fetch.mockResolvedValueOnce(respond(202, accepted))
    await expect(retryAnnotation(DATASET_ID, MEMBER_ID, 'submission-1')).resolves.toEqual(accepted)

    const requests = fetch.mock.calls.map(([request]) => request)
    expect(requests).toHaveLength(6)
    expect(new URL(requests[0]!.url).pathname).toBe(
      `/api/v1/training-datasets/${DATASET_ID}/members/${MEMBER_ID}/annotation-context`,
    )
    await expect(requests[0]!.clone().json()).resolves.toEqual({})
    expect(new URL(requests[1]!.url).pathname).toBe('/api/v1/annotation-contexts/signed-context')
    expect(requests[1]!.cache).toBe('no-store')
    expect(new URL(requests[2]!.url).pathname).toBe(
      `/api/v1/training-datasets/${DATASET_ID}/members/${MEMBER_ID}/annotations`,
    )
    expect(requests[2]!.headers.get('Idempotency-Key')).toBe('annotation-1')
    expect(requests[2]!.headers.get('If-Match')).toBe('0')
    await expect(requests[2]!.clone().json()).resolves.toEqual({
      context_token: 'signed-context',
      mode: 'single_operator',
      segments: [{ start: 0, end: 3.5, action_index: 0, action_description: 'client input' }],
    })
    expect(new URL(requests[3]!.url).pathname).toBe(
      `/api/v1/training-datasets/${DATASET_ID}/members/${MEMBER_ID}/annotations`,
    )
    expect(new URL(requests[4]!.url).pathname).toBe(
      `/api/v1/training-datasets/${DATASET_ID}/members/${MEMBER_ID}/annotations/submission-1`,
    )
    expect(new URL(requests[5]!.url).pathname).toBe(
      `/api/v1/training-datasets/${DATASET_ID}/members/${MEMBER_ID}/annotations/submission-1/retry`,
    )
  })
})
