/**
 * The control-plane client: what a caller is told when a request fails.
 *
 * `fetch` is replaced, which is the seam — everything above it is this application's own code and
 * everything below it is the network. §5.15's unknown-value fallback lives here, so the case that
 * matters most is a response this client has never seen a code for.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { ControlPlaneError, endSession, openSession, readSession } from '@/api/controlPlane'

const CREDENTIALS = { login_name: 'wang.li', password: 'assembly-line-3' }

function respond(
  status: number,
  body: unknown,
  contentType = 'application/problem+json',
): Response {
  return new Response(status === 204 ? null : JSON.stringify(body), {
    status,
    headers: { 'content-type': contentType },
  })
}

function stubFetch(response: Response | Error) {
  // The parameters are declared so `stub.mock.calls[0]` is typed as this pair. Without them the
  // record is an empty tuple and every read of it needs a cast, which is a cast asserting the
  // very thing the assertion below is about (TS2352).
  const stub = vi.fn((_path: string, _options: RequestInit) =>
    response instanceof Error ? Promise.reject(response) : Promise.resolve(response),
  )
  vi.stubGlobal('fetch', stub)
  return stub
}

beforeEach(() => {
  document.cookie = 'sop_csrf=derived-token'
})

afterEach(() => {
  vi.unstubAllGlobals()
  document.cookie = 'sop_csrf=; expires=Thu, 01 Jan 1970 00:00:00 GMT'
})

describe('a successful call', () => {
  it('sends the credentials and returns the session', async () => {
    const session = {
      user_id: '018f-1',
      login_name: 'wang.li',
      display_name: '王丽',
      expires_at: '2026-09-07T13:00:00+00:00',
    }
    const stub = stubFetch(respond(201, session, 'application/json'))

    await expect(openSession(CREDENTIALS)).resolves.toEqual(session)

    const [path, options] = stub.mock.calls[0]!
    // ADR-0003: the fixed literal prefix.
    expect(path).toBe('/api/v1/auth/session')
    expect(options.body).toBe(JSON.stringify(CREDENTIALS))
    // The session is a cookie, so it has to be sent; `same-origin` is what §六's single origin
    // allows.
    expect(options.credentials).toBe('same-origin')
  })

  it('carries the CSRF token from the cookie into the header on a modifying request', async () => {
    const stub = stubFetch(respond(204, null))

    await endSession()

    const [, options] = stub.mock.calls[0]!
    expect((options.headers as Record<string, string>)['x-csrf-token']).toBe('derived-token')
  })

  it('does not send a CSRF token on a read', async () => {
    const stub = stubFetch(respond(200, { login_name: 'wang.li' }, 'application/json'))

    await readSession()

    const [, options] = stub.mock.calls[0]!
    expect((options.headers as Record<string, string>)['x-csrf-token']).toBeUndefined()
  })
})

describe('a refusal', () => {
  it('surfaces the stable error code and the displayable title', async () => {
    stubFetch(
      respond(401, {
        title: '登录名或密码不正确',
        status: 401,
        error_code: 'CREDENTIALS_REJECTED',
      }),
    )

    await expect(openSession(CREDENTIALS)).rejects.toMatchObject({
      errorCode: 'CREDENTIALS_REJECTED',
      message: '登录名或密码不正确',
      status: 401,
    })
  })

  it('keeps field errors as data so a form can place each message', async () => {
    stubFetch(
      respond(422, {
        title: '提交的内容不合要求',
        error_code: 'REQUEST_INVALID',
        field_errors: [{ field: 'password', message: '必填' }],
      }),
    )

    await expect(openSession(CREDENTIALS)).rejects.toMatchObject({
      fieldErrors: [{ field: 'password', message: '必填' }],
    })
  })

  it('displays an unrecognized error code by its title rather than failing on it', async () => {
    // §5.15: every client needs a fallback for a code it does not know. The enumeration grows by
    // addition, so this is the ordinary case after any backend release, not an edge case.
    stubFetch(respond(409, { title: '模板版本已被绑定', error_code: 'SOME_FUTURE_REFUSAL' }))

    const error = await openSession(CREDENTIALS).catch((caught: unknown) => caught)

    expect(error).toBeInstanceOf(ControlPlaneError)
    expect((error as ControlPlaneError).errorCode).toBe('SOME_FUTURE_REFUSAL')
    expect((error as ControlPlaneError).message).toBe('模板版本已被绑定')
  })

  it('falls back to a generic message when the body is not a problem document', async () => {
    // An Nginx error page, or a 502 with an HTML body. Without this the operator gets a blank
    // screen or a parse error.
    stubFetch(new Response('<html>502 Bad Gateway</html>', { status: 502 }))

    await expect(readSession()).rejects.toMatchObject({
      errorCode: 'UNKNOWN',
      message: '请求未能完成，请稍后重试',
      status: 502,
    })
  })

  it('reports an unreachable center as such', async () => {
    stubFetch(new TypeError('Failed to fetch'))

    await expect(readSession()).rejects.toMatchObject({
      errorCode: 'NETWORK_UNREACHABLE',
      status: 0,
    })
  })
})
