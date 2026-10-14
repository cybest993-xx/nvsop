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

function saveButton(wrapper: VueWrapper, label: string) {
  const found = wrapper.findAll('button').find((item) => item.text() === label)
  expect(found).toBeDefined()
  return found!
}

beforeEach(() => {
  setActivePinia(createPinia())
  vi.clearAllMocks()
  api.readRetentionPolicy.mockResolvedValue({ policy: DEFAULT_POLICY, revision: 0 })
  api.readStationRetention.mockResolvedValue(stationView('follow_global'))
})

describe('工位保留策略面板', () => {
  it('reads and saves a known station without device list permissions', async () => {
    api.updateStationRetention.mockResolvedValue({ ...stationView('follow_global'), revision: 1 })
    const wrapper = mountPanel(['retention.policy.view', 'retention.policy.edit'], [])
    await flushPromises()

    expect(api.readStationRetention).not.toHaveBeenCalled()
    await wrapper.get('input[aria-label="工位 ID"]').setValue('station-1')
    await saveButton(wrapper, '读取工位保留组').trigger('click')
    await flushPromises()
    expect(api.readStationRetention).toHaveBeenCalledWith('station-1')
    await saveButton(wrapper, '保存工位保留组').trigger('click')
    await flushPromises()
    expect(api.updateStationRetention).toHaveBeenCalledWith(
      'station-1',
      { mode: 'follow_global', overrides: null },
      0,
    )
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

  it('saves the complete custom group with the station revision', async () => {
    api.updateStationRetention.mockResolvedValue(stationView('custom'))
    const wrapper = mountPanel(['retention.policy.view', 'retention.policy.edit'])
    await flushPromises()

    await stationModeSelect(wrapper).setValue('custom')
    await flushPromises()

    // 默认组里 6 条是有界时长、2 条是永久，因此时长输入框只有 6 个；顺序与字段顺序一致。
    const secondsInputs = wrapper.findAll('input[placeholder="秒"]')
    expect(secondsInputs.length).toBe(6)
    await secondsInputs[0]!.setValue('120')
    await saveButton(wrapper, '保存工位保留组').trigger('click')
    await flushPromises()

    expect(api.updateStationRetention).toHaveBeenCalledTimes(1)
    const [stationId, submitted, revision] = api.updateStationRetention.mock.calls[0]!
    expect(stationId).toBe('station-1')
    // If-Match 是该工位覆盖记录版本，不是设备工位修订。
    expect(revision).toBe(0)

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

  it('surfaces a revision conflict instead of a false success', async () => {
    api.updateStationRetention.mockRejectedValue(
      new ControlPlaneError({
        message: '没有执行该操作的权限',
        errorCode: 'RETENTION_POLICY_CONFLICT',
        status: 409,
        detail: '保留策略已被他人修改，请刷新后重试',
      }),
    )
    const wrapper = mountPanel(['retention.policy.view', 'retention.policy.edit'])
    await flushPromises()

    await saveButton(wrapper, '保存工位保留组').trigger('click')
    await flushPromises()

    expect(wrapper.text()).toContain('保留策略已被他人修改，请刷新后重试')
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
