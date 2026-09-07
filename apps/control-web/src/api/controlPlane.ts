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
  createConnector as generatedCreateConnector,
  createRole as generatedCreateRole,
  createUser as generatedCreateUser,
  deleteConnector as generatedDeleteConnector,
  deleteRole as generatedDeleteRole,
  deleteUser as generatedDeleteUser,
  editConnector as generatedEditConnector,
  editRole as generatedEditRole,
  editUser as generatedEditUser,
  endSession as generatedEndSession,
  listConnectors as generatedListConnectors,
  listInferenceHosts as generatedListInferenceHosts,
  listPermissions as generatedListPermissions,
  listRoles as generatedListRoles,
  listStations as generatedListStations,
  listUsers as generatedListUsers,
  openSession as generatedOpenSession,
  readConnector as generatedReadConnector,
  readSession as generatedReadSession,
  resetUserPassword as generatedResetUserPassword,
  setConnectorStatus as generatedSetConnectorStatus,
  setUserRoles as generatedSetUserRoles,
  setUserStatus as generatedSetUserStatus,
  type ConnectorPlacement,
  type ConnectorView,
  type DeviceStatus,
  type ItemPageConnectorView,
  type ItemPageInferenceHostView,
  type ItemPageRoleView,
  type ItemPageStationView,
  type ItemPageStr,
  type ItemPageUserView,
  type ProblemDocument,
  type RoleView,
  type SessionView,
  type StatusChanged,
  type UserView,
} from '@/api/generated'

export type {
  ConnectorPlacement,
  ConnectorView,
  DeviceStatus,
  InferenceHostView,
  ItemPageConnectorView,
  ItemPageInferenceHostView,
  ItemPageStationView,
  RoleView,
  SessionView,
  StationView,
  StatusChanged,
  UserView,
} from '@/api/generated'

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
  /** The backend's specific reason, when it composed one — which login name is taken, which
   * permission would leave the system unadministrable. `message` is the displayable title;
   * `detail` is the sentence under it. Either may be absent, and a screen shows
   * `detail ?? message`. */
  readonly detail: string | null

  constructor(options: {
    message: string
    errorCode: string
    status: number
    fieldErrors?: FieldError[]
    detail?: string | null
    cause?: unknown
  }) {
    super(options.message, options.cause === undefined ? undefined : { cause: options.cause })
    this.name = 'ControlPlaneError'
    this.errorCode = options.errorCode
    this.status = options.status
    this.fieldErrors = options.fieldErrors ?? []
    this.detail = options.detail ?? null
  }
}

/**
 * One hook the session store registers: invoked whenever the backend answers 401, so an identity
 * revoked under the caller (deactivated by another administrator, expired mid-form) clears the
 * cached session everywhere at once instead of every screen learning about 401s on its own.
 * Registered by the store rather than importing it here, which would be a circular import.
 */
let unauthorizedHandler: (() => void) | null = null

export function setUnauthorizedHandler(handler: (() => void) | null): void {
  unauthorizedHandler = handler
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
    const error = controlPlaneError(result.error, result.response)
    // A 401 after sign-in means the identity was taken away under the caller, not that a form
    // was filled in wrong: the hook lets the session store clear it centrally, whatever screen
    // the call came from.
    if (error.status === 401) {
      unauthorizedHandler?.()
    }
    throw error
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
    detail: problem?.detail ?? null,
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

// ——— 工位与设备：页面只通过生成客户端访问控制面。 ———

export function readConnectors(): Promise<ItemPageConnectorView> {
  return execute(generatedListConnectors())
}

export function readConnector(connectorId: string): Promise<ConnectorView> {
  return execute(generatedReadConnector({ path: { connector_id: connectorId } }))
}

export function readInferenceHosts(): Promise<ItemPageInferenceHostView> {
  return execute(generatedListInferenceHosts())
}

export function readStations(): Promise<ItemPageStationView> {
  return execute(generatedListStations())
}

export function createConnector(submitted: ConnectorPlacement): Promise<ConnectorView> {
  return execute(generatedCreateConnector({ body: submitted }))
}

export function editConnector(
  connectorId: string,
  submitted: ConnectorPlacement,
  revision: number,
): Promise<ConnectorView> {
  return execute(
    generatedEditConnector({
      path: { connector_id: connectorId },
      headers: { 'If-Match': revision },
      body: submitted,
    }),
  )
}

export function setConnectorStatus(
  connectorId: string,
  status: DeviceStatus,
  revision: number,
): Promise<ConnectorView> {
  return execute(
    generatedSetConnectorStatus({
      path: { connector_id: connectorId },
      headers: { 'If-Match': revision },
      body: { status },
    }),
  )
}

export function deleteConnector(connectorId: string, revision: number): Promise<void> {
  return execute(
    generatedDeleteConnector({
      path: { connector_id: connectorId },
      headers: { 'If-Match': revision },
    }),
  )
}

// ——— 用户与权限 (C2.2): the administration calls, thin over the generated SDK. ———
// Every one of these names a permission in its OpenAPI metadata; the enforcement is the use
// case's, so this list is only what the screen may offer, never what the backend allows.

export function readUsers(): Promise<ItemPageUserView> {
  return execute(generatedListUsers())
}

export function createUser(submitted: {
  login_name: string
  display_name: string
  password: string
}): Promise<UserView> {
  return execute(generatedCreateUser({ body: submitted }))
}

export function editUser(userId: string, submitted: { display_name: string }): Promise<UserView> {
  return execute(generatedEditUser({ path: { user_id: userId }, body: submitted }))
}

export function resetUserPassword(userId: string, password: string): Promise<void> {
  return execute(generatedResetUserPassword({ path: { user_id: userId }, body: { password } }))
}

export function setUserStatus(
  userId: string,
  status: 'active' | 'deactivated',
): Promise<StatusChanged> {
  return execute(generatedSetUserStatus({ path: { user_id: userId }, body: { status } }))
}

export function setUserRoles(userId: string, roleIds: string[]): Promise<UserView> {
  return execute(generatedSetUserRoles({ path: { user_id: userId }, body: { role_ids: roleIds } }))
}

export function deleteUser(userId: string): Promise<void> {
  return execute(generatedDeleteUser({ path: { user_id: userId } }))
}

export function readRoles(): Promise<ItemPageRoleView> {
  return execute(generatedListRoles())
}

/** The permissions a role may contain. Rendered as the role form's checkboxes, so a permission
 * the backend does not register cannot be offered and one it adds appears without a change
 * here. */
export function readPermissionCatalogue(): Promise<ItemPageStr> {
  return execute(generatedListPermissions())
}

export function createRole(submitted: {
  code: string
  name: string
  permissions: string[]
}): Promise<RoleView> {
  return execute(generatedCreateRole({ body: submitted }))
}

export function editRole(
  roleId: string,
  submitted: { name: string; permissions: string[] },
): Promise<RoleView> {
  return execute(generatedEditRole({ path: { role_id: roleId }, body: submitted }))
}

export function deleteRole(roleId: string): Promise<void> {
  return execute(generatedDeleteRole({ path: { role_id: roleId } }))
}
