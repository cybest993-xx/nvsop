import ElementPlus from 'element-plus'
import { createPinia, setActivePinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'

import PointManagement from '@/modules/devices/PointManagement.vue'
import { useSessionStore } from '@/session/store'
import type { ConnectorView } from '@/api/controlPlane'

const api = vi.hoisted(() => ({
  readPoints: vi.fn(),
  readPoint: vi.fn(),
  readStations: vi.fn(),
  readConnectors: vi.fn(),
  createPoint: vi.fn(),
  editPoint: vi.fn(),
  setPointStatus: vi.fn(),
  deletePoint: vi.fn(),
  updateConnectorCapability: vi.fn(),
  validatePointBinding: vi.fn(),
}))

vi.mock('@/api/controlPlane', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/controlPlane')>()),
  ...api,
}))

const POINT = {
  id: 'point-1',
  identifier: 'DI-01',
  semantic_label: '工件到位',
  direction: 'input' as const,
  station_id: 'station-1',
  connector_id: 'connector-1',
  status: 'active' as const,
  revision: 4,
  created_at: '2026-09-07T04:00:00Z',
  created_by: 'operator-1',
  updated_at: '2026-09-07T04:00:00Z',
  updated_by: 'operator-1',
}

const STATION = {
  id: 'station-1',
  code: 'A-01',
  name: '一号装配工位',
  tags: [],
  status: 'active' as const,
  revision: 2,
  created_at: '2026-09-07T04:00:00Z',
  created_by: 'operator-1',
  updated_at: '2026-09-07T04:00:00Z',
  updated_by: 'operator-1',
}

const CONNECTOR = {
  id: 'connector-1',
  station_id: 'station-1',
  host_id: 'host-1',
  name: '现场 IO',
  connector_type: 'hikvision_isapi' as const,
  configuration: { address: '192.168.10.21', port: 80 },
  credentials_configured: false,
  reachability: 'unverified',
  health_detail: null,
  capability: { verification: 'unverified' as const },
  status: 'active' as const,
  revision: 5,
  created_at: '2026-09-07T04:00:00Z',
  created_by: 'operator-1',
  updated_at: '2026-09-07T04:00:00Z',
  updated_by: 'operator-1',
}

beforeEach(() => {
  setActivePinia(createPinia())
  vi.clearAllMocks()
  api.readPoints.mockResolvedValue({ items: [POINT], page: 1, page_size: 20, total: 1 })
})

afterEach(() => {
  document.body.innerHTML = ''
})

describe('点位管理', () => {
  it('lists points for point viewers without fetching unauthorized parent resources', async () => {
    const session = useSessionStore()
    session.current = {
      user_id: 'viewer-1',
      login_name: 'viewer',
      display_name: '点位查看者',
      expires_at: '2026-09-07T13:00:00Z',
      permissions: ['device.point.view'],
    }

    const wrapper = mount(PointManagement, { global: { plugins: [ElementPlus] } })
    await flushPromises()

    expect(api.readPoints).toHaveBeenCalledOnce()
    expect(api.readStations).not.toHaveBeenCalled()
    expect(api.readConnectors).not.toHaveBeenCalled()
    expect(wrapper.text()).toContain('DI-01')
    expect(wrapper.text()).toContain('工件到位')
    expect(wrapper.text()).toContain('输入')
    expect(wrapper.text()).toContain('在用')
    expect(wrapper.findAll('button').map((button) => button.text())).not.toContain('新建点位')
  })

  it('does not request a point list without point view permission and states the available action', async () => {
    const session = useSessionStore()
    session.current = {
      user_id: 'editor-1',
      login_name: 'editor',
      display_name: '点位编辑者',
      expires_at: '2026-09-07T13:00:00Z',
      permissions: ['device.point.edit'],
    }

    const wrapper = mount(PointManagement, { global: { plugins: [ElementPlus] } })
    await flushPromises()

    expect(api.readPoints).not.toHaveBeenCalled()
    expect(wrapper.text()).toContain('当前没有点位查看权限')
    expect(wrapper.findAll('button').map((button) => button.text())).toContain('按标识编辑')
  })

  it('creates a point without fetching station or connector parents', async () => {
    api.createPoint.mockResolvedValue(POINT)
    const session = useSessionStore()
    session.current = {
      user_id: 'editor-2',
      login_name: 'editor',
      display_name: '点位编辑者',
      expires_at: '2026-09-07T13:00:00Z',
      permissions: ['device.point.edit'],
    }

    const wrapper = mount(PointManagement, { global: { plugins: [ElementPlus] } })
    await flushPromises()
    await wrapper
      .findAll('button')
      .find((button) => button.text() === '新建点位')!
      .trigger('click')
    await wrapper.find('input[name="identifier"]').setValue('DO-01')
    await wrapper.find('input[name="semantic_label"]').setValue('安全灯')
    await wrapper.find('input[name="station-id"]').setValue('station-1')
    await wrapper.find('input[name="connector-id"]').setValue('connector-1')
    await wrapper.find('.modal form').trigger('submit')
    await flushPromises()

    expect(api.readStations).not.toHaveBeenCalled()
    expect(api.readConnectors).not.toHaveBeenCalled()
    expect(api.createPoint).toHaveBeenCalledWith({
      identifier: 'DO-01',
      semantic_label: '安全灯',
      direction: 'input',
      station_id: 'station-1',
      connector_id: 'connector-1',
    })
  })

  it('edits a visible point with the row revision', async () => {
    api.editPoint.mockResolvedValue({ ...POINT, semantic_label: '工件到位确认' })
    const session = useSessionStore()
    session.current = {
      user_id: 'editor-3',
      login_name: 'editor',
      display_name: '点位编辑者',
      expires_at: '2026-09-07T13:00:00Z',
      permissions: ['device.point.view', 'device.point.edit'],
    }

    const wrapper = mount(PointManagement, { global: { plugins: [ElementPlus] } })
    await flushPromises()
    await wrapper
      .findAll('button')
      .find((button) => button.text() === '编辑')!
      .trigger('click')
    await wrapper.find('input[name="semantic_label"]').setValue('工件到位确认')
    await wrapper.find('.modal form').trigger('submit')
    await flushPromises()

    expect(api.editPoint).toHaveBeenCalledWith(
      'point-1',
      {
        identifier: 'DI-01',
        semantic_label: '工件到位确认',
        direction: 'input',
        station_id: 'station-1',
        connector_id: 'connector-1',
      },
      4,
    )
  })

  it('deactivates and restores a point with its revision', async () => {
    api.setPointStatus.mockResolvedValue({ ...POINT, status: 'deactivated' })
    const session = useSessionStore()
    session.current = {
      user_id: 'editor-4',
      login_name: 'editor',
      display_name: '点位编辑者',
      expires_at: '2026-09-07T13:00:00Z',
      permissions: ['device.point.view', 'device.point.edit'],
    }

    const wrapper = mount(PointManagement, { global: { plugins: [ElementPlus] } })
    await flushPromises()
    await wrapper
      .findAll('button')
      .find((button) => button.text() === '停用')!
      .trigger('click')
    await flushPromises()

    expect(api.setPointStatus).toHaveBeenCalledWith('point-1', 'deactivated', 4)
    expect(wrapper.text()).toContain('点位已停用')
  })

  it('deletes a point with its revision', async () => {
    api.deletePoint.mockResolvedValue(undefined)
    const session = useSessionStore()
    session.current = {
      user_id: 'deleter-1',
      login_name: 'deleter',
      display_name: '点位删除者',
      expires_at: '2026-09-07T13:00:00Z',
      permissions: ['device.point.view', 'device.point.delete'],
    }

    const wrapper = mount(PointManagement, { global: { plugins: [ElementPlus] } })
    await flushPromises()
    await wrapper
      .findAll('button')
      .find((button) => button.text() === '删除')!
      .trigger('click')
    await flushPromises()

    expect(api.deletePoint).toHaveBeenCalledWith('point-1', 4)
    expect(wrapper.text()).toContain('点位已删除')
  })

  it('opens point detail through the generated resource call', async () => {
    api.readPoint.mockResolvedValue(POINT)
    const session = useSessionStore()
    session.current = {
      user_id: 'viewer-2',
      login_name: 'viewer',
      display_name: '点位查看者',
      expires_at: '2026-09-07T13:00:00Z',
      permissions: ['device.point.view'],
    }

    const wrapper = mount(PointManagement, { global: { plugins: [ElementPlus] } })
    await flushPromises()
    await wrapper
      .findAll('button')
      .find((button) => button.text() === '详情')!
      .trigger('click')
    await flushPromises()

    expect(api.readPoint).toHaveBeenCalledWith('point-1')
    expect(wrapper.text()).toContain('点位详情')
    expect(wrapper.text()).toContain('工件到位')
    expect(wrapper.text()).toContain('2026/09/07 12:00')
  })

  it('passes station and connector filters to the real list query', async () => {
    const session = useSessionStore()
    session.current = {
      user_id: 'viewer-3',
      login_name: 'viewer',
      display_name: '点位查看者',
      expires_at: '2026-09-07T13:00:00Z',
      permissions: ['device.point.view'],
    }

    const wrapper = mount(PointManagement, { global: { plugins: [ElementPlus] } })
    await flushPromises()
    await wrapper.find('input[aria-label="按工位 ID 筛选"]').setValue('station-7')
    await wrapper.find('input[aria-label="按连接器 ID 筛选"]').setValue('connector-8')
    await flushPromises()

    expect(api.readPoints).toHaveBeenLastCalledWith({
      page: 1,
      page_size: 20,
      station_id: 'station-7',
      connector_id: 'connector-8',
    })
  })

  it('offers point pagination and requests the selected page', async () => {
    const secondPage = { ...POINT, id: 'point-21', identifier: 'DI-21' }
    api.readPoints
      .mockResolvedValueOnce({ items: [POINT], page: 1, page_size: 20, total: 21 })
      .mockResolvedValueOnce({ items: [secondPage], page: 2, page_size: 20, total: 21 })
    const session = useSessionStore()
    session.current = {
      user_id: 'viewer-page',
      login_name: 'viewer',
      display_name: '点位查看者',
      expires_at: '2026-09-07T13:00:00Z',
      permissions: ['device.point.view'],
    }

    const wrapper = mount(PointManagement, { global: { plugins: [ElementPlus] } })
    await flushPromises()

    expect(wrapper.text()).toContain('第 1 / 2 页')
    await wrapper.find('button[aria-label="下一页"]').trigger('click')
    await flushPromises()

    expect(api.readPoints).toHaveBeenLastCalledWith({ page: 2, page_size: 20 })
    expect(wrapper.text()).toContain('DI-21')
    expect(wrapper.text()).toContain('第 2 / 2 页')
  })

  it('shows measured capability facts without inferring them for an unverified connector', async () => {
    const measured = {
      ...CONNECTOR,
      capability: {
        verification: 'measured' as const,
        delivery: 'polled' as const,
        polling_interval_seconds: 0.5,
        max_delivery_delay_seconds: 0.8,
        sequencing: 'sequenced' as const,
        edge_preservation: 'preserved' as const,
        timestamp_source: 'host_receipt' as const,
      },
    }
    const session = useSessionStore()
    session.current = {
      user_id: 'capability-viewer',
      login_name: 'viewer',
      display_name: '能力查看者',
      expires_at: '2026-09-07T13:00:00Z',
      permissions: ['device.connector.view'],
    }

    const wrapper = mount(PointManagement, {
      props: { connectors: [CONNECTOR, measured], stations: [STATION] },
      global: { plugins: [ElementPlus] },
    })
    await flushPromises()

    expect(wrapper.text()).toContain('现场 IO')
    expect(wrapper.text()).toContain('未验证')
    expect(wrapper.text()).toContain('已实测')
    expect(wrapper.text()).toContain('轮询')
    expect(wrapper.text()).toContain('0.5 秒')
    expect(wrapper.text()).toContain('0.8 秒')
    expect(wrapper.text()).toContain('保留边沿')
    expect(wrapper.text()).toContain('主机接收时刻')
    expect(wrapper.text()).not.toContain('未验证 / 0.5 秒')
  })

  it('updates an existing measured capability with its connector revision', async () => {
    api.updateConnectorCapability.mockResolvedValue(CONNECTOR)
    const measured = {
      ...CONNECTOR,
      capability: {
        verification: 'measured' as const,
        delivery: 'polled' as const,
        polling_interval_seconds: 0.5,
        max_delivery_delay_seconds: 0.8,
        sequencing: 'sequenced' as const,
        edge_preservation: 'preserved' as const,
        timestamp_source: 'host_receipt' as const,
      },
    }
    const session = useSessionStore()
    session.current = {
      user_id: 'capability-editor',
      login_name: 'editor',
      display_name: '能力编辑者',
      expires_at: '2026-09-07T13:00:00Z',
      permissions: ['device.connector.view', 'device.connector.edit'],
    }

    const wrapper = mount(PointManagement, {
      props: { connectors: [measured], stations: [STATION] },
      global: { plugins: [ElementPlus] },
    })
    await flushPromises()
    await wrapper
      .findAll('button')
      .find((button) => button.text() === '编辑声明')!
      .trigger('click')
    await wrapper.find('input[name="max-delivery-delay-seconds"]').setValue('1.2')
    await wrapper.find('.modal form').trigger('submit')
    await flushPromises()

    expect(api.updateConnectorCapability).toHaveBeenCalledWith(
      'connector-1',
      {
        verification: 'measured',
        delivery: 'polled',
        polling_interval_seconds: 0.5,
        max_delivery_delay_seconds: 1.2,
        sequencing: 'sequenced',
        edge_preservation: 'preserved',
        timestamp_source: 'host_receipt',
      },
      5,
    )
  })

  it('updates the polling interval requirement when delivery changes to pushed', async () => {
    const measured = {
      ...CONNECTOR,
      capability: {
        verification: 'measured' as const,
        delivery: 'polled' as const,
        polling_interval_seconds: 0.5,
        max_delivery_delay_seconds: 0.8,
        sequencing: 'sequenced' as const,
        edge_preservation: 'preserved' as const,
        timestamp_source: 'host_receipt' as const,
      },
    }
    const session = useSessionStore()
    session.current = {
      user_id: 'capability-editor-3',
      login_name: 'editor',
      display_name: '能力编辑者',
      expires_at: '2026-09-07T13:00:00Z',
      permissions: ['device.connector.view', 'device.connector.edit'],
    }

    const wrapper = mount(PointManagement, {
      props: { connectors: [measured], stations: [STATION] },
      global: { plugins: [ElementPlus] },
    })
    await flushPromises()
    await wrapper
      .findAll('button')
      .find((button) => button.text() === '编辑声明')!
      .trigger('click')

    const pollingInterval = wrapper.find('input[name="polling-interval-seconds"]')
    expect(pollingInterval.attributes('required')).toBeDefined()
    await wrapper.find('select[name="delivery"]').setValue('pushed')
    await flushPromises()
    expect(pollingInterval.attributes('required')).toBeUndefined()
  })

  it('keeps an unverified capability explicit when a connector editor saves by known id', async () => {
    api.updateConnectorCapability.mockResolvedValue(CONNECTOR)
    const session = useSessionStore()
    session.current = {
      user_id: 'capability-editor-2',
      login_name: 'editor',
      display_name: '能力编辑者',
      expires_at: '2026-09-07T13:00:00Z',
      permissions: ['device.connector.edit'],
    }

    const wrapper = mount(PointManagement, { global: { plugins: [ElementPlus] } })
    await flushPromises()
    await wrapper.find('input[name="known-capability-connector-id"]').setValue('connector-9')
    await wrapper.find('input[name="known-capability-revision"]').setValue('6')
    await wrapper.find('.direct-form').trigger('submit')
    await flushPromises()
    await wrapper.find('.modal form').trigger('submit')
    await flushPromises()

    expect(api.readConnectors).not.toHaveBeenCalled()
    expect(api.updateConnectorCapability).toHaveBeenCalledWith(
      'connector-9',
      { verification: 'unverified' },
      6,
    )
  })

  it('keeps every unknown capability fact raw and disables its edit action', async () => {
    const future = {
      ...CONNECTOR,
      capability: {
        verification: 'future_measurement',
        delivery: 'future_delivery',
        polling_interval_seconds: 'future_interval',
        max_delivery_delay_seconds: 'future_delay',
        sequencing: 'future_sequence',
        edge_preservation: 'future_edge',
        timestamp_source: 'future_timestamp',
      },
    } as unknown as ConnectorView
    const session = useSessionStore()
    session.current = {
      user_id: 'capability-viewer-2',
      login_name: 'viewer',
      display_name: '能力查看者',
      expires_at: '2026-09-07T13:00:00Z',
      permissions: ['device.connector.view', 'device.connector.edit'],
    }

    const wrapper = mount(PointManagement, {
      props: { connectors: [future], stations: [STATION] },
      global: { plugins: [ElementPlus] },
    })
    await flushPromises()

    expect(wrapper.text()).toContain('未知能力声明（future_measurement）')
    for (const rawValue of [
      'future_delivery',
      'future_interval',
      'future_delay',
      'future_sequence',
      'future_edge',
      'future_timestamp',
    ]) {
      expect(wrapper.text()).toContain(rawValue)
    }
    expect(wrapper.findAll('button').map((button) => button.text())).not.toContain('编辑声明')
    expect(wrapper.text()).toContain('未知值，不能编辑')
  })

  it('keeps missing and unknown measured facts explicit and only marks pushed intervals inapplicable', async () => {
    const measuredWithUnknowns = {
      ...CONNECTOR,
      capability: {
        verification: 'measured',
        delivery: 'polled',
        polling_interval_seconds: null,
        max_delivery_delay_seconds: 'future_delay',
        sequencing: 'future_sequence',
        edge_preservation: 'future_edge',
        timestamp_source: 'future_timestamp',
      },
    } as unknown as ConnectorView
    const pushed = {
      ...CONNECTOR,
      id: 'connector-pushed',
      name: '推送连接器',
      capability: {
        verification: 'measured',
        delivery: 'pushed',
        polling_interval_seconds: null,
        max_delivery_delay_seconds: 0.2,
        sequencing: 'sequenced',
        edge_preservation: 'preserved',
        timestamp_source: 'host_receipt',
      },
    } as unknown as ConnectorView
    const session = useSessionStore()
    session.current = {
      user_id: 'capability-viewer-3',
      login_name: 'viewer',
      display_name: '能力查看者',
      expires_at: '2026-09-07T13:00:00Z',
      permissions: ['device.connector.view', 'device.connector.edit'],
    }

    const wrapper = mount(PointManagement, {
      props: { connectors: [measuredWithUnknowns, pushed], stations: [STATION] },
      global: { plugins: [ElementPlus] },
    })
    await flushPromises()

    expect(wrapper.text()).toContain('未提供')
    expect(wrapper.text()).toContain('future_delay')
    expect(wrapper.text()).toContain('future_sequence')
    expect(wrapper.text()).toContain('future_edge')
    expect(wrapper.text()).toContain('future_timestamp')
    expect(wrapper.text()).toContain('不适用')
    const table = wrapper.find('table[aria-label="连接器能力声明清单"]')
    const unknownRow = table.findAll('tbody tr').find((row) => row.text().includes('现场 IO'))
    const pushedRow = table.findAll('tbody tr').find((row) => row.text().includes('推送连接器'))
    expect(unknownRow?.findAll('button').map((button) => button.text())).not.toContain('编辑声明')
    expect(pushedRow?.findAll('button').map((button) => button.text())).toContain('编辑声明')
  })

  it('refuses partial unverified and extra-field capability documents before editing', async () => {
    const partialUnverified = {
      ...CONNECTOR,
      name: '部分未验证连接器',
      capability: { verification: 'unverified', delivery: 'pushed' },
    } as unknown as ConnectorView
    const measuredWithExtraField = {
      ...CONNECTOR,
      id: 'connector-extra',
      name: '带额外事实的连接器',
      capability: {
        verification: 'measured',
        delivery: 'pushed',
        polling_interval_seconds: null,
        max_delivery_delay_seconds: 0.2,
        sequencing: 'sequenced',
        edge_preservation: 'preserved',
        timestamp_source: 'host_receipt',
        future_fact: 'must-not-be-dropped',
      },
    } as unknown as ConnectorView
    const session = useSessionStore()
    session.current = {
      user_id: 'capability-viewer-4',
      login_name: 'viewer',
      display_name: '能力查看者',
      expires_at: '2026-09-07T13:00:00Z',
      permissions: ['device.connector.view', 'device.connector.edit'],
    }

    const wrapper = mount(PointManagement, {
      props: { connectors: [partialUnverified, measuredWithExtraField], stations: [STATION] },
      global: { plugins: [ElementPlus] },
    })
    await flushPromises()

    const table = wrapper.find('table[aria-label="连接器能力声明清单"]')
    for (const name of ['部分未验证连接器', '带额外事实的连接器']) {
      const row = table.findAll('tbody tr').find((candidate) => candidate.text().includes(name))
      expect(row?.findAll('button').map((button) => button.text())).not.toContain('编辑声明')
    }
    expect(wrapper.text()).toContain('未知值，不能编辑')
  })

  it('validates a no-point action path without fetching a connector', async () => {
    api.validatePointBinding.mockResolvedValue({ accepted: true, reasons: [] })
    const session = useSessionStore()
    session.current = {
      user_id: 'validator-1',
      login_name: 'validator',
      display_name: '绑定校验员',
      expires_at: '2026-09-07T13:00:00Z',
      permissions: ['device.point.view'],
    }

    const wrapper = mount(PointManagement, {
      attachTo: document.body,
      global: { plugins: [ElementPlus] },
    })
    await flushPromises()
    await wrapper.find('input[name="validation-station-id"]').setValue('station-1')
    await wrapper.find('input[name="validation-budget-seconds"]').setValue('0.5')
    await wrapper.find('select[name="validation-role"]').setValue('ordered_step')
    await wrapper.find('.validation-form').trigger('submit')
    await flushPromises()

    expect(api.readConnectors).not.toHaveBeenCalled()
    expect(api.validatePointBinding).toHaveBeenCalledWith({
      station_id: 'station-1',
      point_id: null,
      role: 'ordered_step',
      budget_seconds: 0.5,
    })
    expect(wrapper.text()).toContain('可以绑定')
  })

  it('renders every binding refusal reason including an unknown future code', async () => {
    api.validatePointBinding.mockResolvedValue({
      accepted: false,
      reasons: [
        { code: 'point_required', field: 'point_id', message: '安全输出必须选择点位' },
        {
          code: 'future_reason' as never,
          field: 'capability.delivery',
          message: '未来版本的具体原因',
        },
      ],
    })
    const session = useSessionStore()
    session.current = {
      user_id: 'validator-2',
      login_name: 'validator',
      display_name: '绑定校验员',
      expires_at: '2026-09-07T13:00:00Z',
      permissions: ['device.point.view'],
    }

    const wrapper = mount(PointManagement, {
      attachTo: document.body,
      global: { plugins: [ElementPlus] },
    })
    await flushPromises()
    await wrapper.find('input[name="validation-station-id"]').setValue('station-1')
    await wrapper.find('input[name="validation-budget-seconds"]').setValue('1')
    await wrapper.find('select[name="validation-role"]').setValue('safety_output')
    await wrapper.find('.validation-form').trigger('submit')
    await flushPromises()

    expect(wrapper.text()).toContain('不能绑定')
    expect(wrapper.text()).toContain('point_required')
    expect(wrapper.text()).toContain('point_id')
    expect(wrapper.text()).toContain('安全输出必须选择点位')
    expect(wrapper.text()).toContain('future_reason')
    expect(wrapper.text()).toContain('capability.delivery')
    expect(wrapper.text()).toContain('未来版本的具体原因')
  })

  it('accepts an explicitly entered zero-second binding budget', async () => {
    api.validatePointBinding.mockResolvedValue({ accepted: true, reasons: [] })
    const session = useSessionStore()
    session.current = {
      user_id: 'validator-zero',
      login_name: 'validator',
      display_name: '绑定校验员',
      expires_at: '2026-09-07T13:00:00Z',
      permissions: ['device.point.view'],
    }

    const wrapper = mount(PointManagement, {
      attachTo: document.body,
      global: { plugins: [ElementPlus] },
    })
    await flushPromises()
    await wrapper.find('input[name="validation-station-id"]').setValue('station-1')
    await wrapper.find('input[name="validation-budget-seconds"]').setValue('0')
    await wrapper.find('select[name="validation-role"]').setValue('ordered_step')
    await wrapper.find('.validation-form').trigger('submit')
    await flushPromises()

    expect(api.validatePointBinding).toHaveBeenCalledWith({
      station_id: 'station-1',
      point_id: null,
      role: 'ordered_step',
      budget_seconds: 0,
    })
    expect(wrapper.text()).toContain('可以绑定')
  })

  it('requires a binding budget instead of silently supplying one', async () => {
    api.validatePointBinding.mockResolvedValue({ accepted: true, reasons: [] })
    const session = useSessionStore()
    session.current = {
      user_id: 'validator-3',
      login_name: 'validator',
      display_name: '绑定校验员',
      expires_at: '2026-09-07T13:00:00Z',
      permissions: ['device.point.view'],
    }

    const wrapper = mount(PointManagement, {
      attachTo: document.body,
      global: { plugins: [ElementPlus] },
    })
    await flushPromises()
    await wrapper.find('input[name="validation-station-id"]').setValue('station-1')
    await wrapper.find('select[name="validation-role"]').setValue('start_signal')
    await wrapper.find('.validation-form').trigger('submit')
    await flushPromises()

    expect(api.validatePointBinding).not.toHaveBeenCalled()
    expect(wrapper.find('[role="alert"]').text()).toContain('预算秒数为必填项')
  })
})
