<script setup lang="ts">
import { reactive } from 'vue'

import * as api from '@/api/controlPlane'
import type { ConnectorCapability, ConnectorView } from '@/api/controlPlane'
import FieldControl from './FieldControl.vue'

const options = (values: Record<string, string>): [string, string][] => Object.entries(values)
const delivery = options({ pushed: '推送', polled: '轮询' })
const sequencing = options({ sequenced: '保序', unsequenced: '不保序' })
const edge = options({ preserved: '保留边沿', may_drop: '可能丢边沿' })
const timestamp = options({ device_clock: '设备时钟', host_receipt: '主机接收时刻' })
const fields = [
  ['delivery', '投递 / 周期', delivery],
  ['polling_interval_seconds', '轮询周期'],
  ['max_delivery_delay_seconds', '最大延迟'],
  ['sequencing', '保序', sequencing],
  ['edge_preservation', '边沿', edge],
  ['timestamp_source', '时间戳来源', timestamp],
] as const
type Field = (typeof fields)[number]
type Key = Field[0]
type MeasuredCapability = Extract<ConnectorCapability, { verification: 'measured' }>

const emit = defineEmits<{ changed: [] }>()
defineProps<{ canView: boolean; canEdit: boolean; connectors: ConnectorView[] }>()
const targetFields = [
  ['id', 'known-capability-connector-id', '连接器 ID', 'text'],
  ['revision', 'known-capability-revision', '已知修订号', 'number'],
] as const
const modes = options({ unverified: '保持未验证', measured: '录入实测能力' })
const state = reactive({
  draft: Object.fromEntries(fields.map(([key]) => [key, ''])) as Record<Key, string>,
  target: { id: '', revision: '' },
  selected: null as ConnectorView | null,
  mode: 'unverified',
  dialog: false,
  failure: '',
  notice: '',
})
function raw(value: unknown): string {
  if (value == null || value === '') return '未提供'
  return typeof value === 'string'
    ? value
    : (JSON.stringify(value) ?? Object.prototype.toString.call(value))
}
const values = (item: ConnectorView | null): Record<string, unknown> =>
  typeof item?.capability === 'object' && item.capability !== null ? { ...item.capability } : {}
const known = (field: Field, value: unknown): boolean =>
  field[2]?.some(([key]) => key === value) ?? false
const verificationText = (value: unknown): string =>
  (({ unverified: '未验证', measured: '已实测' }) as Record<string, string>)[String(value)] ??
  `未知能力声明（${raw(value)}）`
function display(item: ConnectorView, field: Field): string {
  const value = values(item)
  const text = raw(value[field[0]])
  if (field[0] === 'polling_interval_seconds' && value.delivery === 'pushed') return '不适用'
  if (field[0] === 'polling_interval_seconds' || field[0] === 'max_delivery_delay_seconds')
    return text === '未提供' ? text : `${text} 秒`
  if (text === '未提供') return text
  return field[2]?.find(([key]) => key === value[field[0]])?.[1] ?? `未知${field[1]}（${text}）`
}
const capabilityKeys = ['verification', ...fields.map(([key]) => key)]
const hasOnlyKeys = (value: Record<string, unknown>, keys: readonly string[]): boolean => {
  const actual = Object.keys(value)
  return actual.length === keys.length && keys.every((key) => Object.hasOwn(value, key))
}
const valid = (field: Field, value: unknown): boolean =>
  field[0] === 'polling_interval_seconds'
    ? value === null || (typeof value === 'number' && Number.isFinite(value) && value > 0)
    : field[2]
      ? known(field, value)
      : typeof value === 'number' && Number.isFinite(value) && value >= 0
function measuredDocument(value: Record<string, unknown>): boolean {
  if (!hasOnlyKeys(value, capabilityKeys) || value.verification !== 'measured') return false
  if (!fields.every((field) => valid(field, value[field[0]]))) return false
  const delay = value.max_delivery_delay_seconds
  const interval = value.polling_interval_seconds
  return (
    typeof delay === 'number' &&
    Number.isFinite(delay) &&
    (value.delivery === 'pushed'
      ? interval === null
      : typeof interval === 'number' && interval <= delay)
  )
}
function editable(item: ConnectorView): boolean {
  const value = values(item)
  if (value.verification === 'unverified') return hasOnlyKeys(value, ['verification'])
  return measuredDocument(value)
}
function openEditor(item: ConnectorView | null): void {
  if (item !== null && !editable(item)) {
    state.failure = '能力声明含未知值，不能编辑；请先由服务端完成兼容处理'
    return
  }
  const value = values(item)
  state.mode = value.verification === 'measured' ? 'measured' : 'unverified'
  fields.forEach(
    ([key]) =>
      (state.draft[key] = state.mode === 'measured' ? raw(value[key]).replace('未提供', '') : ''),
  )
  state.target.id = item?.id ?? state.target.id
  state.target.revision = item ? String(item.revision) : state.target.revision
  Object.assign(state, { selected: item, failure: '', notice: '', dialog: true })
}
function measured(): ConnectorCapability | null {
  if (fields.some((field) => field[2] && !known(field, state.draft[field[0]]))) {
    state.failure = '请填写完整的实测枚举值'
    return null
  }
  const delay = Number(state.draft.max_delivery_delay_seconds)
  const interval =
    state.draft.delivery === 'polled' ? Number(state.draft.polling_interval_seconds) : null
  if (
    !Number.isFinite(delay) ||
    delay < 0 ||
    (interval !== null && (!Number.isFinite(interval) || interval <= 0 || delay < interval))
  ) {
    state.failure = '轮询周期必须为正数且不大于最大投递延迟'
    return null
  }
  return {
    verification: 'measured',
    delivery: state.draft.delivery as MeasuredCapability['delivery'],
    polling_interval_seconds: interval,
    max_delivery_delay_seconds: delay,
    sequencing: state.draft.sequencing as MeasuredCapability['sequencing'],
    edge_preservation: state.draft.edge_preservation as MeasuredCapability['edge_preservation'],
    timestamp_source: state.draft.timestamp_source as MeasuredCapability['timestamp_source'],
  }
}
async function submit(): Promise<void> {
  const id = state.target.id.trim()
  const revision = Number(String(state.target.revision).trim())
  if (!id) return void (state.failure = '请输入连接器 ID')
  if (!Number.isInteger(revision) || revision < 1)
    return void (state.failure = '修订号必须是正整数')
  const capability =
    state.mode === 'unverified' ? { verification: 'unverified' as const } : measured()
  if (!capability) return
  try {
    await api.updateConnectorCapability(id, capability, revision)
  } catch (error) {
    if (!(error instanceof api.ControlPlaneError)) throw error
    state.failure = error.detail ?? error.message
    return
  }
  state.dialog = false
  state.notice = state.mode === 'measured' ? '能力声明已保存' : '已保持未验证'
  emit('changed')
}
const control = (field: Field) => ({
  id: field[0].replaceAll('_', '-'),
  label: field[1],
  options: field[2],
  type: field[0].endsWith('_seconds') ? 'number' : undefined,
  min: field[0] === 'max_delivery_delay_seconds' ? 0 : undefined,
  placeholder:
    field[0] === 'polling_interval_seconds'
      ? '轮询时必填；推送留空'
      : field[0] === 'max_delivery_delay_seconds'
        ? '必填'
        : undefined,
  required: field[0] !== 'polling_interval_seconds' || state.draft.delivery === 'polled',
})
</script>

<template>
  <section aria-labelledby="capability-heading">
    <h3 id="capability-heading">连接器能力声明</h3>
    <table v-if="canView && connectors.length" aria-label="连接器能力声明清单">
      <thead>
        <tr>
          <th scope="col">连接器</th>
          <th scope="col">验证状态</th>
          <th v-for="field in fields" :key="field[0]" scope="col">{{ field[1] }}</th>
          <th scope="col">操作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="connector in connectors" :key="connector.id">
          <th scope="row">{{ connector.name }}</th>
          <td>{{ verificationText(values(connector).verification) }}</td>
          <td v-for="field in fields" :key="field[0]">{{ display(connector, field) }}</td>
          <td>
            <button
              v-if="canEdit && editable(connector)"
              type="button"
              @click="openEditor(connector)"
            >
              编辑声明
            </button>
            <span v-else-if="canEdit">未知值，不能编辑</span>
          </td>
        </tr>
      </tbody>
    </table>
    <p v-else-if="canView">没有可显示的连接器能力声明。</p>
    <form v-else-if="canEdit" class="direct-form" @submit.prevent="openEditor(null)">
      <FieldControl
        v-for="field in targetFields"
        :id="field[1]"
        :key="field[0]"
        v-model="state.target[field[0]]"
        :label="field[2]"
        :type="field[3]"
      />
      <button type="submit">录入能力声明</button>
    </form>
    <p v-else role="status">能力声明需要连接器查看权限；当前不会请求连接器资源。</p>
    <p v-if="state.failure" role="alert">{{ state.failure }}</p>
    <p v-if="state.notice" role="status">{{ state.notice }}</p>
    <dialog v-if="state.dialog" open class="modal" aria-labelledby="capability-dialog-heading">
      <h3 id="capability-dialog-heading">
        {{ state.selected ? `编辑 ${state.selected.name} 的能力声明` : '录入连接器能力声明' }}
      </h3>
      <button
        v-for="[mode, label] in modes"
        :key="mode"
        type="button"
        :class="{ selected: state.mode === mode }"
        @click="state.mode = mode"
      >
        {{ label }}
      </button>
      <form @submit.prevent="submit">
        <template v-if="state.mode === 'measured'">
          <FieldControl
            v-for="field in fields"
            :key="field[0]"
            v-model="state.draft[field[0]]"
            v-bind="control(field)"
          />
        </template>
        <button type="button" @click="state.dialog = false">取消</button>
        <button type="submit">
          {{ state.mode === 'measured' ? '保存实测能力' : '保存未验证状态' }}
        </button>
      </form>
    </dialog>
  </section>
</template>
