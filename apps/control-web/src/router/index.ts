/**
 * Routes, and the one guard that decides whether a caller may see them.
 *
 * Navigation is the five items §5.4 fixes. Only 概览 has a view in this slice: the other four
 * arrive with their own tickets, and the shell shows them as not yet available rather than
 * linking to an empty page.
 */

import { createRouter, createWebHistory, type RouteRecordRaw } from 'vue-router'

import { useSessionStore } from '@/session/store'

export const LOGIN_ROUTE = 'login'
export const OVERVIEW_ROUTE = 'overview'
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
    return true
  })

  return router
}
