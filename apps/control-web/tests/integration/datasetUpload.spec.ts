import { afterEach, describe, expect, it, vi } from 'vitest'

import { ObjectUploadError, uploadVideoObject } from '@/modules/datasets/upload'

const UPLOAD_PATH =
  '/api/v1/training-datasets/dataset-1/members/member-1/attempts/attempt-1/content'

const INSTRUCTIONS = {
  method: 'PUT',
  url: UPLOAD_PATH,
  fields: {},
  headers: { 'Content-Type': 'application/octet-stream' },
  expires_at: '2026-09-08T09:00:00Z',
  max_bytes: 100,
  object_key: 'training-datasets/dataset-1/video-1',
}

type Listener = (event: Event) => void

class FakeUploadTarget {
  private readonly listeners = new Map<string, Listener[]>()

  addEventListener(name: string, listener: Listener): void {
    const current = this.listeners.get(name) ?? []
    current.push(listener)
    this.listeners.set(name, current)
  }

  emit(name: string, event: Event): void {
    for (const listener of this.listeners.get(name) ?? []) {
      listener(event)
    }
  }
}

class FakeXmlHttpRequest extends FakeUploadTarget {
  static instances: FakeXmlHttpRequest[] = []
  readonly upload = new FakeUploadTarget()
  readonly headers = new Map<string, string>()
  method = ''
  url = ''
  async = false
  withCredentials = false
  status = 0
  responseText = ''
  body: File | FormData | null = null

  constructor() {
    super()
    FakeXmlHttpRequest.instances.push(this)
  }

  open(method: string, url: string, async: boolean): void {
    this.method = method
    this.url = url
    this.async = async
  }

  setRequestHeader(name: string, value: string): void {
    this.headers.set(name, value)
  }

  send(body: File | FormData): void {
    this.body = body
  }

  finish(status: number, responseText = ''): void {
    this.status = status
    this.responseText = responseText
    this.emit('load', new Event('load'))
  }

  fail(): void {
    this.emit('error', new Event('error'))
  }
}

afterEach(() => {
  vi.unstubAllGlobals()
  FakeXmlHttpRequest.instances = []
  document.cookie = 'sop_csrf=; Max-Age=0; path=/'
})

describe('authenticated center upload', () => {
  it('puts the raw file to the control plane with session cookie and CSRF header', async () => {
    vi.stubGlobal('XMLHttpRequest', FakeXmlHttpRequest)
    document.cookie = 'sop_csrf=csrf-token-1; path=/'
    const file = new File(['video bytes'], 'line-1.mp4', { type: 'video/mp4' })
    const progress: number[] = []

    const pending = uploadVideoObject(INSTRUCTIONS, file, (value) => progress.push(value))
    const request = FakeXmlHttpRequest.instances[0]!

    expect(request.method).toBe('PUT')
    expect(request.url).toBe(new URL(UPLOAD_PATH, window.location.origin).toString())
    expect(request.async).toBe(true)
    expect(request.withCredentials).toBe(true)
    expect(request.headers.get('content-type')).toBe('application/octet-stream')
    expect(request.headers.get('x-csrf-token')).toBe('csrf-token-1')
    expect(request.body).toBe(file)

    request.upload.emit(
      'progress',
      Object.assign(new Event('progress'), { lengthComputable: true, loaded: 50, total: 100 }),
    )
    request.upload.emit(
      'progress',
      Object.assign(new Event('progress'), { lengthComputable: true, loaded: 100, total: 100 }),
    )
    request.finish(204)

    await expect(pending).resolves.toBeUndefined()
    expect(progress).toEqual([50, 100, 100])
  })

  it('does not turn a control-plane refusal into a successful transfer', async () => {
    vi.stubGlobal('XMLHttpRequest', FakeXmlHttpRequest)
    const pending = uploadVideoObject(
      INSTRUCTIONS,
      new File(['video bytes'], 'line-1.mp4', { type: 'video/mp4' }),
      vi.fn(),
    )
    FakeXmlHttpRequest.instances[0]!.finish(422, '{"error_code":"SIZE_EXCEEDED"}')

    await expect(pending).rejects.toMatchObject({
      name: 'ObjectUploadError',
      status: 422,
    } satisfies Partial<ObjectUploadError>)
  })

  it('fails closed when the backend returns an unsupported method', async () => {
    await expect(
      uploadVideoObject(
        { ...INSTRUCTIONS, method: 'POST' },
        new File(['video bytes'], 'line-1.mp4'),
        vi.fn(),
      ),
    ).rejects.toMatchObject({ status: 0 })
  })
})
