<script setup lang="ts">
import { NButton, NForm, NInput, NSelect, NTag } from 'naive-ui'
import { computed, onMounted, onUnmounted, ref } from 'vue'
import {
  ControlPlaneError,
  cancelNormalHandover,
  createNormalHandover,
  grantNormalHandover,
  readNormalHandover,
  openRuntimeProjection,
  type NormalHandoverView,
  type StationView,
  type InferenceHostView,
  type RuntimePhysicalSafety,
} from '@/api/controlPlane'
import { useSessionStore } from '@/session/store'
import FieldRow from '@/components/FieldRow.vue'

defineProps<{ stations: StationView[]; hosts: InferenceHostView[] }>()
const session = useSessionStore()
const draft = ref({ station_id: '', from_host_id: '', to_host_id: '' })
const requestId = ref('')
const record = ref<NormalHandoverView | null>(null)
const busy = ref(false)
const failure = ref('')
const mirrors = new Map<string, RuntimePhysicalSafety>()
const mirrorVersion = ref(0)
const connected = ref(false)
let closeStream: (() => void) | undefined
let submissionIdentity = ''
let submissionKey = ''
const ready = computed(() => Object.values(draft.value).every((value) => value.trim()))
const statusLabels: Record<string, string> = {
  pending: '等待旧机停写确认',
  confirmed: '旧机已停写，等待授予新机',
  cancelled: '已取消',
  timed_out: '已超时',
  failed: '停写失败',
}
const statusLabel = computed(() =>
  record.value?.new_grant_id
    ? '已授予新机执行权'
    : (statusLabels[record.value?.status ?? ''] ?? '未知状态'),
)
const newHostState = computed(() => {
  void mirrorVersion.value
  const value = record.value
  if (!value?.new_grant_id) return ''
  if (!session.may('monitor.report.view')) return '已授予新机；当前账户无运行状态查看权限'
  const mirror = mirrors.get(value.station_id)
  return connected.value &&
    mirror?.status === 'protected' &&
    mirror.edge_status.host_id === value.to_host_id &&
    mirror.edge_status.grant_id === value.new_grant_id &&
    !mirror.edge_status.stale
    ? '新机已确认配置，物理写入门禁有效'
    : '等待新机确认配置与上报写入状态'
})
const fields = [
  { key: 'station_id' as const, label: '交权工位' },
  { key: 'from_host_id' as const, label: '旧推理机' },
  { key: 'to_host_id' as const, label: '新推理机' },
]
async function attempt(operation: () => Promise<NormalHandoverView>): Promise<void> {
  busy.value = true
  failure.value = ''
  try {
    record.value = await operation()
    requestId.value = record.value.handover_id
    if (['cancelled', 'timed_out', 'failed'].includes(record.value.status)) submissionIdentity = ''
  } catch (error) {
    failure.value =
      error instanceof ControlPlaneError
        ? `${error.detail ?? error.message}（${error.errorCode}）`
        : '请求失败，请重试'
  } finally {
    busy.value = false
  }
}
function create(): void {
  const input = Object.fromEntries(
    Object.entries(draft.value).map(([key, value]) => [key, value.trim()]),
  ) as typeof draft.value
  const identity = JSON.stringify(input)
  if (identity !== submissionIdentity) {
    submissionIdentity = identity
    submissionKey = Array.from(crypto.getRandomValues(new Uint32Array(4)), (v) =>
      v.toString(16),
    ).join('-')
  }
  void attempt(() => createNormalHandover(input, submissionKey))
}
onMounted(() => {
  if (session.may('monitor.report.view')) {
    closeStream = openRuntimeProjection(
      (value) => {
        if (value.physical_safety) mirrors.set(value.station_id, value.physical_safety)
        mirrorVersion.value++
      },
      (value) => {
        connected.value = value
      },
    )
  }
})
onUnmounted(() => closeStream?.())
</script>

<template>
  <section class="normal-handover" aria-labelledby="normal-handover-heading">
    <header>
      <h2 id="normal-handover-heading">正常交权</h2>
      <p>旧机停止工位判定与处置、释放点位并确认后，才可授予新机执行权。</p>
    </header>
    <NForm class="normal-handover__form" label-placement="top" @submit.prevent="create">
      <FieldRow v-for="field in fields" :key="field.key" :label="field.label">
        <template #default="{ id, labelId }">
          <NSelect
            v-if="field.key === 'station_id' && stations.length"
            v-model:value="draft[field.key]"
            :aria-labelledby="labelId"
            :options="stations"
            label-field="name"
            value-field="id"
          />
          <NSelect
            v-else-if="field.key !== 'station_id' && hosts.length"
            v-model:value="draft[field.key]"
            :aria-labelledby="labelId"
            :options="hosts"
            label-field="name"
            value-field="id"
          />
          <NInput
            v-else
            v-model:value="draft[field.key]"
            :input-props="{ id, 'aria-label': field.label, placeholder: '输入已知 ID' }"
          />
        </template>
      </FieldRow>
      <NButton attr-type="submit" type="primary" :disabled="busy || !ready">请求旧机交权</NButton>
    </NForm>
    <NForm
      class="normal-handover__lookup"
      @submit.prevent="attempt(() => readNormalHandover(requestId.trim()))"
    >
      <FieldRow label="正常交权请求 ID">
        <template #default="{ id }">
          <NInput v-model:value="requestId" :input-props="{ id }" />
        </template>
      </FieldRow>
      <NButton attr-type="submit" :disabled="busy || !requestId.trim()">查询 / 刷新交权</NButton>
    </NForm>
    <article v-if="record" aria-label="正常交权进度">
      <NTag
        :type="record.new_grant_id ? 'success' : record.status === 'pending' ? 'warning' : 'info'"
        >{{ statusLabel }}</NTag
      >
      <dl>
        <dt>工位</dt>
        <dd>{{ stations.find((v) => v.id === record?.station_id)?.name ?? record.station_id }}</dd>
        <dt>旧机 → 新机</dt>
        <dd>
          {{ hosts.find((v) => v.id === record?.from_host_id)?.name ?? record.from_host_id }} →
          {{ hosts.find((v) => v.id === record?.to_host_id)?.name ?? record.to_host_id }}
        </dd>
        <dt>停写确认</dt>
        <dd>
          {{
            record.confirmed_at
              ? new Date(record.confirmed_at).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai' })
              : '尚未收到'
          }}
        </dd>
        <dt>新授权</dt>
        <dd>{{ record.new_grant_id ?? '尚未授予' }}</dd>
      </dl>
      <p v-if="newHostState" role="status">{{ newHostState }}</p>
      <p v-if="record.failure_code" role="alert">
        {{ record.failure_code }}：{{ record.failure_detail }}
      </p>
      <p v-if="record.status === 'timed_out' || record.status === 'cancelled'">
        该请求不能授予新机；旧机已停写时保持停写。
      </p>
      <NButton
        v-if="record.status === 'confirmed' && !record.new_grant_id"
        type="primary"
        :disabled="busy"
        @click="attempt(() => grantNormalHandover(record!.handover_id))"
        >授予新机执行权</NButton
      >
      <NButton
        v-if="record.status === 'pending'"
        :disabled="busy"
        @click="attempt(() => cancelNormalHandover(record!.handover_id))"
        >取消交权</NButton
      >
    </article>
    <p v-if="failure" role="alert" class="normal-handover__failure">{{ failure }}</p>
  </section>
</template>

<style scoped>
.normal-handover {
  margin-top: 1.5rem;
  padding-block: 1.25rem;
  border-top: 1px solid var(--color-border);
}
.normal-handover header p {
  color: var(--color-text-muted);
}
.normal-handover__form {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr)) auto;
  align-items: end;
  gap: 1rem;
}
.normal-handover :deep(.n-form-item) {
  margin-bottom: 0;
}
.normal-handover__lookup {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 0.75rem;
  margin-block: 1rem;
}
.normal-handover__lookup .n-form-item {
  flex: 1;
  min-width: 16rem;
}
.normal-handover dl {
  display: grid;
  grid-template-columns: auto 1fr;
  gap: 0.5rem 1rem;
}
.normal-handover dd {
  margin: 0;
  overflow-wrap: anywhere;
}
.normal-handover__failure {
  color: var(--color-danger);
}
@media (max-width: 900px) {
  .normal-handover__form {
    grid-template-columns: 1fr;
  }
}
</style>
