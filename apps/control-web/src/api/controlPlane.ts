/**
 * Application-facing adapter over the generated OpenAPI SDK.
 *
 * Endpoint paths, methods, request bodies, response bodies, and error shapes come from
 * `src/api/generated/`. This file contains only browser policy the OpenAPI document cannot:
 * same-origin cookies, the CSRF double-submit header, and the operator-facing unknown-error
 * fallback required by §5.15.
 */

import { client } from '@/api/generated/client.gen'
import {
  endSession as generatedEndSession,
  openSession as generatedOpenSession,
  readSession as generatedReadSession,
  type ProblemDocument,
  type SessionView,
} from '@/api/generated'

export type { SessionView } from '@/api/generated'

const CSRF_COOKIE = 'sop_csrf'
const CSRF_HEADER = 'x-csrf-token'
const MODIFYING_METHODS = new Set(['POST', 'PUT', 'PATCH', 'DELETE'])
const GENERIC_MESSAGE = '请求未能完成，请稍后重试'

/** One rejected input, named so a form can put the message beside the right control. */
export interface FieldError {
  field: string
  message: string
}

export class ControlPlaneError extends Error {
  readonly errorCode: string
  readonly status: number
  readonly fieldErrors: FieldError[]

  constructor(options: {
    message: string
    errorCode: string
    status: number
    fieldErrors?: FieldError[]
    cause?: unknown
  }) {
    super(options.message, options.cause === undefined ? undefined : { cause: options.cause })
    this.name = 'ControlPlaneError'
    this.errorCode = options.errorCode
    this.status = options.status
    this.fieldErrors = options.fieldErrors ?? []
  }
}

client.setConfig({ baseUrl: window.location.origin, credentials: 'same-origin' })
client.interceptors.request.use((request) => {
  if (!MODIFYING_METHODS.has(request.method)) {
    return request
  }
  const token = csrfToken()
  if (token === null) {
    return request
  }
  const headers = new Headers(request.headers)
  headers.set(CSRF_HEADER, token)
  return new Request(request, { headers })
})

function csrfToken(): string | null {
  const match = document.cookie.split('; ').find((entry) => entry.startsWith(`${CSRF_COOKIE}=`))
  return match ? decodeURIComponent(match.slice(CSRF_COOKIE.length + 1)) : null
}

interface GeneratedResult<T> {
  data?: T
  error?: unknown
  response?: Response
}

async function execute<T>(request: Promise<GeneratedResult<T>>): Promise<T> {
  const result = await request
  if (result.error !== undefined) {
    throw controlPlaneError(result.error, result.response)
  }
  if (result.response?.status === 204) {
    return undefined as T
  }
  if (result.data === undefined) {
    throw new ControlPlaneError({
      message: GENERIC_MESSAGE,
      errorCode: 'UNKNOWN',
      status: result.response?.status ?? 0,
    })
  }
  return result.data
}

function controlPlaneError(error: unknown, response: Response | undefined): ControlPlaneError {
  if (response === undefined) {
    return new ControlPlaneError({
      message: '无法连接服务器，请检查网络后重试',
      errorCode: 'NETWORK_UNREACHABLE',
      status: 0,
      cause: error,
    })
  }
  const problem = asProblem(error)
  return new ControlPlaneError({
    message: problem?.title ?? GENERIC_MESSAGE,
    errorCode: problem?.error_code ?? 'UNKNOWN',
    status: response.status,
    fieldErrors: problem?.field_errors ?? [],
  })
}

function asProblem(value: unknown): ProblemDocument | null {
  if (typeof value !== 'object' || value === null) {
    return null
  }
  const candidate = value as Partial<ProblemDocument>
  if (typeof candidate.title !== 'string' || typeof candidate.error_code !== 'string') {
    return null
  }
  return candidate as ProblemDocument
}

export function openSession(credentials: {
  login_name: string
  password: string
}): Promise<SessionView> {
  return execute(generatedOpenSession({ body: credentials }))
}

export function readSession(): Promise<SessionView> {
  return execute(generatedReadSession())
}

export function endSession(): Promise<void> {
  return execute(generatedEndSession())
}
