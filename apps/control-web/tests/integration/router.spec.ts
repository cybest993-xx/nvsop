/**
 * The guard: what an anonymous caller gets, and what a signed-in one gets.
 *
 * The router is real and the control-plane client is replaced at its module boundary — the guard's
 * decisions are about what the backend answered, and standing up a backend to say "401" would make
 * the suite that proves the redirect the slow one.
 */

import { setActivePinia, createPinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { ControlPlaneError } from '@/api/controlPlane'
import { createAppRouter } from '@/router'

const { readSession } = vi.hoisted(() => ({ readSession: vi.fn() }))

vi.mock('@/api/controlPlane', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/controlPlane')>()),
  readSession,
}))

const SESSION = {
  user_id: '018f-1',
  login_name: 'wang.li',
  display_name: '王丽',
  expires_at: '2026-09-07T13:00:00+00:00',
  // C2.2 added this. Empty by default: an account holding nothing is what proves the anonymous and
  // restore paths do not depend on a permission. The pruning cases below pass their own.
  permissions: [] as string[],
}

function anonymous() {
  readSession.mockRejectedValue(
    new ControlPlaneError({
      message: '请先登录',
      errorCode: 'AUTHENTICATION_REQUIRED',
      status: 401,
    }),
  )
}

beforeEach(() => {
  setActivePinia(createPinia())
  readSession.mockReset()
})

describe('an anonymous caller', () => {
  it('is sent to the login page when asking for a protected route', async () => {
    anonymous()
    const router = createAppRouter()

    await router.push('/')
    await router.isReady()

    expect(router.currentRoute.value.name).toBe('login')
  })

  it('is returned to the page they asked for after signing in', async () => {
    // Being dropped on the overview loses whatever the operator had opened, which on a deep link
    // from a colleague's message is the whole point of the link. The path has to be one the
    // router recognizes: an unknown path is rewritten by the catch-all before the guard sees it,
    // so there is nothing left to carry — correct, because that path was never a page.
    anonymous()
    const router = createAppRouter()

    await router.push('/?station=A12')

    expect(router.currentRoute.value.name).toBe('login')
    expect(router.currentRoute.value.query.next).toBe('/?station=A12')
  })

  it('reaches the login page itself', async () => {
    anonymous()
    const router = createAppRouter()

    await router.push('/login')

    expect(router.currentRoute.value.name).toBe('login')
  })
})

describe('a caller whose session is restored', () => {
  it('reaches the protected route', async () => {
    // §六 stores sessions server-side, so a reopened browser is signed in without having kept
    // anything locally: the guard asks, and this is the answer.
    readSession.mockResolvedValue(SESSION)
    const router = createAppRouter()

    await router.push('/')

    expect(router.currentRoute.value.name).toBe('overview')
  })

  it('is redirected away from the login page', async () => {
    readSession.mockResolvedValue(SESSION)
    const router = createAppRouter()

    await router.push('/login')

    expect(router.currentRoute.value.name).toBe('overview')
  })

  it('is asked about only once per page load', async () => {
    // The guard runs on every navigation; re-asking would put a request on the path of each one.
    readSession.mockResolvedValue(SESSION)
    const router = createAppRouter()

    await router.push('/')
    await router.push('/login')
    await router.push('/')

    expect(readSession).toHaveBeenCalledTimes(1)
  })
})

describe('an unknown path', () => {
  it('is answered by the not-found page, not a redirect to the overview', async () => {
    // A redirect would dress a miss up as a success: the overview cannot say what went wrong
    // because nothing went wrong with it. The miss gets its own answer — what was not found,
    // and the way back.
    readSession.mockResolvedValue(SESSION)
    const router = createAppRouter()

    await router.push('/not/a/page')

    expect(router.currentRoute.value.name).toBe('not-found')
  })

  it('answers the same way when nobody is signed in', async () => {
    // A stale bookmark outlives the session that made it. Sending it to the login first would
    // make the operator sign in to be told the page is gone.
    anonymous()
    const router = createAppRouter()

    await router.push('/not/a/page')

    expect(router.currentRoute.value.name).toBe('not-found')
  })
})

describe('a page that names required permissions', () => {
  it('reaches the device page for a connector viewer', async () => {
    readSession.mockResolvedValue({
      ...SESSION,
      permissions: ['device.connector.view'],
    })
    const router = createAppRouter()

    await router.push('/devices')
    await router.isReady()

    expect(router.currentRoute.value.name).toBe('devices')
  })

  it.each(['device.connector.edit', 'device.connector.delete'])(
    'reaches the device page for a caller holding only %s',
    async (permission) => {
      readSession.mockResolvedValue({ ...SESSION, permissions: [permission] })
      const router = createAppRouter()

      await router.push('/devices')
      await router.isReady()

      expect(router.currentRoute.value.name).toBe('devices')
    },
  )

  it('sends a caller without any connector permission away from the device page', async () => {
    readSession.mockResolvedValue({ ...SESSION, permissions: [] })
    const router = createAppRouter()

    await router.push('/devices')
    await router.isReady()

    expect(router.currentRoute.value.name).toBe('overview')
  })

  it.each(['template.draft.view', 'template.draft.edit'])(
    'reaches the template page for a caller holding only %s',
    async (permission) => {
      readSession.mockResolvedValue({ ...SESSION, permissions: [permission] })
      const router = createAppRouter()

      await router.push('/templates')
      await router.isReady()

      expect(router.currentRoute.value.name).toBe('templates')
    },
  )

  it('sends a caller without a template permission away from the template page', async () => {
    readSession.mockResolvedValue({ ...SESSION, permissions: [] })
    const router = createAppRouter()

    await router.push('/templates')
    await router.isReady()

    expect(router.currentRoute.value.name).toBe('overview')
  })

  it.each(['dataset.dataset.view', 'dataset.dataset.import'])(
    'reaches the training-dataset page for a caller holding only %s',
    async (permission) => {
      readSession.mockResolvedValue({ ...SESSION, permissions: [permission] })
      const router = createAppRouter()

      await router.push('/training-datasets')
      await router.isReady()

      expect(router.currentRoute.value.name).toBe('datasets')
    },
  )

  it('sends a caller without a dataset permission away from the training-dataset page', async () => {
    readSession.mockResolvedValue({ ...SESSION, permissions: [] })
    const router = createAppRouter()

    await router.push('/training-datasets')
    await router.isReady()

    expect(router.currentRoute.value.name).toBe('overview')
  })

  it('is reachable by a caller holding one of them', async () => {
    // Any one, not all: 用户与权限 is useful to someone who may read accounts but not roles, and the
    // page renders each half according to what they hold.
    readSession.mockResolvedValue({ ...SESSION, permissions: ['auth.user.view'] })
    const router = createAppRouter()

    await router.push('/access')
    await router.isReady()

    expect(router.currentRoute.value.name).toBe('access')
  })

  it('is reachable by a caller holding only the other one', async () => {
    readSession.mockResolvedValue({ ...SESSION, permissions: ['auth.role.view'] })
    const router = createAppRouter()

    await router.push('/access')
    await router.isReady()

    expect(router.currentRoute.value.name).toBe('access')
  })

  it('sends a caller holding neither to the overview', async () => {
    // A deep link or a stale bookmark. Answered by the guard rather than by the page rendering two
    // empty tabs — and the backend refuses the calls behind it either way.
    readSession.mockResolvedValue({ ...SESSION, permissions: [] })
    const router = createAppRouter()

    await router.push('/access')
    await router.isReady()

    expect(router.currentRoute.value.name).toBe('overview')
  })

  it('sends a caller holding an unrelated permission to the overview', async () => {
    readSession.mockResolvedValue({ ...SESSION, permissions: ['auth.user.edit'] })
    const router = createAppRouter()

    await router.push('/access')
    await router.isReady()

    // `edit` does not imply `view`, in the guard as in the backend: there is no implication between
    // members, so a caller who may change an account but was never granted the read cannot open the
    // page. The same pair of permissions is checked the same way on both sides.
    expect(router.currentRoute.value.name).toBe('overview')
  })
})
