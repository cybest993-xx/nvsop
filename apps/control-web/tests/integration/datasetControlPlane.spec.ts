import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import {
  confirmVideoUpload,
  createTrainingDataset,
  readDatasetMembers,
  readJob,
  readTrainingDatasets,
  requestVideoUpload,
  retryVideoUpload,
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
          method: 'POST',
          url: 'https://minio.example.test/upload',
          fields: {},
          headers: {},
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
  })
})
