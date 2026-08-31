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
  // Anything else is not a page. Sent to the overview rather than to a 404 view: every path this
  // application serves is one it generated itself, so a miss is a stale bookmark.
  { path: '/:pathMatch(.*)*', redirect: { name: OVERVIEW_ROUTE } },
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
    if (session.current && anonymous) {
      return { name: OVERVIEW_ROUTE }
    }
    return true
  })

  return router
}
