/**
 * Routes, and the one guard that decides whether a caller may see them.
 *
 * Navigation is the five items §5.4 fixes. 概览、工位与设备 and 用户与权限 have views; the other
 * two arrive with their own tickets, and the shell shows them as not yet available rather than
 * linking to an empty page.
 */

import { createRouter, createWebHistory, type RouteRecordRaw } from 'vue-router'

import { useSessionStore } from '@/session/store'

declare module 'vue-router' {
  interface RouteMeta {
    /** Reachable without a session. Only the login and not-found pages. */
    anonymous?: boolean
    /** The document title fragment for the page. */
    title?: string
    /** Permissions any one of which makes the page reachable. Declared so the guard's check is
     * typed rather than reading an untyped `meta` bag — and so a route naming a permission that
     * does not exist fails nothing here but simply never matches, which the backend refuses
     * anyway; the string must be one the backend registers. */
    requires?: string[]
  }
}

export const LOGIN_ROUTE = 'login'
export const OVERVIEW_ROUTE = 'overview'
export const DEVICES_ROUTE = 'devices'
export const ACCESS_ROUTE = 'access'
export const NOT_FOUND_ROUTE = 'not-found'

const routes: RouteRecordRaw[] = [
  {
    path: '/login',
    name: LOGIN_ROUTE,
    component: () => import('@/session/LoginView.vue'),
    meta: { anonymous: true, title: '登录' },
  },
  {
    path: '/',
    component: () => import('@/shell/AppShell.vue'),
    children: [
      {
        path: '',
        name: OVERVIEW_ROUTE,
        component: () => import('@/modules/overview/OverviewView.vue'),
        meta: { title: '概览' },
      },
      {
        path: 'devices',
        name: DEVICES_ROUTE,
        component: () => import('@/modules/devices/DevicesView.vue'),
        meta: {
          title: '工位与设备',
          requires: ['device.connector.view', 'device.connector.edit', 'device.connector.delete'],
        },
      },
      {
        path: 'access',
        name: ACCESS_ROUTE,
        component: () => import('@/modules/access/AccessView.vue'),
        // `requires` is checked by the guard below against the caller's own permission list. A
        // deep link to a page the caller may not see is answered here rather than by the page
        // rendering an empty table — and either way the backend refuses the calls behind it.
        meta: { title: '用户与权限', requires: ['auth.user.view', 'auth.role.view'] },
      },
    ],
  },
  // Anything else is not a page, and says so rather than redirecting: a stale bookmark sent to
  // the overview would dress the miss up as a success. Anonymous, because a bookmark outlives
  // the session that made it, and finding that out is not worth signing in for.
  {
    path: '/:pathMatch(.*)*',
    name: NOT_FOUND_ROUTE,
    component: () => import('@/NotFoundView.vue'),
    meta: { anonymous: true, title: '页面不存在' },
  },
]

export function createAppRouter() {
  const router = createRouter({ history: createWebHistory(), routes })

  router.beforeEach(async (to) => {
    const session = useSessionStore()
    // Asked once per page load. Until it has been answered, "is there a session" is unknown, and
    // guessing 'no' would bounce a deep link that was perfectly valid.
    if (!session.settled) {
      await session.restore()
    }

    const anonymous = to.meta.anonymous === true
    if (!session.current && !anonymous) {
      // The intended path is carried so the login can return the operator to it — being sent to
      // the overview after signing in loses whatever they had opened.
      return { name: LOGIN_ROUTE, query: { next: to.fullPath } }
    }
    if (session.current && to.name === LOGIN_ROUTE) {
      // Signed-in callers do not see the login form. The rule is about the login page itself,
      // not about every anonymous route: the not-found page is anonymous too, and a signed-in
      // operator with a stale bookmark still needs its answer.
      return { name: OVERVIEW_ROUTE }
    }
    // A page whose permissions the caller holds none of goes to the overview rather than
    // rendering an empty shell — the navigation never shows such an item, so reaching one here
    // means a hand-typed URL, and the honest answer is "nothing is here for you".
    const requires = to.meta.requires
    if (requires !== undefined && !requires.some((permission) => session.may(permission))) {
      return { name: OVERVIEW_ROUTE }
    }
    return true
  })

  return router
}
