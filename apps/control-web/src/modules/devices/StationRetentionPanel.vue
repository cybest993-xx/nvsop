<script setup lang="ts">
import { ElButton, ElForm, ElFormItem, ElInput, ElMessage, ElOption, ElSelect } from 'element-plus'
import { computed, onMounted, reactive, ref, watch } from 'vue'

import {
  ControlPlaneError,
  readRetentionPolicy,
  readStationRetention,
  updateRetentionPolicy,
  updateStationRetention,
  type GlobalRetentionPolicy,
  type RetentionMode,
  type RetentionPolicyMode,
  type RetentionPolicyView,
  type StationRetention,
  type StationView,
} from '@/api/controlPlane'
import { useSessionStore } from '@/session/store'

const props = defineProps<{ stations: StationView[] }>()
const session = useSessionStore()
const mayEdit = computed(() => session.may('retention.policy.edit'))

// 字段集合与中心 `POLICY_FIELDS` 一致；界面不重排、不逐字段混合默认值与覆盖组。
const FIELDS = [
  { key: 'evidence_pass', label: '证据：通过类' },
  { key: 'evidence_fail', label: '证据：不通过类' },
  { key: 'evidence_indeterminate', label: '证据：不可判定类' },
  { key: 'record_detail', label: '记录明细' },
  { key: 'record_compression_age', label: '记录压缩起始年龄' },
  { key: 'record_aggregate_age', label: '记录聚合归档年龄' },
  { key: 'disposal', label: '违规与处置记录' },
  { key: 'local_reported', label: '推理机本地已上报数据' },
] as const

type FieldKey = (typeof FIELDS)[number]['key']
type RuleDraft = { mode: RetentionMode; seconds: string }

const RULE_MODES: { value: RetentionMode; label: string }[] = [
  { value: 'duration', label: '时长' },
  { value: 'permanent', label: '永久' },
  { value: 'discard', label: '显式不保留' },
]

const selectedStationId = ref('')
const globalPolicy = ref<GlobalRetentionPolicy | null>(null)
const stationRetention = ref<StationRetention | null>(null)
const loading = ref(false)
const loadSequence = ref(0)
const saving = ref(false)
const failure = ref('')
const fieldErrors = ref<{ field: string; message: string }[]>([])
const editingGlobal = ref(false)

const globalDraft = reactive(blankDraft())
const stationDraft = reactive(blankDraft())
const stationMode = ref<RetentionPolicyMode>('follow_global')

const stationName = computed(
  () => props.stations.find((station) => station.id === selectedStationId.value)?.name ?? '',
)

function blankRule(): RuleDraft {
  return { mode: 'duration', seconds: '' }
}

function blankDraft(): Record<FieldKey, RuleDraft> {
  const draft = {} as Record<FieldKey, RuleDraft>
  for (const field of FIELDS) {
    draft[field.key] = blankRule()
  }
  return draft
}

function writeDraft(target: Record<FieldKey, RuleDraft>, policy: RetentionPolicyView): void {
  for (const field of FIELDS) {
    const rule = policy[field.key]
    target[field.key] = {
      mode: rule.mode,
      seconds: rule.seconds === null ? '' : String(rule.seconds),
    }
  }
}

function describe(rule: { mode: string; seconds?: number | null }): string {
  switch (rule.mode) {
    case 'duration':
      return rule.seconds === null || rule.seconds === undefined
        ? '时长（未提供秒数）'
        : `时长 ${formatSeconds(rule.seconds)}`
    case 'permanent':
      return '永久'
    case 'discard':
      return '显式不保留'
    case 'unset':
      return '未设置'
    default:
      return `未知模式（${rule.mode}）`
  }
}

function formatSeconds(seconds: number): string {
  const parts: string[] = []
  const days = Math.floor(seconds / 86400)
  const hours = Math.floor((seconds % 86400) / 3600)
  const minutes = Math.floor((seconds % 3600) / 60)
  if (days > 0) parts.push(`${days} 天`)
  if (hours > 0) parts.push(`${hours} 小时`)
  if (minutes > 0) parts.push(`${minutes} 分钟`)
  if (parts.length === 0) parts.push(`${seconds} 秒`)
  return `${parts.join(' ')}（${seconds} 秒）`
}

function readDraft(target: Record<FieldKey, RuleDraft>): RetentionPolicyView {
  const policy = {} as RetentionPolicyView
  for (const field of FIELDS) {
    const rule = target[field.key]
    if (rule.mode === 'duration') {
      const seconds = Number(rule.seconds)
      if (!Number.isInteger(seconds) || seconds <= 0) {
        throw new Error(`${field.label}：时长必须是正整数秒`)
      }
      policy[field.key] = { mode: rule.mode, seconds }
      continue
    }
    policy[field.key] = { mode: rule.mode, seconds: null }
  }
  return policy
}

function resetFailure(): void {
  failure.value = ''
  fieldErrors.value = []
}

function recordFailure(error: unknown): void {
  if (error instanceof ControlPlaneError) {
    failure.value = error.detail ?? error.message
    fieldErrors.value = error.fieldErrors
    return
  }
  failure.value = error instanceof Error ? error.message : '保留策略请求失败'
}

function fieldError(name: string): string {
  const error = fieldErrors.value.find(({ field }) => field === name || field.endsWith(`.${name}`))
  return error?.message ?? ''
}

async function load(): Promise<void> {
  if (!session.may('retention.policy.view') || !selectedStationId.value) return
  const stationId = selectedStationId.value
  const sequence = ++loadSequence.value
  loading.value = true
  stationRetention.value = null
  resetFailure()
  try {
    const [global, station] = await Promise.all([
      readRetentionPolicy(),
      readStationRetention(stationId),
    ])
    if (sequence !== loadSequence.value) return
    globalPolicy.value = global
    stationRetention.value = station
    writeDraft(globalDraft, global.policy)
    stationMode.value = station.mode
    writeDraft(stationDraft, station.overrides ?? station.default)
  } catch (error) {
    if (sequence === loadSequence.value) recordFailure(error)
  } finally {
    if (sequence === loadSequence.value) loading.value = false
  }
}

async function saveGlobal(): Promise<void> {
  const current = globalPolicy.value
  if (current === null) return
  resetFailure()
  saving.value = true
  try {
    const updated = await updateRetentionPolicy(readDraft(globalDraft), current.revision)
    globalPolicy.value = updated
    writeDraft(globalDraft, updated.policy)
    editingGlobal.value = false
    ElMessage.success('全局默认保留组已保存')
    await load()
  } catch (error) {
    recordFailure(error)
  } finally {
    saving.value = false
  }
}

async function saveStation(): Promise<void> {
  const current = stationRetention.value
  if (current === null) return
  resetFailure()
  saving.value = true
  try {
    const submitted = {
      mode: stationMode.value,
      overrides: stationMode.value === 'custom' ? readDraft(stationDraft) : null,
    }
    const updated = await updateStationRetention(current.station_id, submitted, current.revision)
    if (current.station_id !== selectedStationId.value) return

    stationRetention.value = updated
    stationMode.value = updated.mode
    writeDraft(stationDraft, updated.overrides ?? updated.default)
    ElMessage.success('工位保留组已保存')
  } catch (error) {
    recordFailure(error)
  } finally {
    saving.value = false
  }
}

watch(selectedStationId, () => {
  void load()
})

// 设备页先挂载面板、工位列表随后到达：列表就绪后自动选中第一个工位并加载。
watch(
  () => props.stations,
  (stations) => {
    if (selectedStationId.value === '' && stations.length > 0) {
      selectedStationId.value = stations[0]!.id
    }
  },
)

onMounted(() => {
  if (!session.may('retention.policy.view')) return
  selectedStationId.value = props.stations[0]?.id ?? ''
})
</script>

<template>
  <section
    v-if="session.may('retention.policy.view')"
    class="retention"
    aria-labelledby="retention-heading"
  >
    <header class="retention__header">
      <div>
        <h2 id="retention-heading">保留策略</h2>
        <p class="retention__note">
          全局默认整组生效；工位要么整组跟随全局，要么整组自定义。录像滚动窗口按推理机配置，不在此处。
        </p>
      </div>
      <ElSelect
        v-model="selectedStationId"
        aria-label="选择工位"
        placeholder="选择工位"
        class="retention__station"
      >
        <ElOption
          v-for="station in props.stations"
          :key="station.id"
          :label="`${station.name}（${station.code}）`"
          :value="station.id"
        />
      </ElSelect>
    </header>

    <p v-if="failure" class="retention__failure" role="alert">{{ failure }}</p>
    <p v-if="loading" class="retention__note">正在读取保留策略…</p>

    <template v-if="!loading && globalPolicy && stationRetention">
      <div class="retention__grid">
        <section aria-label="全局默认组">
          <h3>全局默认组</h3>
          <p class="retention__note">
            {{
              globalPolicy.revision === 0
                ? '尚未设置：当前显示代码默认值，保存后才成为权威值。'
                : `已设置，revision ${globalPolicy.revision}。`
            }}
          </p>
          <dl class="retention__rules">
            <template v-for="field in FIELDS" :key="`default-${field.key}`">
              <dt>{{ field.label }}</dt>
              <dd>{{ describe(globalPolicy.policy[field.key]) }}</dd>
            </template>
          </dl>
          <ElButton v-if="mayEdit" type="primary" link @click="editingGlobal = !editingGlobal">
            {{ editingGlobal ? '收起全局编辑' : '编辑全局默认' }}
          </ElButton>
        </section>

        <section :aria-label="`工位 ${stationName} 的覆盖与生效组`">
          <h3>工位覆盖与生效组</h3>
          <p class="retention__note">
            当前模式：{{
              stationRetention.mode === 'custom' ? '整组自定义' : '整组跟随全局'
            }}；覆盖记录 revision {{ stationRetention.revision }}（0
            表示尚未建立，生效值整组来自全局默认）。
          </p>
          <dl class="retention__rules">
            <template v-for="field in FIELDS" :key="`effective-${field.key}`">
              <dt>{{ field.label }}</dt>
              <dd>{{ describe(stationRetention.effective[field.key]) }}</dd>
            </template>
          </dl>
          <p v-if="stationRetention.overrides" class="retention__note">
            覆盖组与默认组不同；生效值整组来自覆盖组，不做逐字段合并。
          </p>
          <p v-else class="retention__note">该工位没有覆盖组，生效值整组等于全局默认组。</p>
        </section>
      </div>

      <ElForm
        v-if="mayEdit"
        label-position="top"
        class="retention__form"
        aria-label="保存保留组"
        @submit.prevent="saveStation"
      >
        <ElFormItem label="工位保留来源">
          <ElSelect v-model="stationMode" aria-label="工位保留来源" class="retention__select">
            <ElOption label="整组跟随全局默认" value="follow_global" />
            <ElOption label="整组自定义" value="custom" />
          </ElSelect>
        </ElFormItem>
        <p v-if="fieldError('retention_policy')" class="retention__failure" role="alert">
          {{ fieldError('retention_policy') }}
        </p>
        <div v-if="stationMode === 'custom'" class="retention__rules-form">
          <ElFormItem v-for="field in FIELDS" :key="`station-${field.key}`" :label="field.label">
            <div class="retention__rule">
              <ElSelect
                v-model="stationDraft[field.key].mode"
                :aria-label="`${field.label} 保留方式`"
              >
                <ElOption
                  v-for="mode in RULE_MODES"
                  :key="mode.value"
                  :label="mode.label"
                  :value="mode.value"
                />
              </ElSelect>
              <ElInput
                v-if="stationDraft[field.key].mode === 'duration'"
                v-model="stationDraft[field.key].seconds"
                :aria-label="`${field.label} 秒数`"
                inputmode="numeric"
                placeholder="秒"
              />
            </div>
            <p v-if="fieldError(field.key)" class="retention__failure" role="alert">
              {{ fieldError(field.key) }}
            </p>
          </ElFormItem>
        </div>
        <ElButton type="primary" :loading="saving" @click="saveStation"> 保存工位保留组 </ElButton>
      </ElForm>

      <ElForm
        v-if="mayEdit && editingGlobal"
        label-position="top"
        class="retention__form"
        aria-label="保存全局默认保留组"
        @submit.prevent="saveGlobal"
      >
        <div class="retention__rules-form">
          <ElFormItem
            v-for="field in FIELDS"
            :key="`global-${field.key}`"
            :label="`全局 ${field.label}`"
          >
            <div class="retention__rule">
              <ElSelect
                v-model="globalDraft[field.key].mode"
                :aria-label="`全局 ${field.label} 保留方式`"
              >
                <ElOption
                  v-for="mode in RULE_MODES"
                  :key="mode.value"
                  :label="mode.label"
                  :value="mode.value"
                />
              </ElSelect>
              <ElInput
                v-if="globalDraft[field.key].mode === 'duration'"
                v-model="globalDraft[field.key].seconds"
                :aria-label="`全局 ${field.label} 秒数`"
                inputmode="numeric"
                placeholder="秒"
              />
            </div>
          </ElFormItem>
        </div>
        <ElButton type="primary" :loading="saving" @click="saveGlobal">
          保存全局默认保留组（revision {{ globalPolicy.revision }}）
        </ElButton>
      </ElForm>
    </template>
  </section>
</template>

<style scoped>
.retention {
  margin-top: 1.5rem;
}

.retention__header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 1rem;
  flex-wrap: wrap;
}

.retention__station {
  min-width: 18rem;
}

.retention__note {
  margin: 0.35rem 0;
  color: var(--el-text-color-secondary);
}

.retention__failure {
  margin: 0.35rem 0;
  color: var(--el-color-danger);
}

.retention__grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 1rem 1.5rem;
  margin: 1rem 0;
}

.retention__rules {
  display: grid;
  grid-template-columns: minmax(9rem, auto) 1fr;
  gap: 0.35rem 0.8rem;
  margin: 0.5rem 0;
}

.retention__rules dd {
  margin: 0;
}

.retention__select {
  width: 100%;
}

.retention__rules-form {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 0 1rem;
}

.retention__rule {
  display: grid;
  grid-template-columns: 9rem minmax(0, 1fr);
  gap: 0.5rem;
  width: 100%;
}

@media (max-width: 720px) {
  .retention__grid,
  .retention__rules-form {
    grid-template-columns: 1fr;
  }
}
</style>
