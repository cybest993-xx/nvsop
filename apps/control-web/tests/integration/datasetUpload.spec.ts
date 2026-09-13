import { afterEach, describe, expect, it, vi } from 'vitest'

import { ObjectUploadError, uploadVideoObject } from '@/modules/datasets/upload'

const INSTRUCTIONS = {
  method: 'POST',
  url: 'https://minio.example.test/factory-sop',
  fields: { key: 'training-datasets/dataset-1/video-1', policy: 'signed-policy' },
  headers: { 'x-amz-meta-test': 'allowed' },
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
  withCredentials = true
  status = 0
  body: FormData | null = null

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

  send(body: FormData): void {
    this.body = body
  }

  finish(status: number): void {
    this.status = status
    this.emit('load', new Event('load'))
  }

  fail(): void {
    this.emit('error', new Event('error'))
  }
}

afterEach(() => {
  vi.unstubAllGlobals()
  FakeXmlHttpRequest.instances = []
})

describe('direct object-store upload', () => {
  it('posts only the signed form to MinIO and reports transfer progress', async () => {
    vi.stubGlobal('XMLHttpRequest', FakeXmlHttpRequest)
    const file = new File(['video bytes'], 'line-1.mp4', { type: 'video/mp4' })
    const progress: number[] = []

    const pending = uploadVideoObject(INSTRUCTIONS, file, (value) => progress.push(value))
    const request = FakeXmlHttpRequest.instances[0]!

    expect(request.method).toBe('POST')
    expect(request.url).toBe(INSTRUCTIONS.url)
    expect(request.async).toBe(true)
    expect(request.withCredentials).toBe(false)
    expect(request.headers.get('x-amz-meta-test')).toBe('allowed')
    expect(request.body?.get('key')).toBe(INSTRUCTIONS.fields.key)
    expect(request.body?.get('policy')).toBe(INSTRUCTIONS.fields.policy)
    expect(request.body?.get('file')).toBeInstanceOf(File)

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

  it('does not turn an object-store refusal into a successful transfer', async () => {
    vi.stubGlobal('XMLHttpRequest', FakeXmlHttpRequest)
    const pending = uploadVideoObject(
      INSTRUCTIONS,
      new File(['video bytes'], 'line-1.mp4', { type: 'video/mp4' }),
      vi.fn(),
    )
    FakeXmlHttpRequest.instances[0]!.finish(403)

    await expect(pending).rejects.toMatchObject({
      name: 'ObjectUploadError',
      status: 403,
    } satisfies Partial<ObjectUploadError>)
  })

  it('fails closed when the backend returns an unsupported method', async () => {
    await expect(
      uploadVideoObject(
        { ...INSTRUCTIONS, method: 'PUT' },
        new File(['video bytes'], 'line-1.mp4'),
        vi.fn(),
      ),
    ).rejects.toMatchObject({ status: 0 })
  })
})
