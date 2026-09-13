import ElementPlus from 'element-plus'
import { createPinia, setActivePinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'

import { ControlPlaneError } from '@/api/controlPlane'
import DevicesView from '@/modules/devices/DevicesView.vue'
import { useSessionStore } from '@/session/store'

const api = vi.hoisted(() => ({
  readConnectors: vi.fn(),
  readConnector: vi.fn(),
  readCameraMedia: vi.fn(),
  readInferenceHosts: vi.fn(),
  readStations: vi.fn(),
  createConnector: vi.fn(),
  editConnector: vi.fn(),
  setConnectorStatus: vi.fn(),
  deleteConnector: vi.fn(),
  readStationTemplateConfiguration: vi.fn(),
  readTemplateVersions: vi.fn(),
  validateTemplateBinding: vi.fn(),
  bindTemplateVersion: vi.fn(),
  updateStationRuntimeParameters: vi.fn(),
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

const STATION_CONFIGURATION = {
  station_id: 'station-1',
  station_revision: 4,
  runtime_parameters_revision: 1,
  runtime_parameter_mode: 'follow_template' as const,
  template_defaults: {
    idle_timeout_seconds: 30,
    step_deadline_seconds: 90,
    disposition_policy: 'record',
  },
  runtime_overrides: null,
  effective_runtime_parameters: {
    idle_timeout_seconds: 30,
    step_deadline_seconds: 90,
    disposition_policy: 'record',
  },
  desired: null,
  version: null,
  status: 'unbound' as const,
  status_detail: null,
  topology_issues: [],
  backends: [],
}

const CAMERA_MEDIA = {
  camera_id: 'camera-1',
  camera_name: '一号相机',
  camera_address: '10.0.8.21',
  main_stream_path: '/Streaming/Channels/101',
  sub_stream_path: '/Streaming/Channels/102',
  camera_status: 'active' as const,
  camera_revision: 2,
  station_id: 'station-1',
  station_name: '一号装配工位',
  station_status: 'active' as const,
  host_id: 'host-1',
  host_name: '推理机 A',
  host_status: 'active' as const,
  backend_id: 'backend-1',
  media_path: 'camera-1',
  media_path_mode: 'passthrough' as const,
  recording_mode: 'continuous' as const,
  credentials_configured: false,
  mediamtx_address: 'https://media.example.test:8889',
  mediamtx_playback_address: 'https://media.example.test:9996',
  recording_window_seconds: 3600,
}

afterEach(() => {
  vi.unstubAllGlobals()
})

beforeEach(() => {
  setActivePinia(createPinia())
  vi.clearAllMocks()
  api.readConnectors.mockResolvedValue({ items: [CONNECTOR], page: 1, page_size: 50, total: 1 })
  api.readCameraMedia.mockResolvedValue({ items: [], page: 1, page_size: 50, total: 0 })
  api.readInferenceHosts.mockResolvedValue({
    items: [{ id: 'host-1', name: '推理机 A' }],
    page: 1,
    page_size: 50,
    total: 1,
  })
  api.readStations.mockResolvedValue({
    items: [{ id: 'station-1', code: 'A-01', name: '一号装配工位' }],
    page: 1,
    page_size: 50,
    total: 1,
  })
  api.readStationTemplateConfiguration.mockResolvedValue(STATION_CONFIGURATION)
  api.readTemplateVersions.mockResolvedValue({ items: [], page: 1, page_size: 50, total: 0 })
  api.validateTemplateBinding.mockResolvedValue({
    ...STATION_CONFIGURATION,
    version: null,
    current_mode: 'follow_template',
    current_defaults: STATION_CONFIGURATION.template_defaults,
    current_overrides: null,
    requested_mode: 'follow_template',
    requested_overrides: null,
    accepted: true,
    reasons: [],
  })
  api.bindTemplateVersion.mockResolvedValue(STATION_CONFIGURATION)
  api.updateStationRuntimeParameters.mockResolvedValue(STATION_CONFIGURATION)
})

describe('工位与设备中的连接器', () => {
  it('does not request parent lookups the caller may not view', async () => {
    const session = useSessionStore()
    session.current = {
      user_id: 'viewer-1',
      login_name: 'viewer',
      display_name: '只读人员',
      expires_at: '2026-09-07T13:00:00Z',
      permissions: ['device.connector.view'],
    }

    mount(DevicesView, { global: { plugins: [ElementPlus] } })
    await flushPromises()

    expect(api.readConnectors).toHaveBeenCalledOnce()
    expect(api.readInferenceHosts).not.toHaveBeenCalled()
    expect(api.readStations).not.toHaveBeenCalled()
  })

  it('offers connector actions only to a fully authorized operator', async () => {
    const session = useSessionStore()
    session.current = {
      user_id: 'admin-1',
      login_name: 'administrator',
      display_name: '系统管理员',
      expires_at: '2026-09-07T13:00:00Z',
      permissions: [
        'device.connector.view',
        'device.connector.edit',
        'device.connector.delete',
        'device.inference_host.view',
        'device.station.view',
      ],
    }

    const wrapper = mount(DevicesView, { global: { plugins: [ElementPlus] } })
    await flushPromises()

    const labels = wrapper.findAll('button').map((button) => button.text())
    expect(labels).toContain('新建连接器')
    expect(labels).toContain('编辑')
    expect(labels).toContain('停用')
    expect(labels).toContain('删除')
  })

  it('edits a visible connector without requiring parent lookup permission', async () => {
    api.editConnector.mockResolvedValue(CONNECTOR)
    const session = useSessionStore()
    session.current = {
      user_id: 'editor-1',
      login_name: 'editor',
      display_name: '连接器编辑者',
      expires_at: '2026-09-07T13:00:00Z',
      permissions: ['device.connector.view', 'device.connector.edit'],
    }

    const wrapper = mount(DevicesView, { global: { plugins: [ElementPlus] } })
    await flushPromises()

    expect(api.readConnectors).toHaveBeenCalledOnce()
    expect(api.readInferenceHosts).not.toHaveBeenCalled()
    expect(api.readStations).not.toHaveBeenCalled()

    await wrapper
      .findAll('button')
      .find((button) => button.text() === '编辑')!
      .trigger('click')
    await wrapper.find('input[name="address"]').setValue('10.0.0.10')
    await wrapper
      .findAll('button')
      .find((button) => button.text() === '保存')!
      .trigger('click')
    await flushPromises()

    expect(api.editConnector).toHaveBeenCalledWith(
      'connector-1',
      {
        name: '装配线输入',
        connector_type: 'hikvision_isapi',
        configuration: { address: '10.0.0.10', port: 80 },
        station_id: 'station-1',
        host_id: 'host-1',
      },
      3,
    )
  })

  it('lets an edit-only caller submit a known connector without fetching it', async () => {
    api.editConnector.mockResolvedValue(CONNECTOR)
    const session = useSessionStore()
    session.current = {
      user_id: 'editor-2',
      login_name: 'editor',
      display_name: '连接器编辑者',
      expires_at: '2026-09-07T13:00:00Z',
      permissions: ['device.connector.edit'],
    }

    const wrapper = mount(DevicesView, { global: { plugins: [ElementPlus] } })
    await flushPromises()

    expect(api.readConnectors).not.toHaveBeenCalled()
    expect(api.readInferenceHosts).not.toHaveBeenCalled()
    expect(api.readStations).not.toHaveBeenCalled()

    await wrapper
      .findAll('button')
      .find((button) => button.text() === '按标识编辑')!
      .trigger('click')
    await wrapper.find('input[name="known-connector-id"]').setValue('connector-1')
    await wrapper.find('input[name="known-revision"]').setValue('7')
    await wrapper.find('input[name="name"]').setValue('现场输入')
    await wrapper.find('input[name="address"]').setValue('10.0.0.11')
    await wrapper.find('input[name="station-id"]').setValue('station-1')
    await wrapper.find('input[name="host-id"]').setValue('host-1')
    await wrapper
      .findAll('button')
      .find((button) => button.text() === '保存')!
      .trigger('click')
    await flushPromises()

    expect(api.editConnector).toHaveBeenCalledWith(
      'connector-1',
      {
        name: '现场输入',
        connector_type: 'hikvision_isapi',
        configuration: { address: '10.0.0.11' },
        station_id: 'station-1',
        host_id: 'host-1',
      },
      7,
    )
  })

  it('lets a delete-only caller delete by a known identifier and revision', async () => {
    api.deleteConnector.mockResolvedValue(undefined)
    const session = useSessionStore()
    session.current = {
      user_id: 'deleter-1',
      login_name: 'deleter',
      display_name: '连接器删除者',
      expires_at: '2026-09-07T13:00:00Z',
      permissions: ['device.connector.delete'],
    }

    const wrapper = mount(DevicesView, { global: { plugins: [ElementPlus] } })
    await flushPromises()

    expect(api.readConnectors).not.toHaveBeenCalled()
    await wrapper.find('input[name="known-connector-id"]').setValue('connector-1')
    await wrapper.find('input[name="known-revision"]').setValue('7')
    await wrapper.find('form').trigger('submit')
    await flushPromises()

    expect(api.deleteConnector).toHaveBeenCalledWith('connector-1', 7)
    expect(document.body.textContent).toContain('连接器已删除')
  })

  it('creates a connector with only non-secret parameters', async () => {
    api.createConnector.mockResolvedValue(CONNECTOR)
    const session = useSessionStore()
    session.current = {
      user_id: 'admin-1',
      login_name: 'administrator',
      display_name: '系统管理员',
      expires_at: '2026-09-07T13:00:00Z',
      permissions: [
        'device.connector.view',
        'device.connector.edit',
        'device.inference_host.view',
        'device.station.view',
      ],
    }

    const wrapper = mount(DevicesView, { global: { plugins: [ElementPlus] } })
    await flushPromises()
    await wrapper
      .findAll('button')
      .find((button) => button.text() === '新建连接器')!
      .trigger('click')
    await flushPromises()

    expect(wrapper.find('input[name="password"]').exists()).toBe(false)
    expect(wrapper.find('input[name="username"]').exists()).toBe(false)

    await wrapper.find('input[name="name"]').setValue('现场输入')
    await wrapper.find('input[name="address"]').setValue('10.0.0.9')
    await wrapper.find('input[name="port"]').setValue('8080')
    await wrapper
      .findAll('button')
      .find((button) => button.text() === '创建')!
      .trigger('click')
    await flushPromises()

    expect(api.createConnector).toHaveBeenCalledWith({
      name: '现场输入',
      connector_type: 'hikvision_isapi',
      configuration: { address: '10.0.0.9', port: 8080 },
      station_id: 'station-1',
      host_id: 'host-1',
    })
  })

  it('opens a detail view from the real connector resource', async () => {
    api.readConnector.mockResolvedValue(CONNECTOR)
    const session = useSessionStore()
    session.current = {
      user_id: 'admin-1',
      login_name: 'administrator',
      display_name: '系统管理员',
      expires_at: '2026-09-07T13:00:00Z',
      permissions: ['device.connector.view', 'device.inference_host.view', 'device.station.view'],
    }

    const wrapper = mount(DevicesView, { global: { plugins: [ElementPlus] } })
    await flushPromises()
    await wrapper
      .findAll('button')
      .find((button) => button.text() === '详情')!
      .trigger('click')
    await flushPromises()

    expect(api.readConnector).toHaveBeenCalledWith('connector-1')
    expect(wrapper.text()).toContain('连接器详情')
    expect(wrapper.text()).toContain('一号装配工位')
    expect(wrapper.text()).toContain('推理机 A')
    expect(wrapper.text()).toContain('192.168.10.21:80')
    expect(wrapper.text()).toContain('未验证')
  })

  it('edits the whole placement with the revision it read', async () => {
    api.editConnector.mockResolvedValue(CONNECTOR)
    const session = useSessionStore()
    session.current = {
      user_id: 'admin-1',
      login_name: 'administrator',
      display_name: '系统管理员',
      expires_at: '2026-09-07T13:00:00Z',
      permissions: [
        'device.connector.view',
        'device.connector.edit',
        'device.inference_host.view',
        'device.station.view',
      ],
    }

    const wrapper = mount(DevicesView, { global: { plugins: [ElementPlus] } })
    await flushPromises()
    await wrapper
      .findAll('button')
      .find((button) => button.text() === '编辑')!
      .trigger('click')
    await flushPromises()
    await wrapper.find('input[name="address"]').setValue('10.0.0.10')
    await wrapper
      .findAll('button')
      .find((button) => button.text() === '保存')!
      .trigger('click')
    await flushPromises()

    expect(api.editConnector).toHaveBeenCalledWith(
      'connector-1',
      {
        name: '装配线输入',
        connector_type: 'hikvision_isapi',
        configuration: { address: '10.0.0.10', port: 80 },
        station_id: 'station-1',
        host_id: 'host-1',
      },
      3,
    )
  })

  it('deactivates and restores with the row revision', async () => {
    api.setConnectorStatus.mockResolvedValue({ ...CONNECTOR, status: 'deactivated' })
    const session = useSessionStore()
    session.current = {
      user_id: 'admin-1',
      login_name: 'administrator',
      display_name: '系统管理员',
      expires_at: '2026-09-07T13:00:00Z',
      permissions: ['device.connector.view', 'device.connector.edit'],
    }

    const wrapper = mount(DevicesView, { global: { plugins: [ElementPlus] } })
    await flushPromises()
    await wrapper
      .findAll('button')
      .find((button) => button.text() === '停用')!
      .trigger('click')
    await flushPromises()

    expect(api.setConnectorStatus).toHaveBeenCalledWith('connector-1', 'deactivated', 3)
    expect(document.body.textContent).toContain('连接器已停用')
  })

  it('deletes with the row revision when delete permission is present', async () => {
    api.deleteConnector.mockResolvedValue(undefined)
    const session = useSessionStore()
    session.current = {
      user_id: 'admin-1',
      login_name: 'administrator',
      display_name: '系统管理员',
      expires_at: '2026-09-07T13:00:00Z',
      permissions: ['device.connector.view', 'device.connector.delete'],
    }

    const wrapper = mount(DevicesView, { global: { plugins: [ElementPlus] } })
    await flushPromises()
    await wrapper
      .findAll('button')
      .find((button) => button.text() === '删除')!
      .trigger('click')
    await flushPromises()

    expect(api.deleteConnector).toHaveBeenCalledWith('connector-1', 3)
    expect(document.body.textContent).toContain('连接器已删除')
  })

  it('puts a backend refusal beside the rejected non-secret field', async () => {
    api.createConnector.mockRejectedValue(
      new ControlPlaneError({
        message: '提交的内容不合要求',
        errorCode: 'CONNECTOR_CONFIGURATION_SECRET',
        status: 422,
        fieldErrors: [{ field: 'configuration.address', message: '地址不能携带凭据' }],
      }),
    )
    const session = useSessionStore()
    session.current = {
      user_id: 'admin-1',
      login_name: 'administrator',
      display_name: '系统管理员',
      expires_at: '2026-09-07T13:00:00Z',
      permissions: [
        'device.connector.view',
        'device.connector.edit',
        'device.inference_host.view',
        'device.station.view',
      ],
    }

    const wrapper = mount(DevicesView, { global: { plugins: [ElementPlus] } })
    await flushPromises()
    await wrapper
      .findAll('button')
      .find((button) => button.text() === '新建连接器')!
      .trigger('click')
    await flushPromises()
    await wrapper.find('input[name="address"]').setValue('https://user:secret@example.test') // pragma: allowlist secret
    await wrapper
      .findAll('button')
      .find((button) => button.text() === '创建')!
      .trigger('click')
    await flushPromises()

    expect(wrapper.find('[role="alert"]').text()).toContain('提交的内容不合要求')
    expect(wrapper.findAll('.devices__field-error').map((item) => item.text())).toContain(
      '地址不能携带凭据',
    )
  })

  it('lists a connector with its station and host names and stays unverified', async () => {
    const session = useSessionStore()
    session.current = {
      user_id: 'admin-1',
      login_name: 'administrator',
      display_name: '系统管理员',
      expires_at: '2026-09-07T13:00:00Z',
      permissions: ['device.connector.view', 'device.inference_host.view', 'device.station.view'],
    }

    const wrapper = mount(DevicesView, { global: { plugins: [ElementPlus] } })
    await flushPromises()

    expect(wrapper.text()).toContain('装配线输入')
    expect(wrapper.text()).toContain('一号装配工位')
    expect(wrapper.text()).toContain('推理机 A')
    expect(wrapper.text()).toContain('已配置')
    expect(wrapper.text()).toContain('未验证')
    expect(wrapper.text()).not.toContain('测试连接')
    const labels = wrapper.findAll('button').map((button) => button.text())
    expect(labels).not.toContain('新建连接器')
    expect(labels).not.toContain('编辑')
    expect(labels).not.toContain('停用')
    expect(labels).not.toContain('删除')
  })

  it('does not reinterpret unknown connector type or status values', async () => {
    const futureConnector = {
      ...CONNECTOR,
      connector_type: 'future_connector' as typeof CONNECTOR.connector_type,
      status: 'future_status' as typeof CONNECTOR.status,
    }
    api.readConnectors.mockResolvedValue({
      items: [futureConnector],
      page: 1,
      page_size: 50,
      total: 1,
    })
    const session = useSessionStore()
    session.current = {
      user_id: 'viewer-2',
      login_name: 'viewer',
      display_name: '只读人员',
      expires_at: '2026-09-07T13:00:00Z',
      permissions: ['device.connector.view', 'device.connector.edit'],
    }

    const wrapper = mount(DevicesView, { global: { plugins: [ElementPlus] } })
    await flushPromises()

    const row = wrapper.find('tbody tr')
    expect(row.text()).toContain('未知连接器类型（future_connector）')
    expect(row.text()).toContain('未知状态（future_status）')
    expect(row.text()).not.toContain('板卡连接器')
    expect(row.text()).not.toContain('已停用')
    const labels = row.findAll('button').map((button) => button.text())
    expect(labels).not.toContain('停用')
    expect(labels).not.toContain('恢复')
    expect(row.text()).toContain('状态未知，不能切换')
  })

  it('shows desired and reported template facts without collapsing them into one status', async () => {
    api.readStationTemplateConfiguration.mockResolvedValue({
      ...STATION_CONFIGURATION,
      desired: {
        id: 'binding-1',
        version_id: 'version-1',
        sha256: 'a'.repeat(64),
        config_revision: 2,
        revision: 1,
        created_by: 'operator-1',
        updated_by: 'operator-1',
        created_at: '2026-09-08T01:00:00Z',
        updated_at: '2026-09-08T01:00:00Z',
      },
      status: 'waiting',
      status_detail: '等待推理机应用期望配置',
      backends: [
        {
          backend_id: 'backend-1',
          host_id: 'host-1',
          status: 'waiting',
          reported_version_id: 'old-version',
          reported_sha256: 'b'.repeat(64),
          reported_config_revision: 1,
          reported_at: '2026-09-08T01:02:00Z',
          rejection_code: 'future_rejection_code',
          rejection_detail: '现场报告无法确认',
          rejection_at: null,
        },
      ],
    })
    const session = useSessionStore()
    session.current = {
      user_id: 'station-viewer',
      login_name: 'station.viewer',
      display_name: '工位查看者',
      expires_at: '2026-09-07T13:00:00Z',
      permissions: ['device.station.view'],
    }

    const wrapper = mount(DevicesView, { global: { plugins: [ElementPlus] } })
    await flushPromises()

    expect(wrapper.text()).toContain('等待推理机应用')
    expect(wrapper.text()).toContain('version-1')
    expect(wrapper.text()).toContain('old-vers')
    expect(wrapper.text()).toContain('修订 1')
    expect(wrapper.text()).toContain('等待推理机应用期望配置')
    expect(wrapper.text()).toContain('拒绝原因：future_rejection_code')
    expect(wrapper.text()).toContain('请核对版本、摘要和配置修订')
  })

  it('prechecks and binds a known version without requiring template version view permission', async () => {
    api.validateTemplateBinding.mockResolvedValue({
      version: { id: 'version-1', sha256: 'a'.repeat(64) },
      station_revision: 4,
      current_mode: 'follow_template',
      current_defaults: STATION_CONFIGURATION.template_defaults,
      current_overrides: null,
      requested_mode: 'follow_template',
      requested_overrides: null,
      accepted: true,
      reasons: [],
    })
    api.bindTemplateVersion.mockResolvedValue({
      ...STATION_CONFIGURATION,
      desired: { version_id: 'version-1', sha256: 'a'.repeat(64), config_revision: 2 },
      status: 'waiting',
    })
    const session = useSessionStore()
    session.current = {
      user_id: 'station-editor',
      login_name: 'station.editor',
      display_name: '工位编辑者',
      expires_at: '2026-09-07T13:00:00Z',
      permissions: ['device.station.view', 'device.station.edit'],
    }

    const wrapper = mount(DevicesView, { global: { plugins: [ElementPlus] } })
    await flushPromises()
    await wrapper
      .findAll('button')
      .find((button) => button.text() === '绑定 / 编辑')!
      .trigger('click')
    await flushPromises()
    await wrapper.find('input[name="known-template-version-id-dialog"]').setValue('version-1')
    await wrapper
      .findAll('button')
      .find((button) => button.text() === '预检绑定')!
      .trigger('click')
    await flushPromises()

    expect(api.validateTemplateBinding).toHaveBeenCalledWith({
      station_id: 'station-1',
      version_id: 'version-1',
      runtime_parameter_mode: 'follow_template',
      runtime_parameters: null,
    })
    await wrapper
      .findAll('button')
      .find((button) => button.text() === '正式绑定')!
      .trigger('click')
    await flushPromises()

    expect(api.bindTemplateVersion).toHaveBeenCalledWith(
      {
        station_id: 'station-1',
        version_id: 'version-1',
        runtime_parameter_mode: 'follow_template',
        runtime_parameters: null,
      },
      4,
    )
  })

  it('preserves unknown runtime parameters in the known-ID edit flow', async () => {
    api.validateTemplateBinding.mockResolvedValue({
      version: { id: 'version-1', sha256: 'a'.repeat(64) },
      station_revision: 7,
      current_mode: 'custom',
      current_defaults: STATION_CONFIGURATION.template_defaults,
      current_overrides: {
        idle_timeout_seconds: 12,
        step_deadline_seconds: 44,
        disposition_policy: 'hold',
      },
      requested_mode: 'custom',
      requested_overrides: {
        idle_timeout_seconds: 12,
        step_deadline_seconds: 44,
        disposition_policy: 'hold',
      },
      accepted: true,
      reasons: [],
    })
    api.bindTemplateVersion.mockResolvedValue(STATION_CONFIGURATION)
    const session = useSessionStore()
    session.current = {
      user_id: 'station-editor-only',
      login_name: 'station.editor.only',
      display_name: '仅编辑人员',
      expires_at: '2026-09-07T13:00:00Z',
      permissions: ['device.station.edit'],
    }

    const wrapper = mount(DevicesView, { global: { plugins: [ElementPlus] } })
    await flushPromises()
    expect(api.readStations).not.toHaveBeenCalled()
    await wrapper.find('input[name="known-station-id"]').setValue('station-1')
    await wrapper.find('input[name="known-station-revision"]').setValue('7')
    await wrapper.find('input[name="known-template-version-id"]').setValue('version-1')
    await wrapper
      .findAll('button')
      .find((button) => button.text() === '打开配置')!
      .trigger('click')
    await wrapper
      .findAll('button')
      .find((button) => button.text() === '预检绑定')!
      .trigger('click')
    await flushPromises()

    expect(api.validateTemplateBinding).toHaveBeenCalledWith({
      station_id: 'station-1',
      version_id: 'version-1',
      runtime_parameter_mode: null,
      runtime_parameters: null,
    })
    await wrapper
      .findAll('button')
      .find((button) => button.text() === '正式绑定')!
      .trigger('click')
    await flushPromises()

    expect(api.bindTemplateVersion).toHaveBeenCalledWith(
      {
        station_id: 'station-1',
        version_id: 'version-1',
        runtime_parameter_mode: null,
        runtime_parameters: null,
      },
      7,
    )
  })
})

describe('工位与设备中的相机媒体', () => {
  it('uses the generated media query only for a camera viewer and shows the stable path', async () => {
    api.readCameraMedia.mockResolvedValue({
      items: [CAMERA_MEDIA],
      page: 1,
      page_size: 50,
      total: 1,
    })
    const session = useSessionStore()
    session.current = {
      user_id: 'camera-viewer',
      login_name: 'viewer',
      display_name: '查看人员',
      expires_at: '2026-09-07T13:00:00Z',
      permissions: ['device.camera.view'],
    }

    const wrapper = mount(DevicesView, { global: { plugins: [ElementPlus] } })
    await flushPromises()

    expect(api.readCameraMedia).toHaveBeenCalledOnce()
    expect(wrapper.text()).toContain('直接媒体路径')
    expect(wrapper.text()).toContain('camera-1')
    expect(wrapper.text()).toContain('https://media.example.test:9996')
  })

  it('queries the direct playback interface without sending center credentials', async () => {
    api.readCameraMedia.mockResolvedValue({
      items: [CAMERA_MEDIA],
      page: 1,
      page_size: 50,
      total: 1,
    })
    const fetch = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: () => Promise.resolve([{ start: '2026-09-12T01:00:00Z', duration: 60 }]),
    })
    vi.stubGlobal('fetch', fetch)
    const session = useSessionStore()
    session.current = {
      user_id: 'camera-playback-viewer',
      login_name: 'viewer',
      display_name: '查看人员',
      expires_at: '2026-09-07T13:00:00Z',
      permissions: ['device.camera.view'],
    }

    const wrapper = mount(DevicesView, { global: { plugins: [ElementPlus] } })
    await flushPromises()
    await wrapper
      .findAll('button')
      .find((button) => button.text() === '查询片段')!
      .trigger('click')
    await flushPromises()

    const [request, options] = fetch.mock.calls[0] ?? []
    expect(request).toBeDefined()
    expect(String(request)).toContain('https://media.example.test:9996/list')
    expect(options).toEqual(expect.objectContaining({ credentials: 'omit', cache: 'no-store' }))
    expect(wrapper.text()).toContain('录像查询')
  })
})
