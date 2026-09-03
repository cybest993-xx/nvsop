/**
 * The center backend's control plane, as this application sees it.
 *
 * One place parses `application/problem+json`, because §5.15's rule about unknown values has to
 * hold everywhere and a per-caller `catch` would implement it a slightly different way each
 * time. What a caller gets is either the payload or a `ControlPlaneError` carrying a stable
 * `errorCode` and a sentence already fit to display.
 */

// ADR-0003: a fixed literal prefix, not a version axis. There will be no `/api/v2`.
const API_PREFIX = '/api/v1'

// Read from the cookie and echoed back in the header on every modifying request. The session
// cookie itself is `HttpOnly` and is deliberately unreadable from here.
const CSRF_COOKIE = 'sop_csrf'
const CSRF_HEADER = 'x-csrf-token'

const MODIFYING_METHODS = new Set(['POST', 'PUT', 'PATCH', 'DELETE'])

/** RFC 9457, with §5.15's extension members. Every field is optional on the wire. */
interface ProblemDocument {
  title?: string
  status?: number
  detail?: string
  error_code?: string
  field_errors?: { field: string; message: string }[]
}

/** One rejected input, named so a form can put the message beside the right control. */
export interface FieldError {
  field: string
  message: string
}

/** Shown when the response is not a problem document at all — a proxy error page, a dropped
 * connection, a 502 from Nginx. §5.15 requires a fallback rather than a blank screen. */
const GENERIC_MESSAGE = '请求未能完成，请稍后重试'

export class ControlPlaneError extends Error {
  /** The stable `error_code`, or `UNKNOWN` when the response carried none. A caller branches on
   * this; anything it does not recognize it displays by `message` instead (§5.15). */
  readonly errorCode: string
  readonly status: number
  readonly fieldErrors: FieldError[]

  constructor(options: {
    message: string
    errorCode: string
    status: number
    fieldErrors?: FieldError[]
  }) {
    super(options.message)
    this.name = 'ControlPlaneError'
    this.errorCode = options.errorCode
    this.status = options.status
    this.fieldErrors = options.fieldErrors ?? []
  }
}

function csrfToken(): string | null {
  const match = document.cookie.split('; ').find((entry) => entry.startsWith(`${CSRF_COOKIE}=`))
  return match ? decodeURIComponent(match.slice(CSRF_COOKIE.length + 1)) : null
}

async function toError(response: Response): Promise<ControlPlaneError> {
  let problem: ProblemDocument = {}
  try {
    problem = (await response.json()) as ProblemDocument
  } catch {
    // Not JSON: an Nginx error page, or a body that never arrived. The status is all there is.
  }
  return new ControlPlaneError({
    // The backend's `title` is already a displayable Simplified Chinese sentence, which is what
    // lets an `error_code` this application has never seen still produce a usable message.
    message: problem.title ?? GENERIC_MESSAGE,
    errorCode: problem.error_code ?? 'UNKNOWN',
    status: response.status,
    fieldErrors: problem.field_errors ?? [],
  })
}

async function request<T>(
  path: string,
  options: { method: string; body?: unknown } = { method: 'GET' },
): Promise<T> {
  const headers: Record<string, string> = {}
  if (options.body !== undefined) {
    headers['content-type'] = 'application/json'
  }
  if (MODIFYING_METHODS.has(options.method)) {
    const token = csrfToken()
    if (token !== null) {
      headers[CSRF_HEADER] = token
    }
  }

  let response: Response
  try {
    response = await fetch(`${API_PREFIX}${path}`, {
      method: options.method,
      headers,
      // The session lives in a cookie, so it has to be sent. Same origin in both the deployment
      // and `vite dev` (§六), which is what allows `SameSite=strict`.
      credentials: 'same-origin',
      body: options.body === undefined ? null : JSON.stringify(options.body),
    })
  } catch (cause) {
    // The request never reached the backend: the center is down, or the browser is offline.
    throw new ControlPlaneError({
      message: '无法连接服务器，请检查网络后重试',
      errorCode: 'NETWORK_UNREACHABLE',
      status: 0,
      ...(cause instanceof Error ? { cause } : {}),
    })
  }

  if (!response.ok) {
    throw await toError(response)
  }
  if (response.status === 204) {
    return undefined as T
  }
  return (await response.json()) as T
}

/** Who the caller is and when their session ends, as `GET`/`POST /auth/session` reports it. */
export interface SessionView {
  user_id: string
  login_name: string
  display_name: string
  expires_at: string
}

export function openSession(credentials: {
  login_name: string
  password: string
}): Promise<SessionView> {
  return request<SessionView>('/auth/session', { method: 'POST', body: credentials })
}

export function readSession(): Promise<SessionView> {
  return request<SessionView>('/auth/session')
}

export function endSession(): Promise<void> {
  return request<void>('/auth/session', { method: 'DELETE' })
}
