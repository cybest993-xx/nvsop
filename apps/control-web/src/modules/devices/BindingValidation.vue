<script setup lang="ts">
import { computed, reactive, ref } from 'vue'

import * as api from '@/api/controlPlane'
import type {
  BindingValidationRequest,
  BindingValidationView,
  StationView,
} from '@/api/controlPlane'
import FieldControl, { type FieldProps } from './FieldControl.vue'

type Props = { canView: boolean; canViewStations: boolean; stations: StationView[] }
type FieldKey = 'station_id' | 'point_id' | 'role' | 'budget_seconds'

const props = defineProps<Props>()
const draft = reactive({ station_id: '', point_id: '', role: '', budget_seconds: '' })
const loading = ref(false)
const failure = ref('')
const result = ref<BindingValidationView | null>(null)
const reasons = computed(() => result.value?.reasons ?? [])
type Role = BindingValidationRequest['role']
const roles: readonly [Role, string][] = [
  ['start_signal', '开始信号'],
  ['end_signal', '结束信号'],
  ['ordered_step', '顺序步骤'],
  ['unordered_step', '无序步骤'],
  ['safety_output', '安全输出'],
]
const stationOptions = computed(() =>
  props.stations.map((item) => [item.id, `${item.name}（${item.code}）`] as const),
)
const field = (key: FieldKey, id: string, label: string, extra: Partial<FieldProps> = {}) => ({
  fieldKey: key,
  id,
  label,
  ...extra,
})
const fields = computed(() => [
  field('station_id', 'validation-station-id', '工位', {
    options: props.canViewStations ? stationOptions.value : undefined,
    placeholder: '输入已知工位 ID',
    required: true,
  }),
  field('point_id', 'validation-point-id', '点位 / 动作来源（可选）', {
    placeholder: '留空表示动作来源；安全输出不能留空',
  }),
  field('role', 'validation-role', '角色', { options: roles, required: true }),
  field('budget_seconds', 'validation-budget-seconds', '所需预算（秒）', {
    type: 'number',
    min: 0,
    placeholder: '必填',
    required: true,
  }),
])
async function submit(): Promise<void> {
  failure.value = ''
  result.value = null
  const station = draft.station_id.trim()
  if (!station) return void (failure.value = '工位为必填项')
  if (!roles.some(([key]) => key === draft.role)) return void (failure.value = '请选择点位角色')
  const text = String(draft.budget_seconds).trim()
  const budget = Number(text)
  if (!text || !Number.isFinite(budget) || budget < 0)
    return void (failure.value = '所需预算秒数为必填项，且必须是非负数')
  const request: BindingValidationRequest = {
    station_id: station,
    point_id: draft.point_id.trim() || null,
    role: draft.role as Role,
    budget_seconds: budget,
  }
  loading.value = true
  try {
    result.value = await api.validatePointBinding(request)
  } catch (error) {
    if (!(error instanceof api.ControlPlaneError)) throw error
    failure.value = error.detail ?? error.message
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <section aria-labelledby="validation-heading">
    <h3 id="validation-heading">点位角色绑定预检</h3>
    <p v-if="!canView" role="status">绑定预检需要点位查看权限；当前不会请求点位或连接器详情。</p>
    <form v-else class="validation-form" @submit.prevent="submit">
      <FieldControl
        v-for="control in fields"
        :key="control.fieldKey"
        v-model="draft[control.fieldKey as FieldKey]"
        v-bind="control"
      />
      <button type="submit">{{ loading ? '预检中…' : '执行预检' }}</button>
    </form>
    <p v-if="failure" role="alert">{{ failure }}</p>
    <strong v-if="result">{{ result.accepted ? '可以绑定' : '不能绑定' }}</strong>
    <table v-if="result && reasons.length" aria-label="绑定预检原因">
      <thead>
        <tr>
          <th v-for="header in ['原因码', '字段', '说明']" :key="header" scope="col">
            {{ header }}
          </th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="reason in reasons" :key="`${reason.code}-${reason.field}-${reason.message}`">
          <td>{{ reason.code }}</td>
          <td>{{ reason.field }}</td>
          <td>{{ reason.message }}</td>
        </tr>
      </tbody>
    </table>
  </section>
</template>
