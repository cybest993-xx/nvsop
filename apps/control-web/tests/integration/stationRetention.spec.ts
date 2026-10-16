import ElementPlus from 'element-plus'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'

import { ControlPlaneError } from '@/api/controlPlane'
import StationRetentionPanel from '@/modules/devices/StationRetentionPanel.vue'
import { useSessionStore } from '@/session/store'

const api = vi.hoisted(() => ({
  readRetentionPolicy: vi.fn(),
  readStationRetention: vi.fn(),
  estimateRetentionPolicyImpact: vi.fn(),
  estimateStationRetentionImpact: vi.fn(),
  updateRetentionPolicy: vi.fn(),
  updateStationRetention: vi.fn(),
}))

vi.mock('@/api/controlPlane', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/controlPlane')>()),
  ...api,
}))

type Rule = { mode: 'duration' | 'permanent' | 'discard'; seconds: number | null }

function rule(mode: Rule['mode'], seconds: number | null): Rule {
  return { mode, seconds }
}

const DEFAULT_POLICY = {
  evidence_pass: rule('duration', 7776000),
  evidence_fail: rule('permanent', null),
  evidence_indeterminate: rule('duration', 7776000),
  record_detail: rule('duration', 7776000),
  record_compression_age: rule('duration', 604800),
  record_aggregate_age: rule('duration', 7776000),
  disposal: rule('permanent', null),
  local_reported: rule('duration', 1209600),
}

const CUSTOM_POLICY = { ...DEFAULT_POLICY, evidence_pass: rule('discard', null) }

// 一次真实形状的影响估算：证据面新暴露 3 个对象、其中 1 个受引用保护跳过。
const IMPACT = {
  baseline_revision: 0,
  proposed: DEFAULT_POLICY,
  station_id: null,
  complete: true,
  incomplete_reasons: [],
  fields: [
    {
      field: 'evidence_pass' as const,
      owner: 'evidence' as const,
      category: 'pass' as const,
      removes_objects: true,
      unavailable_reason: null,
      savings_unknown: false,
      bytes_basis: null,
      source_configurations: [],
      exposed_from: '2026-07-16T00:00:00Z',
      exposed_to: '2026-09-14T00:00:00Z',
      exposed: {
        objects: 3,
        known_bytes: 2048,
        unknown_size_objects: 0,
        earliest: '2026-08-01T00:00:00Z',
        latest: '2026-09-01T00:00:00Z',
      },
      protected: {
        objects: 1,
        known_bytes: 1024,
        unknown_size_objects: 0,
        earliest: '2026-08-05T00:00:00Z',
        latest: '2026-08-05T00:00:00Z',
      },
    },
  ],
  reference_time: '2026-10-14T00:00:00Z',
  estimate_token: 'token-1',
}

const STATIONS = [
  {
    id: 'station-1',
    code: 'A-01',
    name: '一号装配工位',
    tags: [],
    status: 'active' as const,
    revision: 4,
    created_by: '018f-1',
    updated_by: '018f-1',
    created_at: '2026-09-07T04:00:00Z',
    updated_at: '2026-09-07T04:00:00Z',
  },
]

function stationView(mode: 'follow_global' | 'custom') {
  return {
    station_id: 'station-1',
    // 覆盖记录版本：0 表示该工位尚无记录，整组跟随全局默认。
    revision: mode === 'custom' ? 2 : 0,
    mode,
    default: DEFAULT_POLICY,
    overrides: mode === 'custom' ? CUSTOM_POLICY : null,
    effective: mode === 'custom' ? CUSTOM_POLICY : DEFAULT_POLICY,
  }
}

function mountPanel(permissions: string[], stations = STATIONS) {
  useSessionStore().current = {
    user_id: '018f-1',
    login_name: 'wang.li',
    display_name: '王丽',
    expires_at: '2026-09-07T13:00:00+00:00',
    permissions,
  }
  return mount(StationRetentionPanel, {
    props: { stations },
    global: { plugins: [ElementPlus] },
  })
}

// 渲染顺序：0 是工位选择，1 是「工位保留来源」。Element Plus 把 aria-label 放在内部输入上，
// 因此按组件顺序取，不按属性取。
function stationModeSelect(wrapper: VueWrapper) {
  const selects = wrapper.findAllComponents({ name: 'ElSelect' })
  expect(selects.length).toBeGreaterThan(1)
  return selects[1]!
}

function button(wrapper: VueWrapper, label: string) {
  const found = wrapper.findAll('button').find((item) => item.text() === label)
  expect(found, `button ${label}`).toBeDefined()
  return found!
}

beforeEach(() => {
  setActivePinia(createPinia())
  vi.clearAllMocks()
  api.readRetentionPolicy.mockResolvedValue({ policy: DEFAULT_POLICY, revision: 0 })
  api.readStationRetention.mockResolvedValue(stationView('follow_global'))
  api.estimateStationRetentionImpact.mockResolvedValue(IMPACT)
  api.estimateRetentionPolicyImpact.mockResolvedValue(IMPACT)
})

describe('工位保留策略面板', () => {
  it('reads a known station and confirms with the estimate token', async () => {
    api.updateStationRetention.mockResolvedValue({ ...stationView('follow_global'), revision: 1 })
    const wrapper = mountPanel(['retention.policy.view', 'retention.policy.edit'], [])
    await flushPromises()

    expect(api.readStationRetention).not.toHaveBeenCalled()
    await wrapper.get('input[aria-label="工位 ID"]').setValue('station-1')
    await button(wrapper, '读取工位保留组').trigger('click')
    await flushPromises()
    expect(api.readStationRetention).toHaveBeenCalledWith('station-1')

    await button(wrapper, '查看影响估算').trigger('click')
    await flushPromises()
    expect(api.estimateStationRetentionImpact).toHaveBeenCalledWith('station-1', {
      mode: 'follow_global',
      overrides: null,
    })
    // 确认前不更新策略：只有点击确认才提交，并带回估算令牌。
    expect(api.updateStationRetention).not.toHaveBeenCalled()
    await button(wrapper, '确认保存工位保留组').trigger('click')
    await flushPromises()
    expect(api.updateStationRetention).toHaveBeenCalledWith(
      'station-1',
      { mode: 'follow_global', overrides: null },
      0,
      'token-1',
    )
  })

  it('shows the estimated exposure and the protected skips before confirming', async () => {
    const wrapper = mountPanel(['retention.policy.view', 'retention.policy.edit'])
    await flushPromises()

    await button(wrapper, '查看影响估算').trigger('click')
    await flushPromises()

    expect(wrapper.text()).toContain('即将缩短的影响')
    expect(wrapper.text()).toContain('新暴露 3 个对象')
    expect(wrapper.text()).toContain('引用保护跳过 1 个')
    expect(wrapper.text()).toContain('证据：通过类')
  })

  it('keeps unknown owner facts visible and blocks confirmation rather than showing zero', async () => {
    api.estimateStationRetentionImpact.mockResolvedValue({
      ...IMPACT,
      complete: false,
      incomplete_reasons: ['local_reported:edge_estimate_refused'],
      fields: [
        {
          ...IMPACT.fields[0],
          field: 'local_reported',
          owner: 'edge',
          category: null,
          exposed: null,
          protected: null,
          unavailable_reason: 'edge_estimate_refused',
          bytes_basis: 'serialized_rows',
          source_configurations: [
            { host_id: 'host-1', configuration_revision: 7, configuration_sha256: 'a'.repeat(64) },
          ],
        },
      ],
    })
    const wrapper = mountPanel(['retention.policy.view', 'retention.policy.edit'])
    await flushPromises()
    await button(wrapper, '查看影响估算').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('估算不完整，不能确认')
    expect(wrapper.text()).toContain('edge_estimate_refused')
    expect(wrapper.text()).toContain('host-1')
    expect(wrapper.text()).toContain('逻辑序列化字节，不代表可回收磁盘空间')
    expect(button(wrapper, '确认保存工位保留组').attributes('disabled')).toBeDefined()
    await button(wrapper, '确认保存工位保留组').trigger('click')
    expect(api.updateStationRetention).not.toHaveBeenCalled()
  })

  it('cancelling a pending confirmation changes nothing', async () => {
    const wrapper = mountPanel(['retention.policy.view', 'retention.policy.edit'])
    await flushPromises()

    await button(wrapper, '查看影响估算').trigger('click')
    await flushPromises()
    await button(wrapper, '取消').trigger('click')
    await flushPromises()

    expect(wrapper.text()).not.toContain('即将缩短的影响')
    expect(api.updateStationRetention).not.toHaveBeenCalled()
  })

  it('surfaces an estimate failure and never falls through to a save', async () => {
    api.estimateStationRetentionImpact.mockRejectedValue(new Error('估算不可用'))
    const wrapper = mountPanel(['retention.policy.view', 'retention.policy.edit'])
    await flushPromises()

    await button(wrapper, '查看影响估算').trigger('click')
    await flushPromises()

    expect(wrapper.get('[role="alert"]').text()).toContain('估算不可用')
    expect(wrapper.findAll('button').some((item) => item.text() === '确认保存工位保留组')).toBe(
      false,
    )
    expect(api.updateStationRetention).not.toHaveBeenCalled()
  })

  it('shows the global default, the effective group and the unset wording', async () => {
    const wrapper = mountPanel(['retention.policy.view'])
    await flushPromises()

    expect(wrapper.text()).toContain('尚未设置')
    expect(wrapper.text()).toContain('永久')
    expect(wrapper.text()).toContain('整组等于全局默认组')
    expect(wrapper.text()).toContain('覆盖记录 revision 0')
    expect(api.readStationRetention).toHaveBeenCalledWith('station-1')
  })

  it('shows the explicit discard wording of a custom group without saving anything', async () => {
    api.readStationRetention.mockResolvedValue(stationView('custom'))
    const wrapper = mountPanel(['retention.policy.view', 'retention.policy.edit'])
    await flushPromises()

    expect(wrapper.text()).toContain('显式不保留')
    expect(wrapper.text()).toContain('整组自定义')
    expect(api.updateStationRetention).not.toHaveBeenCalled()
  })

  it('saves the complete custom group with the station revision on confirmation', async () => {
    api.updateStationRetention.mockResolvedValue(stationView('custom'))
    const wrapper = mountPanel(['retention.policy.view', 'retention.policy.edit'])
    await flushPromises()

    await stationModeSelect(wrapper).setValue('custom')
    await flushPromises()

    // 默认组里 6 条是有界时长、2 条是永久，因此时长输入框只有 6 个；顺序与字段顺序一致。
    const secondsInputs = wrapper.findAll('input[placeholder="秒"]')
    expect(secondsInputs.length).toBe(6)
    await secondsInputs[0]!.setValue('120')
    await button(wrapper, '查看影响估算').trigger('click')
    await flushPromises()
    await button(wrapper, '确认保存工位保留组').trigger('click')
    await flushPromises()

    expect(api.updateStationRetention).toHaveBeenCalledTimes(1)
    const [stationId, submitted, revision, token] = api.updateStationRetention.mock.calls[0]!
    expect(stationId).toBe('station-1')
    // If-Match 是该工位覆盖记录版本，不是设备工位修订。
    expect(revision).toBe(0)
    expect(token).toBe('token-1')

    expect(submitted.mode).toBe('custom')
    const overrides = submitted.overrides as Record<string, Rule>
    expect(Object.keys(overrides).sort()).toEqual([
      'disposal',
      'evidence_fail',
      'evidence_indeterminate',
      'evidence_pass',
      'local_reported',
      'record_aggregate_age',
      'record_compression_age',
      'record_detail',
    ])
    expect(overrides.evidence_pass).toEqual({ mode: 'duration', seconds: 120 })
    expect(overrides.evidence_fail).toEqual({ mode: 'permanent', seconds: null })
  })

  it('does not reuse the previous station form after selecting a station that cannot be read', async () => {
    const wrapper = mountPanel(['retention.policy.view', 'retention.policy.edit'])
    await flushPromises()
    await wrapper.setProps({
      stations: [
        ...STATIONS,
        { ...STATIONS[0]!, id: 'station-2', code: 'A-02', name: '二号装配工位' },
      ],
    })
    api.readStationRetention.mockRejectedValue(new Error('二号工位读取失败'))

    await wrapper.findAllComponents({ name: 'ElSelect' })[0]!.setValue('station-2')
    await flushPromises()

    expect(wrapper.get('[role="alert"]').text()).toContain('二号工位读取失败')
    expect(wrapper.find('form[aria-label="保存保留组"]').exists()).toBe(false)
    expect(api.updateStationRetention).not.toHaveBeenCalled()
  })

  it('surfaces a stale estimate or revision conflict instead of a false success', async () => {
    api.updateStationRetention.mockRejectedValue(
      new ControlPlaneError({
        message: '没有执行该操作的权限',
        errorCode: 'RETENTION_ESTIMATE_STALE',
        status: 409,
        detail: '保留影响估算已过期，请重新估算后再确认',
      }),
    )
    const wrapper = mountPanel(['retention.policy.view', 'retention.policy.edit'])
    await flushPromises()

    await button(wrapper, '查看影响估算').trigger('click')
    await flushPromises()
    await button(wrapper, '确认保存工位保留组').trigger('click')
    await flushPromises()

    expect(wrapper.text()).toContain('保留影响估算已过期，请重新估算后再确认')
    // 失败后挂起的确认被丢弃，界面回到「查看影响估算」。
    expect(wrapper.text()).not.toContain('即将缩短的影响')
  })

  it('renders nothing and reads nothing without the retention view permission', async () => {
    const wrapper = mountPanel([])
    await flushPromises()

    expect(wrapper.find('section').exists()).toBe(false)
    expect(wrapper.text()).toBe('')
    expect(api.readRetentionPolicy).not.toHaveBeenCalled()
    expect(api.readStationRetention).not.toHaveBeenCalled()
  })
})
