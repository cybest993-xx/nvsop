import ElementPlus from 'element-plus'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'

import { ControlPlaneError } from '@/api/controlPlane'
import DevicesView from '@/modules/devices/DevicesView.vue'
import { useSessionStore } from '@/session/store'

const api = vi.hoisted(() => ({
  readConnectors: vi.fn(),
  readConnector: vi.fn(),
  readInferenceHosts: vi.fn(),
  readStations: vi.fn(),
  readPoints: vi.fn(),
  enqueueConnectorConnectionTest: vi.fn(),
  readDeviceCommand: vi.fn(),
}))

vi.mock('@/api/controlPlane', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/controlPlane')>()),
  ...api,
}))

const CONNECTOR = {
  id: 'connector-1',
  station_id: 'station-1',
  host_id: 'host-1',
  name: '装配线输入',
  connector_type: 'hikvision_isapi' as const,
  configuration: { address: '192.168.10.21', port: 80 },
  credentials_configured: true,
  reachability: 'unverified',
  health_detail: null,
  status: 'active' as const,
  revision: 3,
}

let testKeyCounter = 0

const COMMAND = {
  id: 'command-1',
  host_id: 'host-1',
  command_type: 'test_connector_connection' as const,
  target_id: 'connector-1',
  target_revision: 3,
  idempotency_key: 'test-key-1',
  status: 'pending' as const,
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

function setSession(permissions: string[] = ['device.connector.view', 'device.connector.edit']) {
  useSessionStore().current = {
    user_id: 'operator-1',
    login_name: 'operator',
    display_name: '操作员',
    expires_at: '2026-09-08T09:00:00Z',
    permissions,
  }
}

beforeEach(() => {
  setActivePinia(createPinia())
  testKeyCounter = 0
  vi.stubGlobal('crypto', {
    randomUUID: () => `00000000-0000-4000-8000-${String(++testKeyCounter).padStart(12, '0')}`,
    getRandomValues: (bytes: Uint8Array) => bytes,
  })
  vi.clearAllMocks()
  api.readConnectors.mockResolvedValue({ items: [CONNECTOR], page: 1, page_size: 50, total: 1 })
  api.readInferenceHosts.mockResolvedValue({ items: [], page: 1, page_size: 50, total: 0 })
  api.readStations.mockResolvedValue({ items: [], page: 1, page_size: 50, total: 0 })
})

describe('设备页中的连接测试', () => {
  async function mountDevices() {
    setSession()
    const wrapper = mount(DevicesView, { global: { plugins: [ElementPlus] } })
    await flushPromises()
    return wrapper
  }

  it('shows waiting first and then the real reachable result', async () => {
    api.enqueueConnectorConnectionTest.mockResolvedValue(COMMAND)
    api.readDeviceCommand
      .mockResolvedValueOnce({ ...COMMAND, status: 'claimed' })
      .mockResolvedValueOnce({
        ...COMMAND,
        status: 'succeeded',
        result: 'reachable',
        result_detail: '设备响应正常',
        completed_at: '2026-09-08T08:01:00Z',
      })

    const wrapper = await mountDevices()
    await wrapper.find('button[aria-label="测试连接：装配线输入"]').trigger('click')

    expect(wrapper.text()).toContain('连接测试已提交，等待推理机执行')
    await vi.waitFor(() => expect(api.readDeviceCommand).toHaveBeenCalledTimes(2), {
      timeout: 3000,
    })
    await vi.waitFor(() => expect(wrapper.text()).toContain('连接测试成功'), {
      timeout: 3000,
    })
    expect(wrapper.text()).toContain('设备响应正常')
    expect(api.enqueueConnectorConnectionTest).toHaveBeenCalledWith(
      'connector-1',
      expect.any(String),
    )
  })

  it('shows the real unreachable result without treating it as a rejection', async () => {
    api.enqueueConnectorConnectionTest.mockResolvedValue(COMMAND)
    api.readDeviceCommand.mockResolvedValue({
      ...COMMAND,
      status: 'failed',
      result: 'unreachable',
      result_detail: '连接超时',
      completed_at: '2026-09-08T08:01:00Z',
    })

    const wrapper = await mountDevices()
    await wrapper.find('button[aria-label="测试连接：装配线输入"]').trigger('click')

    await vi.waitFor(() => expect(wrapper.text()).toContain('连接测试失败'))
    expect(wrapper.text()).toContain('连接超时')
    expect(wrapper.text()).not.toContain('连接测试被拒绝')
  })

  it('shows a rejected command reason and preserves the connector health state', async () => {
    api.enqueueConnectorConnectionTest.mockResolvedValue(COMMAND)
    api.readDeviceCommand.mockResolvedValue({
      ...COMMAND,
      status: 'rejected',
      result: null,
      result_detail: '推理机未配置该连接器凭据',
      failure_code: 'COMMAND_CREDENTIALS_NOT_CONFIGURED',
      completed_at: '2026-09-08T08:01:00Z',
    })

    const wrapper = await mountDevices()
    await wrapper.find('button[aria-label="测试连接：装配线输入"]').trigger('click')

    await vi.waitFor(() => expect(wrapper.text()).toContain('连接测试被拒绝'))
    expect(wrapper.text()).toContain('推理机未配置该连接器凭据')
    expect(wrapper.text()).toContain('拒绝码：COMMAND_CREDENTIALS_NOT_CONFIGURED')
    expect(wrapper.text()).toContain('未验证')
  })

  it('renders an unknown failure code with the raw code and generic guidance', async () => {
    api.enqueueConnectorConnectionTest.mockResolvedValue(COMMAND)
    api.readDeviceCommand.mockResolvedValue({
      ...COMMAND,
      status: 'rejected',
      result_detail: '未来版本拒绝了该命令',
      failure_code: 'COMMAND_FROM_FUTURE',
      completed_at: '2026-09-08T08:01:00Z',
    })

    const wrapper = await mountDevices()
    await wrapper.find('button[aria-label="测试连接：装配线输入"]').trigger('click')

    await vi.waitFor(() => expect(wrapper.text()).toContain('COMMAND_FROM_FUTURE'))
    expect(wrapper.text()).toContain('请查看命令详情或联系管理员')
  })

  it('retries with a new command idempotency key after a finished test', async () => {
    api.enqueueConnectorConnectionTest
      .mockResolvedValueOnce({ ...COMMAND, id: 'command-1' })
      .mockResolvedValueOnce({ ...COMMAND, id: 'command-2', idempotency_key: 'test-key-2' })
    api.readDeviceCommand.mockResolvedValue({
      ...COMMAND,
      status: 'failed',
      result: 'unreachable',
      result_detail: '连接超时',
      completed_at: '2026-09-08T08:01:00Z',
    })

    const wrapper = await mountDevices()
    await wrapper.find('button[aria-label="测试连接：装配线输入"]').trigger('click')
    await vi.waitFor(() => expect(wrapper.text()).toContain('连接测试失败'))

    await wrapper.find('button[aria-label="重新测试连接：装配线输入"]').trigger('click')

    expect(api.enqueueConnectorConnectionTest).toHaveBeenCalledTimes(2)
    const [, secondKey] = api.enqueueConnectorConnectionTest.mock.calls[1]!
    expect(secondKey).not.toBe(api.enqueueConnectorConnectionTest.mock.calls[0]![1])
  })

  it('keeps the accepted command visible when the operator may edit but not view', async () => {
    setSession(['device.connector.edit'])
    api.enqueueConnectorConnectionTest.mockResolvedValue(COMMAND)

    const wrapper = mount(DevicesView)
    await flushPromises()
    await wrapper.find('input[name="known-test-connector-id"]').setValue('connector-1')
    await wrapper.find('form[aria-label="按标识测试连接"]').trigger('submit')

    expect(api.enqueueConnectorConnectionTest).toHaveBeenCalledWith(
      'connector-1',
      expect.any(String),
    )
    expect(api.readDeviceCommand).not.toHaveBeenCalled()
    expect(wrapper.text()).toContain('命令已提交；当前账号没有查看结果的权限')
  })

  it('stops polling when command visibility is revoked', async () => {
    api.enqueueConnectorConnectionTest.mockResolvedValue(COMMAND)
    api.readDeviceCommand.mockRejectedValue(
      new ControlPlaneError({
        message: '没有查看结果的权限',
        errorCode: 'PERMISSION_DENIED',
        status: 403,
      }),
    )

    const wrapper = await mountDevices()
    await wrapper.find('button[aria-label="测试连接：装配线输入"]').trigger('click')

    await vi.waitFor(() => expect(wrapper.text()).toContain('当前账号没有查看结果的权限'))
    const callsAfterRefusal = api.readDeviceCommand.mock.calls.length
    await new Promise((resolve) => window.setTimeout(resolve, 250))

    expect(api.readDeviceCommand).toHaveBeenCalledTimes(callsAfterRefusal)
    expect(wrapper.text()).toContain('已停止读取命令状态')
  })

  it('retries reading the same active command after permission returns', async () => {
    api.enqueueConnectorConnectionTest.mockResolvedValue(COMMAND)
    api.readDeviceCommand
      .mockRejectedValueOnce(
        new ControlPlaneError({
          message: '没有查看结果的权限',
          errorCode: 'PERMISSION_DENIED',
          status: 403,
        }),
      )
      .mockResolvedValueOnce({
        ...COMMAND,
        status: 'succeeded',
        result: 'reachable',
        result_detail: '设备响应正常',
      })

    const wrapper = await mountDevices()
    await wrapper.find('button[aria-label="测试连接：装配线输入"]').trigger('click')
    await vi.waitFor(() => expect(wrapper.text()).toContain('当前账号没有查看结果的权限'))

    await wrapper.find('button[aria-label="重试读取状态：装配线输入"]').trigger('click')
    await vi.waitFor(() => expect(wrapper.text()).toContain('连接测试成功'))

    expect(api.enqueueConnectorConnectionTest).toHaveBeenCalledTimes(1)
    expect(api.readDeviceCommand).toHaveBeenCalledTimes(2)
  })

  it('reports enqueue failure without showing a fake connection result', async () => {
    api.enqueueConnectorConnectionTest.mockRejectedValue(
      new ControlPlaneError({
        message: '连接器已停用，不能测试',
        errorCode: 'CONNECTOR_DEACTIVATED',
        status: 409,
      }),
    )

    const wrapper = await mountDevices()
    await wrapper.find('button[aria-label="测试连接：装配线输入"]').trigger('click')

    await vi.waitFor(() => expect(wrapper.find('[role="alert"]').text()).toContain('连接器已停用'))
    expect(wrapper.text()).not.toContain('连接测试成功')
    expect(wrapper.text()).not.toContain('连接测试失败')
  })

  it('reuses the idempotency key when enqueue acknowledgement is lost', async () => {
    api.enqueueConnectorConnectionTest
      .mockRejectedValueOnce(new TypeError('网络暂时不可达'))
      .mockResolvedValueOnce(COMMAND)

    const wrapper = await mountDevices()
    await wrapper.find('button[aria-label="测试连接：装配线输入"]').trigger('click')
    await vi.waitFor(() => expect(wrapper.find('[role="alert"]').exists()).toBe(true))

    await wrapper.find('button[aria-label="测试连接：装配线输入"]').trigger('click')
    await vi.waitFor(() => expect(api.enqueueConnectorConnectionTest).toHaveBeenCalledTimes(2))

    expect(api.enqueueConnectorConnectionTest.mock.calls[1]![1]).toBe(
      api.enqueueConnectorConnectionTest.mock.calls[0]![1],
    )
  })
})
