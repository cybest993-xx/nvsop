/**
 * The control-plane client: what a caller is told when a request fails.
 *
 * `fetch` is replaced, which is the seam — everything above it is this application's own code and
 * everything below it is the network. §5.15's unknown-value fallback lives here, so the case that
 * matters most is a response this client has never seen a code for.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import {
  ControlPlaneError,
  bindTemplateVersion,
  createConnector,
  deleteConnector,
  editConnector,
  endSession,
  enqueueConnectorConnectionTest,
  downloadTemplateVersionArtifact,
  readTemplateVersions,
  readConnector,
  readConnectors,
  readDeviceCommand,
  readInferenceHosts,
  readSession,
  readStationTemplateConfiguration,
  readStations,
  openSession,
  updateStationRuntimeParameters,
  validateTemplateBinding,
  publishTemplateVersion,
  readTemplateVersion,
  setConnectorStatus,
} from '@/api/controlPlane'

const CREDENTIALS = { login_name: 'wang.li', password: 'assembly-line-3' } // pragma: allowlist secret

function respond(
  status: number,
  body: unknown,
  contentType = 'application/problem+json',
): Response {
  return new Response(status === 204 ? null : JSON.stringify(body), {
    status,
    headers: { 'content-type': contentType },
  })
}

function stubFetch(response: Response | Error) {
  // The parameters are declared so `stub.mock.calls[0]` is typed as this pair. Without them the
  // record is an empty tuple and every read of it needs a cast, which is a cast asserting the
  // very thing the assertion below is about (TS2352).
  const stub = vi.fn((_request: Request) =>
    response instanceof Error ? Promise.reject(response) : Promise.resolve(response),
  )
  vi.stubGlobal('fetch', stub)
  return stub
}

beforeEach(() => {
  document.cookie = 'sop_csrf=derived-token'
})

afterEach(() => {
  vi.unstubAllGlobals()
  document.cookie = 'sop_csrf=; expires=Thu, 01 Jan 1970 00:00:00 GMT'
})

describe('a successful call', () => {
  it('sends the credentials and returns the session', async () => {
    const session = {
      user_id: '018f-1',
      login_name: 'wang.li',
      display_name: '王丽',
      expires_at: '2026-09-07T13:00:00+00:00',
    }
    const stub = stubFetch(respond(201, session, 'application/json'))

    await expect(openSession(CREDENTIALS)).resolves.toEqual(session)

    const [request] = stub.mock.calls[0]!
    // ADR-0003: the generated SDK carries the fixed literal prefix.
    expect(new URL(request.url).pathname).toBe('/api/v1/auth/session')
    await expect(request.clone().text()).resolves.toBe(JSON.stringify(CREDENTIALS))
    // The session is a cookie, so it has to be sent; `same-origin` is what §六's single origin
    // allows.
    expect(request.credentials).toBe('same-origin')
  })

  it('carries the CSRF token from the cookie into the header on a modifying request', async () => {
    const stub = stubFetch(respond(204, null))

    await endSession()

    const [request] = stub.mock.calls[0]!
    expect(request.headers.get('x-csrf-token')).toBe('derived-token')
  })

  it('does not send a CSRF token on a read', async () => {
    const stub = stubFetch(respond(200, { login_name: 'wang.li' }, 'application/json'))

    await readSession()

    const [request] = stub.mock.calls[0]!
    expect(request.headers.get('x-csrf-token')).toBeNull()
  })
})

describe('connector calls', () => {
  const CONNECTOR = {
    id: 'connector-1',
    station_id: 'station-1',
    host_id: 'host-1',
    name: '装配线输入',
    connector_type: 'hikvision_isapi' as const,
    configuration: { address: '192.168.10.21', port: 80 },
    credentials_configured: false,
    reachability: 'unverified',
    health_detail: null,
    status: 'active' as const,
    revision: 3,
  }

  it('uses the generated list and detail endpoints', async () => {
    const stub = stubFetch(
      respond(200, { items: [CONNECTOR], page: 1, page_size: 50, total: 1 }, 'application/json'),
    )
    await readConnectors()
    expect(new URL(stub.mock.calls[0]![0].url).pathname).toBe('/api/v1/connectors')

    stub.mockResolvedValueOnce(respond(200, CONNECTOR, 'application/json'))
    await readConnector('connector-1')
    expect(new URL(stub.mock.calls[1]![0].url).pathname).toBe('/api/v1/connectors/connector-1')
  })

  it('uses the host and station lookup endpoints', async () => {
    const stub = stubFetch(
      respond(200, { items: [], page: 1, page_size: 50, total: 0 }, 'application/json'),
    )
    await readInferenceHosts()
    expect(new URL(stub.mock.calls[0]![0].url).pathname).toBe('/api/v1/inference-hosts')

    stub.mockResolvedValueOnce(
      respond(200, { items: [], page: 1, page_size: 50, total: 0 }, 'application/json'),
    )
    await readStations()
    expect(new URL(stub.mock.calls[1]![0].url).pathname).toBe('/api/v1/stations')
  })

  it('sends real connector writes with non-secret data and If-Match', async () => {
    const stub = stubFetch(respond(201, CONNECTOR, 'application/json'))
    const placement = {
      name: '装配线输入',
      connector_type: 'hikvision_isapi' as const,
      configuration: { address: '192.168.10.21', port: 80 },
      station_id: 'station-1',
      host_id: 'host-1',
    }

    await createConnector(placement)
    stub.mockResolvedValueOnce(respond(200, CONNECTOR, 'application/json'))
    await editConnector('connector-1', placement, 3)
    stub.mockResolvedValueOnce(respond(200, CONNECTOR, 'application/json'))
    await setConnectorStatus('connector-1', 'deactivated', 3)
    stub.mockResolvedValueOnce(respond(204, null))
    await deleteConnector('connector-1', 3)

    const [createRequest, editRequest, statusRequest, deleteRequest] = stub.mock.calls.map(
      ([request]) => request,
    )
    expect(createRequest!.method).toBe('POST')
    await expect(createRequest!.clone().json()).resolves.toEqual(placement)
    expect(editRequest!.headers.get('If-Match')).toBe('3')
    expect(statusRequest!.headers.get('If-Match')).toBe('3')
    await expect(statusRequest!.clone().json()).resolves.toEqual({ status: 'deactivated' })
    expect(deleteRequest!.method).toBe('DELETE')
    expect(deleteRequest!.headers.get('If-Match')).toBe('3')
  })
})

describe('template version calls', () => {
  const VERSION = {
    id: 'version-1',
    template_id: 'template-1',
    source_import_id: 'import-1',
    source_draft_id: 'draft-1',
    source_draft_revision: 3,
    steps: [{ number: 1, name: '取料', description: '(1)取料' }],
    ordering: 'strict' as const,
    start_signal: { kind: 'action' as const, action_number: 1 },
    end_signals: [],
    runtime_defaults: {
      idle_timeout_seconds: 30,
      step_deadline_seconds: 90,
      disposition_policy: 'record',
    },
    artifacts: [],
    sha256: 'a'.repeat(64),
    published_by: 'operator-1',
    published_at: '2026-09-08T01:00:00Z',
  }

  it('uses generated publish, history, detail and artifact endpoints', async () => {
    const stub = stubFetch(respond(201, VERSION, 'application/json'))

    await publishTemplateVersion('draft-1', 3)
    stub.mockResolvedValueOnce(
      respond(200, { items: [VERSION], page: 1, page_size: 50, total: 1 }, 'application/json'),
    )
    await readTemplateVersions()
    stub.mockResolvedValueOnce(respond(200, VERSION, 'application/json'))
    await readTemplateVersion('version-1')
    stub.mockResolvedValueOnce(
      new Response('artifact', { status: 200, headers: { 'content-type': 'application/json' } }),
    )
    await downloadTemplateVersionArtifact('version-1', 'actions.json')

    const [publishRequest, listRequest, readRequest, artifactRequest] = stub.mock.calls.map(
      ([request]) => request,
    )
    expect(new URL(publishRequest!.url).pathname).toBe('/api/v1/templates/drafts/draft-1/publish')
    expect(publishRequest!.headers.get('If-Match')).toBe('3')
    expect(new URL(listRequest!.url).pathname).toBe('/api/v1/templates/versions')
    expect(new URL(readRequest!.url).pathname).toBe('/api/v1/templates/versions/version-1')
    expect(new URL(artifactRequest!.url).pathname).toBe(
      '/api/v1/templates/versions/version-1/artifacts/actions.json',
    )
  })
})

describe('delegated connection-test calls', () => {
  const COMMAND = {
    id: 'command-1',
    host_id: 'host-1',
    command_type: 'test_connector_connection',
    target_id: 'connector-1',
    target_revision: 3,
    idempotency_key: 'retry-key',
    status: 'pending',
    attempt: 0,
    claimed_at: null,
    lease_expires_at: null,
    result: null,
    result_detail: null,
    failure_code: null,
    completed_at: null,
    created_by: 'operator-1',
    created_at: '2026-09-08T08:00:00Z',
    updated_at: '2026-09-08T08:00:00Z',
  }

  it('uses the generated enqueue and read endpoints with the idempotency key', async () => {
    const stub = stubFetch(respond(202, COMMAND, 'application/json'))

    await enqueueConnectorConnectionTest('connector-1', 'retry-key')
    stub.mockResolvedValueOnce(respond(200, COMMAND, 'application/json'))
    await readDeviceCommand('command-1')

    const [enqueueRequest, readRequest] = stub.mock.calls.map(([request]) => request)
    expect(new URL(enqueueRequest!.url).pathname).toBe(
      '/api/v1/connectors/connector-1/connection-test',
    )
    expect(enqueueRequest!.headers.get('Idempotency-Key')).toBe('retry-key')
    expect(enqueueRequest!.method).toBe('POST')
    expect(new URL(readRequest!.url).pathname).toBe('/api/v1/device-commands/command-1')
    expect(readRequest!.method).toBe('GET')
  })
})

describe('station template configuration calls', () => {
  const binding = {
    station_id: 'station-1',
    version_id: 'version-1',
    runtime_parameter_mode: 'custom' as const,
    runtime_parameters: {
      idle_timeout_seconds: 45,
      step_deadline_seconds: 120,
      disposition_policy: 'record',
    },
  }
  const configuration = {
    station_id: 'station-1',
    station_revision: 8,
    runtime_parameters_revision: 2,
    runtime_parameter_mode: 'custom' as const,
    template_defaults: {
      idle_timeout_seconds: 30,
      step_deadline_seconds: 90,
      disposition_policy: 'record',
    },
    runtime_overrides: binding.runtime_parameters,
    effective_runtime_parameters: binding.runtime_parameters,
    desired: null,
    version: null,
    status: 'unbound' as const,
    status_detail: null,
    topology_issues: [],
    backends: [],
  }

  it('uses the generated binding and runtime configuration endpoints', async () => {
    const stub = stubFetch(respond(200, { ...configuration, accepted: true }, 'application/json'))

    await validateTemplateBinding(binding)
    stub.mockResolvedValueOnce(respond(200, configuration, 'application/json'))
    await bindTemplateVersion(binding, 8)
    stub.mockResolvedValueOnce(respond(200, configuration, 'application/json'))
    await readStationTemplateConfiguration('station-1')
    stub.mockResolvedValueOnce(respond(200, configuration, 'application/json'))
    await updateStationRuntimeParameters(
      'station-1',
      { mode: 'custom', parameters: binding.runtime_parameters },
      8,
    )

    const [previewRequest, bindRequest, readRequest, runtimeRequest] = stub.mock.calls.map(
      ([request]) => request,
    )
    expect(previewRequest!.method).toBe('POST')
    expect(new URL(previewRequest!.url).pathname).toBe('/api/v1/templates/bindings/validate')
    await expect(previewRequest!.clone().json()).resolves.toEqual(binding)
    expect(new URL(bindRequest!.url).pathname).toBe('/api/v1/templates/bindings')
    expect(bindRequest!.headers.get('If-Match')).toBe('8')
    await expect(bindRequest!.clone().json()).resolves.toEqual(binding)
    expect(new URL(readRequest!.url).pathname).toBe(
      '/api/v1/templates/stations/station-1/configuration',
    )
    expect(new URL(runtimeRequest!.url).pathname).toBe(
      '/api/v1/templates/stations/station-1/runtime-parameters',
    )
    expect(runtimeRequest!.headers.get('If-Match')).toBe('8')
    await expect(runtimeRequest!.clone().json()).resolves.toEqual({
      mode: 'custom',
      parameters: binding.runtime_parameters,
    })
  })
})

describe('a refusal', () => {
  it('surfaces the stable error code and the displayable title', async () => {
    stubFetch(
      respond(401, {
        title: '登录名或密码不正确',
        status: 401,
        error_code: 'CREDENTIALS_REJECTED',
      }),
    )

    await expect(openSession(CREDENTIALS)).rejects.toMatchObject({
      errorCode: 'CREDENTIALS_REJECTED',
      message: '登录名或密码不正确',
      status: 401,
    })
  })

  it('keeps field errors as data so a form can place each message', async () => {
    stubFetch(
      respond(422, {
        title: '提交的内容不合要求',
        error_code: 'REQUEST_INVALID',
        field_errors: [{ field: 'password', message: '必填' }],
      }),
    )

    await expect(openSession(CREDENTIALS)).rejects.toMatchObject({
      fieldErrors: [{ field: 'password', message: '必填' }],
    })
  })

  it('displays an unrecognized error code by its title rather than failing on it', async () => {
    // §5.15: every client needs a fallback for a code it does not know. The enumeration grows by
    // addition, so this is the ordinary case after any backend release, not an edge case.
    stubFetch(respond(409, { title: '模板版本已被绑定', error_code: 'SOME_FUTURE_REFUSAL' }))

    const error = await openSession(CREDENTIALS).catch((caught: unknown) => caught)

    expect(error).toBeInstanceOf(ControlPlaneError)
    expect((error as ControlPlaneError).errorCode).toBe('SOME_FUTURE_REFUSAL')
    expect((error as ControlPlaneError).message).toBe('模板版本已被绑定')
  })

  it('falls back to a generic message when the body is not a problem document', async () => {
    // An Nginx error page, or a 502 with an HTML body. Without this the operator gets a blank
    // screen or a parse error.
    stubFetch(new Response('<html>502 Bad Gateway</html>', { status: 502 }))

    await expect(readSession()).rejects.toMatchObject({
      errorCode: 'UNKNOWN',
      message: '请求未能完成，请稍后重试',
      status: 502,
    })
  })

  it('reports an unreachable center as such', async () => {
    stubFetch(new TypeError('Failed to fetch'))

    await expect(readSession()).rejects.toMatchObject({
      errorCode: 'NETWORK_UNREACHABLE',
      status: 0,
    })
  })
})
