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
