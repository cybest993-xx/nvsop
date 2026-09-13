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
  setUnauthorizedHandler,
  type SessionView,
} from '@/api/controlPlane'

export const useSessionStore = defineStore('session', () => {
  const current = ref<SessionView | null>(null)
  /** Whether `restore` has run. The router guard waits for it before deciding anything, so a
   * deep link opened in a fresh tab is not bounced to the login page while the answer is still
   * in flight. */
  const settled = ref(false)
  /** Why the last `restore` could not say who is signed in, when the reason was a fault rather
   * than anonymity. The login page shows it as its refusal: a shell that reduced a server
   * failure to a fresh login form would leave the only clue in the fact that signing in fails
   * again. */
  const fault = ref<string | null>(null)

  // Registered once per store creation, before any screen can issue a call. From here on, a
  // 401 from any request — including ones this store never made — clears the cached identity,
  // so the shell stops rendering buttons for a session the backend has already revoked.
  setUnauthorizedHandler(() => {
    current.value = null
    fault.value = null
  })

  async function restore(): Promise<void> {
    try {
      current.value = await readSession()
      fault.value = null
    } catch (error) {
      if (!(error instanceof ControlPlaneError)) {
        throw error
      }
      current.value = null
      // A 401 here is the ordinary case, not a fault: nobody is signed in. Any other status is
      // the backend or the network saying something broke, and the two must stay
      // distinguishable — that is what §5.15's unknown-error fallback is for.
      fault.value = error.status === 401 ? null : error.message
    } finally {
      settled.value = true
    }
  }

  /** Re-read who is signed in and what they may do.
   *
   * Administration screens call this after any operation that could change the caller's own
   * authorization — reassigning their roles, editing a role they hold, deactivating themselves.
   * The backend already enforces the new state; re-reading it is what stops the screen from
   * rendering buttons for permissions the caller held one request ago. A 401 has already been
   * handled by the hook above, so this only ever widens or narrows what the screen shows. */
  async function refreshIdentity(): Promise<void> {
    try {
      current.value = await readSession()
      fault.value = null
    } catch {
      // A 401 went through the unauthorized handler, which cleared the identity; anything
      // else is a fault the next request will surface again. Nothing to add here.
    }
  }

  async function logIn(credentials: { login_name: string; password: string }): Promise<void> {
    // A new attempt supersedes whatever the last restore reported; the form's own refusal
    // takes over from here.
    fault.value = null
    current.value = await openSession(credentials)
    settled.value = true
  }

  async function logOut(): Promise<void> {
    // Only the backend can revoke the HttpOnly cookie's server-side session. Until it confirms
    // that revocation, keep the cached identity: clearing it would present a false success and
    // the next restore would sign the operator straight back in.
    await endSession()
    current.value = null
  }

  /** Whether the signed-in caller holds `permission`.
   *
   * Used to decide whether a navigation item or a button exists. It is not a security boundary —
   * the backend checks in the use case whatever this returns — but a screen that offers an action
   * the backend will refuse is worse than one that does not offer it, because the operator only
   * finds out after filling the form in. */
  function may(permission: string): boolean {
    return current.value?.permissions?.includes(permission) ?? false
  }

  return { current, settled, fault, restore, refreshIdentity, logIn, logOut, may }
})
