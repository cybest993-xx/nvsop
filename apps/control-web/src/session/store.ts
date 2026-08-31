/**
 * The one place this application keeps who is signed in.
 *
 * There is no token here and nothing in `localStorage` (§六). The session is the `HttpOnly`
 * cookie the browser holds and the row the backend holds; this store caches only what
 * `GET /auth/session` reported, so a reload restores it by asking rather than by remembering.
 */

import { defineStore } from 'pinia'
import { ref } from 'vue'

import {
  ControlPlaneError,
  endSession,
  openSession,
  readSession,
  type SessionView,
} from '@/api/controlPlane'

export const useSessionStore = defineStore('session', () => {
  const current = ref<SessionView | null>(null)
  /** Whether `restore` has run. The router guard waits for it before deciding anything, so a
   * deep link opened in a fresh tab is not bounced to the login page while the answer is still
   * in flight. */
  const settled = ref(false)

  async function restore(): Promise<void> {
    try {
      current.value = await readSession()
    } catch (error) {
      // A 401 here is the ordinary case, not a fault: nobody is signed in. Any other failure
      // leaves the caller anonymous too — the guard's decision is the same, and the message is
      // shown by whatever page the guard sends them to.
      if (!(error instanceof ControlPlaneError)) {
        throw error
      }
      current.value = null
    } finally {
      settled.value = true
    }
  }

  async function logIn(credentials: { login_name: string; password: string }): Promise<void> {
    current.value = await openSession(credentials)
    settled.value = true
  }

  async function logOut(): Promise<void> {
    try {
      await endSession()
    } catch (error) {
      // A failed revocation does not keep the operator signed in here. On a shared shop-floor
      // terminal, a shell that still looks signed in after they pressed 退出 is worse than a row
      // the idle timeout will close — and re-raising would leave the caller unable to navigate
      // away, which is exactly that. A fault that is not the control plane's still surfaces.
      if (!(error instanceof ControlPlaneError)) {
        throw error
      }
    } finally {
      current.value = null
    }
  }

  return { current, settled, restore, logIn, logOut }
})
