/**
 * 强制改绑双人确认面板的展示与客户端防误状态。
 *
 * 后端仍是唯一权威：这些用例只证明面板不会在风险未读、内容变化或身份切换时沿用旧确认，
 * 也不会把旧响应覆盖到当前输入。业务拒绝（同人/已完成/内容不符）由后端判定，面板只据
 * 记录字段禁用与解释。
 */

import ElementPlus from 'element-plus'
import { ElCheckbox } from 'element-plus'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { nextTick } from 'vue'

import { ControlPlaneError } from '@/api/controlPlane'
import ForceHandoverPanel from '@/modules/devices/ForceHandoverPanel.vue'
import { useSessionStore } from '@/session/store'

const api = vi.hoisted(() => ({
  readHandoverRisk: vi.fn(),
  createHandover: vi.fn(),
  readHandover: vi.fn(),
  confirmHandover: vi.fn(),
}))

vi.mock('@/api/controlPlane', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/controlPlane')>()),
  ...api,
}))

const RISK = '若旧机仍在运行将造成重复物理写入'

const RECORD = {
  handover_id: 'handover-1',
  station_id: 'station-1',
  from_host_id: 'host-old',
  to_host_id: 'host-new',
  operator_id: 'operator-1',
  operator_confirmed_at: '2026-09-22T01:00:00Z',
  operator_risk_shown: true,
  second_operator_id: null,
  second_confirmed_at: null,
  second_risk_shown: null,
  risk_statement: RISK,
}

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason?: unknown) => void
  const promise = new Promise<T>((promiseResolve, promiseReject) => {
    resolve = promiseResolve
    reject = promiseReject
  })
  return { promise, resolve, reject }
}

function setIdentity(userId: string): void {
  const session = useSessionStore()
  session.current = {
    user_id: userId,
    login_name: userId,
    display_name: userId,
    expires_at: '2026-09-22T09:00:00Z',
    permissions: ['execution.handover.edit'],
  }
}

function mountPanel(): VueWrapper {
  return mount(ForceHandoverPanel, { global: { plugins: [ElementPlus] } })
}

function buttonWith(wrapper: VueWrapper, text: string) {
  const button = wrapper.findAll('button').find((candidate) => candidate.text().includes(text))
  if (button === undefined) {
    throw new Error(`no button containing ${text}`)
  }
  return button
}

async function setCheckbox(wrapper: VueWrapper, index: number, value: boolean): Promise<void> {
  const boxes = wrapper.findAllComponents(ElCheckbox)
  const box = boxes[index]
  if (box === undefined) {
    throw new Error(`no checkbox at index ${index}`)
  }
  await box.find('input').setValue(value)
}

beforeEach(() => {
  setActivePinia(createPinia())
  vi.clearAllMocks()
  api.readHandoverRisk.mockResolvedValue({ risk_statement: RISK })
})

describe('强制改绑双人确认面板', () => {
  it('建立请求后展示冻结内容，并由另一名用户确认同一内容', async () => {
    setIdentity('operator-1')
    api.createHandover.mockResolvedValue({ ...RECORD, operator_id: 'operator-1' })
    api.readHandover.mockResolvedValue({ ...RECORD, operator_id: 'operator-1' })
    api.confirmHandover.mockResolvedValue({
      ...RECORD,
      operator_id: 'operator-1',
      second_operator_id: 'second-1',
      second_confirmed_at: '2026-09-22T01:05:00Z',
      second_risk_shown: true,
    })

    const wrapper = mountPanel()
    await flushPromises()
    expect(wrapper.text()).toContain(RISK)

    // 没有查看权限时按已知 ID 填写（force-only 用户）。
    await wrapper.find('#handover-station-id').setValue('station-1')
    await wrapper.find('#handover-from-host-id').setValue('host-old')
    await wrapper.find('#handover-to-host-id').setValue('host-new')
    await setCheckbox(wrapper, 0, true)
    await nextTick()

    await buttonWith(wrapper, '建立并完成第一确认').trigger('click')
    await flushPromises()

    expect(api.createHandover).toHaveBeenCalledWith({
      station_id: 'station-1',
      from_host_id: 'host-old',
      to_host_id: 'host-new',
      risk_acknowledgement: RISK,
    })
    expect(wrapper.text()).toContain('等待另一名有权用户确认')
    expect(wrapper.text()).toContain('handover-1')

    // 第二名用户换一个身份登录：面板不沿用上一身份的勾选。
    setIdentity('second-1')
    await nextTick()
    expect(wrapper.find('.handover__record').exists()).toBe(false)

    await wrapper.find('#handover-request-id').setValue('handover-1')
    await buttonWith(wrapper, '读取请求').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('等待另一名有权用户确认')

    await setCheckbox(wrapper, 1, true)
    await nextTick()
    await buttonWith(wrapper, '完成第二确认').trigger('click')
    await flushPromises()

    expect(api.confirmHandover).toHaveBeenCalledWith('handover-1', {
      station_id: 'station-1',
      from_host_id: 'host-old',
      to_host_id: 'host-new',
      risk_acknowledgement: RISK,
    })
    expect(wrapper.text()).toContain('审批已满足，尚未切换执行权')
  })

  it('请求 ID 变化清掉旧确认，进行中的旧响应不覆盖当前输入', async () => {
    setIdentity('second-1')
    const first = deferred<typeof RECORD>()
    api.readHandover.mockReturnValueOnce(first.promise)

    const wrapper = mountPanel()
    await flushPromises()

    await wrapper.find('#handover-request-id').setValue('handover-1')
    await buttonWith(wrapper, '读取请求').trigger('click')
    await nextTick()

    // 读取尚未返回时改输入：旧响应到达后不得渲染。
    await wrapper.find('#handover-request-id').setValue('handover-2')
    first.resolve(RECORD)
    await flushPromises()
    expect(wrapper.find('.handover__record').exists()).toBe(false)

    // 已加载并勾选后再改 ID：旧确认清掉，确认按钮消失。
    api.readHandover.mockResolvedValue({ ...RECORD, operator_id: 'operator-1' })
    await buttonWith(wrapper, '读取请求').trigger('click')
    await flushPromises()
    await setCheckbox(wrapper, 1, true)
    await nextTick()
    expect(wrapper.find('.handover__confirm').exists()).toBe(true)

    await wrapper.find('#handover-request-id').setValue('handover-3')
    await nextTick()
    expect(wrapper.find('.handover__confirm').exists()).toBe(false)
  })

  it('操作者本人与已完成的请求不提供第二确认，并解释原因', async () => {
    setIdentity('operator-1')
    api.readHandover.mockResolvedValue({ ...RECORD, operator_id: 'operator-1' })
    const wrapper = mountPanel()
    await flushPromises()
    await wrapper.find('#handover-request-id').setValue('handover-1')
    await buttonWith(wrapper, '读取请求').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('你是本请求的操作者')
    expect(wrapper.find('.handover__confirm').exists()).toBe(false)

    api.readHandover.mockResolvedValue({
      ...RECORD,
      operator_id: 'operator-1',
      second_operator_id: 'second-1',
      second_confirmed_at: '2026-09-22T01:05:00Z',
      second_risk_shown: true,
    })
    await buttonWith(wrapper, '读取请求').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('该请求已完成两人确认')
    expect(wrapper.text()).toContain('审批已满足，尚未切换执行权')
    expect(wrapper.find('.handover__confirm').exists()).toBe(false)
  })

  it('服务器风险原文读取失败时不能提交', async () => {
    setIdentity('operator-1')
    api.readHandoverRisk.mockRejectedValue(
      new ControlPlaneError({
        message: '没有执行该操作的权限',
        errorCode: 'PERMISSION_DENIED',
        status: 403,
      }),
    )
    const wrapper = mountPanel()
    await flushPromises()
    expect(wrapper.text()).toContain('无法读取服务器风险原文')
    expect(wrapper.find('#handover-station-id').exists()).toBe(false)
    expect(api.createHandover).not.toHaveBeenCalled()
  })
})
