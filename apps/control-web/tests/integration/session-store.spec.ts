/**
 * The session store: what a failed `restore` means, and who is told.
 *
 * The guard's decision needs the store, so the store is where "anonymous" is decided — and the
 * difference between "nobody is signed in" and "the answer could not be had" is exactly what a
 * shell that only renders a login form would erase (§5.15's unknown-error fallback).
 */

import { beforeEach, describe, expect, it, vi } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'

import { useSessionStore } from '@/session/store'

const SESSION = {
  user_id: '018f-1',
  login_name: 'wang.li',
  display_name: '王丽',
  expires_at: '2026-09-07T13:00:00Z',
}

function respond(status: number, body: unknown, contentType = 'application/problem+json') {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': contentType },
  })
}

beforeEach(() => {
  setActivePinia(createPinia())
})

describe('restoring the session', () => {
  it('keeps a refusal to authenticate anonymous without a fault', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() =>
        Promise.resolve(respond(401, { title: '请先登录', error_code: 'AUTHENTICATION_REQUIRED' })),
      ),
    )
    const store = useSessionStore()

    await store.restore()

    expect(store.current).toBeNull()
    expect(store.fault).toBeNull()
    expect(store.settled).toBe(true)
  })

  it('reports a server failure instead of wearing it as anonymity', async () => {
    // A 500 is the backend saying it broke, not the browser saying nobody is home. Treating
    // the two alike sends the operator to a fresh login form as if nothing happened, and the
    // only clue is that signing in fails again.
    vi.stubGlobal(
      'fetch',
      vi.fn(() =>
        Promise.resolve(respond(500, { title: '服务器内部错误', error_code: 'INTERNAL_ERROR' })),
      ),
    )
    const store = useSessionStore()

    await store.restore()

    expect(store.current).toBeNull()
    expect(store.settled).toBe(true)
    expect(store.fault).toBe('服务器内部错误')
  })

  it('reports an unreachable backend rather than calling it a logged-out session', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.reject(new TypeError('fetch failed'))),
    )
    const store = useSessionStore()

    await store.restore()

    expect(store.current).toBeNull()
    expect(store.fault).toBe('无法连接服务器，请检查网络后重试')
  })

  it('clears an earlier fault when a later restore succeeds', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() =>
        Promise.resolve(respond(500, { title: '服务器内部错误', error_code: 'INTERNAL_ERROR' })),
      ),
    )
    const store = useSessionStore()
    await store.restore()
    expect(store.fault).not.toBeNull()

    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.resolve(respond(200, SESSION, 'application/json'))),
    )
    await store.restore()

    expect(store.current).toEqual(SESSION)
    expect(store.fault).toBeNull()
  })
})

describe('what the caller may do', () => {
  // `may` decides which navigation items and buttons exist. It is not a security boundary — the
  // backend's use case checks regardless — but a screen that hides what the operator may do, or
  // offers what it may not, misleads in both directions.
  it('reports a permission the session carries', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() =>
        Promise.resolve(
          respond(200, { ...SESSION, permissions: ['auth.user.edit'] }, 'application/json'),
        ),
      ),
    )
    const store = useSessionStore()
    await store.restore()

    expect(store.may('auth.user.edit')).toBe(true)
    expect(store.may('auth.user.delete')).toBe(false)
  })

  it('reports nothing for an anonymous caller', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() =>
        Promise.resolve(respond(401, { title: '请先登录', error_code: 'AUTHENTICATION_REQUIRED' })),
      ),
    )
    const store = useSessionStore()
    await store.restore()

    expect(store.may('auth.user.view')).toBe(false)
  })

  it('reports nothing when a response predates the permission list', async () => {
    // The wire schema marks `permissions` optional so an old client keeps parsing new responses;
    // the reverse — this front end against a response without the field — must also stay safe.
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.resolve(respond(200, SESSION, 'application/json'))),
    )
    const store = useSessionStore()
    await store.restore()

    expect(store.may('auth.user.view')).toBe(false)
  })
})

describe('keeping the cached identity in step with the backend', () => {
  it('re-reads the identity and the permissions with it', async () => {
    // The administration screens re-read after any change that could alter what the caller may
    // do; `refreshIdentity` is that re-read.
    vi.stubGlobal(
      'fetch',
      vi.fn(() =>
        Promise.resolve(respond(200, { ...SESSION, permissions: [] }, 'application/json')),
      ),
    )
    const store = useSessionStore()
    await store.restore()
    expect(store.may('auth.user.edit')).toBe(false)

    vi.stubGlobal(
      'fetch',
      vi.fn(() =>
        Promise.resolve(
          respond(
            200,
            { ...SESSION, permissions: ['auth.user.edit', 'auth.user.delete'] },
            'application/json',
          ),
        ),
      ),
    )
    await store.refreshIdentity()

    expect(store.may('auth.user.edit')).toBe(true)
    expect(store.may('auth.user.delete')).toBe(true)
  })

  it('clears the identity when any request is answered 401', async () => {
    // A session revoked under the caller — deactivated by another administrator, expired
    // mid-form — must not keep rendering buttons. The unauthorized hook fires for requests this
    // store never made, and the cached identity goes with it.
    vi.stubGlobal(
      'fetch',
      vi.fn(() =>
        Promise.resolve(respond(200, { ...SESSION, permissions: [] }, 'application/json')),
      ),
    )
    const store = useSessionStore()
    await store.restore()
    expect(store.current).not.toBeNull()

    vi.stubGlobal(
      'fetch',
      vi.fn(() =>
        Promise.resolve(
          respond(401, { title: '会话已失效，请重新登录', error_code: 'SESSION_INVALID' }),
        ),
      ),
    )
    await expect(readUsersOnce()).rejects.toThrow()
    await Promise.resolve()

    expect(store.current).toBeNull()
  })
})

/** One call through the generated client's path, so the 401 hook in the adapter runs. */
async function readUsersOnce(): Promise<unknown> {
  const { readUsers } = await import('@/api/controlPlane')
  return readUsers()
}
