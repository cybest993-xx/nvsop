<script setup lang="ts">
import { ElButton, ElForm, ElFormItem, ElInput, ElMessage, ElOption, ElSelect } from 'element-plus'
import { computed, onMounted, reactive, ref, watch } from 'vue'

import {
  ControlPlaneError,
  estimateRetentionPolicyImpact,
  estimateStationRetentionImpact,
  readRetentionPolicy,
  readStationRetention,
  updateRetentionPolicy,
  updateStationRetention,
  type GlobalRetentionPolicy,
  type RetentionImpact,
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
const knownStationId = ref('')
const globalPolicy = ref<GlobalRetentionPolicy | null>(null)
const stationRetention = ref<StationRetention | null>(null)
const loading = ref(false)
const loadSequence = ref(0)
const saving = ref(false)
const failure = ref('')
const fieldErrors = ref<{ field: string; message: string }[]>([])
const editingGlobal = ref(false)
// 提交缩短前必须先展示影响估算：估算结果挂起等待操作员确认或取消。
const globalEstimate = ref<RetentionImpact | null>(null)
const stationEstimate = ref<RetentionImpact | null>(null)
const estimating = ref(false)

const globalDraft = reactive(blankDraft())
const stationDraft = reactive(blankDraft())
const stationMode = ref<RetentionPolicyMode>('follow_global')

const stationName = computed(
  () =>
    props.stations.find((station) => station.id === selectedStationId.value)?.name ??
    selectedStationId.value,
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

function selectKnownStation(): void {
  if (selectedStationId.value === knownStationId.value) {
    void load()
  } else {
    selectedStationId.value = knownStationId.value
  }
}

async function load(): Promise<void> {
  if (!session.may('retention.policy.view') || !selectedStationId.value) return
  const stationId = selectedStationId.value
  const sequence = ++loadSequence.value
  loading.value = true
  stationRetention.value = null
  // 重新读取即作废任何挂起的确认：旧估算不能再用于新基准。
  globalEstimate.value = null
  stationEstimate.value = null
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

/** 逐字段展示 owner 口径，不把多个压缩/聚合窗口相加成删除总量。 */
function fieldLabel(key: string): string {
  return FIELDS.find((field) => field.key === key)!.label
}

function formatMoment(value: string | null): string {
  return value === null
    ? '无对象'
    : new Date(value).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai' })
}

function formatBytes(value: number | null): string {
  if (value === null) return '不可度量'
  if (value < 1024) return `${value} 字节`
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KiB`
  return `${(value / 1024 / 1024).toFixed(1)} MiB`
}

async function requestGlobalEstimate(): Promise<void> {
  const current = globalPolicy.value
  if (current === null) return
  resetFailure()
  estimating.value = true
  try {
    globalEstimate.value = await estimateRetentionPolicyImpact(readDraft(globalDraft))
  } catch (error) {
    recordFailure(error)
  } finally {
    estimating.value = false
  }
}

function cancelGlobalEstimate(): void {
  // 取消不改变任何策略或数据：只丢弃挂起的确认。
  globalEstimate.value = null
}

async function confirmGlobalSave(): Promise<void> {
  const current = globalPolicy.value
  const estimate = globalEstimate.value
  if (current === null || estimate === null || !estimate.complete) return
  resetFailure()
  saving.value = true
  try {
    const updated = await updateRetentionPolicy(
      readDraft(globalDraft),
      current.revision,
      estimate.estimate_token,
    )
    globalPolicy.value = updated
    writeDraft(globalDraft, updated.policy)
    editingGlobal.value = false
    globalEstimate.value = null
    ElMessage.success('全局默认保留组已保存')
    await load()
  } catch (error) {
    recordFailure(error)
    globalEstimate.value = null
  } finally {
    saving.value = false
  }
}

async function requestStationEstimate(): Promise<void> {
  const current = stationRetention.value
  if (current === null) return
  resetFailure()
  estimating.value = true
  try {
    stationEstimate.value = await estimateStationRetentionImpact(current.station_id, {
      mode: stationMode.value,
      overrides: stationMode.value === 'custom' ? readDraft(stationDraft) : null,
    })
  } catch (error) {
    recordFailure(error)
  } finally {
    estimating.value = false
  }
}

function cancelStationEstimate(): void {
  // 取消不改变任何策略或数据：只丢弃挂起的确认。
  stationEstimate.value = null
}

async function confirmStationSave(): Promise<void> {
  const current = stationRetention.value
  const estimate = stationEstimate.value
  if (current === null || estimate === null || !estimate.complete) return
  resetFailure()
  saving.value = true
  try {
    const submitted = {
      mode: stationMode.value,
      overrides: stationMode.value === 'custom' ? readDraft(stationDraft) : null,
    }
    const updated = await updateStationRetention(
      current.station_id,
      submitted,
      current.revision,
      estimate.estimate_token,
    )
    if (current.station_id !== selectedStationId.value) return
    stationRetention.value = updated
    stationMode.value = updated.mode
    writeDraft(stationDraft, updated.overrides ?? updated.default)
    stationEstimate.value = null
    ElMessage.success('工位保留组已保存')
  } catch (error) {
    recordFailure(error)
    stationEstimate.value = null
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
        v-if="props.stations.length > 0"
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
      <ElForm v-else class="retention__station" @submit.prevent="selectKnownStation">
        <ElFormItem label="已知工位 ID">
          <ElInput v-model="knownStationId" aria-label="工位 ID" />
        </ElFormItem>
        <ElButton :loading="loading" @click="selectKnownStation">读取工位保留组</ElButton>
        <p class="retention__note">没有工位列表权限时，可按已知 ID 操作；不会读取设备列表。</p>
      </ElForm>
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
        @submit.prevent="requestStationEstimate"
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
        <div v-if="stationEstimate" class="retention__impact" aria-label="工位保留影响估算">
          <h4>即将缩短的影响</h4>
          <p class="retention__note">
            确认只改变后续回收依据，不同步删除历史数据；估算失败不会按零影响放行。
          </p>
          <p class="retention__note">
            参考时刻：{{ formatMoment(stationEstimate.reference_time) }}（北京时间）。
          </p>
          <p v-if="!stationEstimate.complete" class="retention__failure" role="alert">
            估算不完整，不能确认：{{ stationEstimate.incomplete_reasons.join('、') }}。
          </p>
          <p v-if="stationEstimate.fields.length === 0" class="retention__note">没有缩短字段。</p>
          <ul>
            <li v-for="field in stationEstimate.fields" :key="`station-impact-${field.field}`">
              <strong>{{ fieldLabel(field.field) }}</strong
              >（{{ field.owner }}）：
              <template v-if="field.exposed && field.protected">
                {{ field.removes_objects ? '新暴露' : '处理范围' }}
                {{ field.exposed.objects }} 个对象（{{ formatBytes(field.exposed.known_bytes) }}，
                未知大小 {{ field.exposed.unknown_size_objects }} 个，最早
                {{ formatMoment(field.exposed.earliest) }} ~ 最晚
                {{ formatMoment(field.exposed.latest) }}）； 引用保护跳过
                {{ field.protected.objects }} 个（{{
                  formatBytes(field.protected.known_bytes)
                }}，未知大小 {{ field.protected.unknown_size_objects }} 个， 最早
                {{ formatMoment(field.protected.earliest) }} ~ 最晚
                {{ formatMoment(field.protected.latest) }}）。
              </template>
              <span v-else>不可估算：{{ field.unavailable_reason }}（不是零影响）。</span>
              <p v-if="field.savings_unknown" class="retention__note">
                压缩/聚合节省未知，不代表删除对象或释放空间。
              </p>
              <p v-if="field.bytes_basis === 'serialized_rows'" class="retention__note">
                逻辑序列化字节，不代表可回收磁盘空间，也不代表本机媒体可删除。
              </p>
              <p
                v-for="source in field.source_configurations"
                :key="source.host_id"
                class="retention__note"
              >
                来源 {{ source.host_id }}，配置 revision
                {{ source.configuration_revision ?? '未知' }}，摘要
                {{ source.configuration_sha256 ?? '未知' }}。
              </p>
            </li>
          </ul>
          <ElButton
            type="primary"
            :loading="saving"
            :disabled="!stationEstimate.complete"
            @click="confirmStationSave"
          >
            确认保存工位保留组
          </ElButton>
          <ElButton @click="cancelStationEstimate">取消</ElButton>
        </div>
        <ElButton v-else type="primary" :loading="estimating" @click="requestStationEstimate">
          查看影响估算
        </ElButton>
      </ElForm>

      <ElForm
        v-if="mayEdit && editingGlobal"
        label-position="top"
        class="retention__form"
        aria-label="保存全局默认保留组"
        @submit.prevent="requestGlobalEstimate"
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
        <div v-if="globalEstimate" class="retention__impact" aria-label="全局保留影响估算">
          <h4>即将缩短的影响</h4>
          <p class="retention__note">
            确认只改变后续回收依据，不同步删除历史数据；估算失败不会按零影响放行。
          </p>
          <p class="retention__note">
            参考时刻：{{ formatMoment(globalEstimate.reference_time) }}（北京时间）。
          </p>
          <p v-if="!globalEstimate.complete" class="retention__failure" role="alert">
            估算不完整，不能确认：{{ globalEstimate.incomplete_reasons.join('、') }}。
          </p>
          <p v-if="globalEstimate.fields.length === 0" class="retention__note">没有缩短字段。</p>
          <ul>
            <li v-for="field in globalEstimate.fields" :key="`global-impact-${field.field}`">
              <strong>{{ fieldLabel(field.field) }}</strong
              >（{{ field.owner }}）：
              <template v-if="field.exposed && field.protected">
                {{ field.removes_objects ? '新暴露' : '处理范围' }}
                {{ field.exposed.objects }} 个对象（{{ formatBytes(field.exposed.known_bytes) }}，
                未知大小 {{ field.exposed.unknown_size_objects }} 个，最早
                {{ formatMoment(field.exposed.earliest) }} ~ 最晚
                {{ formatMoment(field.exposed.latest) }}）； 引用保护跳过
                {{ field.protected.objects }} 个（{{
                  formatBytes(field.protected.known_bytes)
                }}，未知大小 {{ field.protected.unknown_size_objects }} 个， 最早
                {{ formatMoment(field.protected.earliest) }} ~ 最晚
                {{ formatMoment(field.protected.latest) }}）。
              </template>
              <span v-else>不可估算：{{ field.unavailable_reason }}（不是零影响）。</span>
              <p v-if="field.savings_unknown" class="retention__note">
                压缩/聚合节省未知，不代表删除对象或释放空间。
              </p>
              <p v-if="field.bytes_basis === 'serialized_rows'" class="retention__note">
                逻辑序列化字节，不代表可回收磁盘空间，也不代表本机媒体可删除。
              </p>
              <p
                v-for="source in field.source_configurations"
                :key="source.host_id"
                class="retention__note"
              >
                来源 {{ source.host_id }}，配置 revision
                {{ source.configuration_revision ?? '未知' }}，摘要
                {{ source.configuration_sha256 ?? '未知' }}。
              </p>
            </li>
          </ul>
          <ElButton
            type="primary"
            :loading="saving"
            :disabled="!globalEstimate.complete"
            @click="confirmGlobalSave"
          >
            确认保存全局默认保留组（revision {{ globalPolicy.revision }}）
          </ElButton>
          <ElButton @click="cancelGlobalEstimate">取消</ElButton>
        </div>
        <ElButton v-else type="primary" :loading="estimating" @click="requestGlobalEstimate">
          查看影响估算（revision {{ globalPolicy.revision }}）
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
.retention__impact {
  margin: 0.75rem 0;
  padding: 0.75rem 1rem;
  border: 1px solid var(--el-border-color);
  border-radius: 4px;
}

.retention__impact h4 {
  margin: 0 0 0.35rem;
}

.retention__impact ul {
  margin: 0.5rem 0;
  padding-left: 1.2rem;
}
</style>
