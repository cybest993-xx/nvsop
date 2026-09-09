<script setup lang="ts">
import {
  ElButton,
  ElDialog,
  ElForm,
  ElFormItem,
  ElInput,
  ElMessage,
  ElOption,
  ElRadio,
  ElRadioGroup,
  ElSelect,
  ElTag,
} from 'element-plus'
import { computed, reactive, ref, watch } from 'vue'

import {
  bindTemplateVersion,
  ControlPlaneError,
  readStationTemplateConfiguration,
  readTemplateVersions,
  updateStationRuntimeParameters,
  validateTemplateBinding,
  type BackendConfigurationStatusView,
  type FieldError,
  type RuntimeParameterMode,
  type RuntimeParametersInput,
  type RuntimeParametersUpdateInput,
  type StationTemplateConfigurationView,
  type StationView,
  type TemplateBindingInput,
  type TemplateBindingPreviewView,
  type TemplateVersionView,
} from '@/api/controlPlane'
import { useSessionStore } from '@/session/store'

interface RuntimeDraft {
  idle_timeout_seconds: string
  step_deadline_seconds: string
  disposition_policy: string
}

interface RuntimeSource {
  idle_timeout_seconds: number | null
  step_deadline_seconds: number | null
  disposition_policy: string | null
}

interface StationTarget {
  stationId: string
  stationRevision: number
  label: string
  configuration: StationTemplateConfigurationView | null
  preserveRuntimeParameters: boolean
}

const props = defineProps<{ stations: StationView[] }>()

const session = useSessionStore()
const mayViewStations = computed(() => session.may('device.station.view'))
const mayEditStations = computed(() => session.may('device.station.edit'))
const mayViewVersions = computed(() => session.may('template.draft.view'))

const configurations = ref(new Map<string, StationTemplateConfigurationView>())
const loading = ref(false)
const failure = ref('')
const fieldErrors = ref<FieldError[]>([])
const loadSequence = ref(0)

const versions = ref<TemplateVersionView[]>([])
const versionsLoading = ref(false)
const versionsLoaded = ref(false)

const dialogVisible = ref(false)
const target = ref<StationTarget | null>(null)
const versionId = ref('')
const runtimeMode = ref<RuntimeParameterMode>('follow_template')
const runtimeDraft = reactive<RuntimeDraft>(newRuntimeDraft())
const preview = ref<TemplateBindingPreviewView | null>(null)
const previewKey = ref('')
const busyAction = ref<'versions' | 'preview' | 'bind' | 'runtime' | ''>('')

const knownStationId = ref('')
const knownStationRevision = ref('')
const knownVersionId = ref('')

function newRuntimeDraft(source?: RuntimeSource | null): RuntimeDraft {
  return {
    idle_timeout_seconds: String(source?.idle_timeout_seconds ?? 30),
    step_deadline_seconds: String(source?.step_deadline_seconds ?? 90),
    disposition_policy: source?.disposition_policy ?? 'record',
  }
}

function resetRuntimeDraft(source?: StationTemplateConfigurationView | null): void {
  const values =
    source?.runtime_overrides ?? source?.effective_runtime_parameters ?? source?.template_defaults
  const next = newRuntimeDraft(values)
  runtimeDraft.idle_timeout_seconds = next.idle_timeout_seconds
  runtimeDraft.step_deadline_seconds = next.step_deadline_seconds
  runtimeDraft.disposition_policy = next.disposition_policy
}

function clearFailure(): void {
  failure.value = ''
  fieldErrors.value = []
}

function recordFailure(error: unknown): void {
  if (error instanceof ControlPlaneError) {
    failure.value = error.detail ?? error.message
    fieldErrors.value = error.fieldErrors
    return
  }
  failure.value = error instanceof Error ? error.message : '请求未能完成，请稍后重试'
  fieldErrors.value = []
}

function fieldError(field: string): string {
  return (
    fieldErrors.value.find((item) => item.field === field || item.field.endsWith(`.${field}`))
      ?.message ?? ''
  )
}

async function loadConfigurations(): Promise<void> {
  const stationIds = props.stations.map((station) => station.id)
  configurations.value = new Map()
  if (!mayViewStations.value || stationIds.length === 0) {
    loading.value = false
    return
  }

  const sequence = loadSequence.value + 1
  loadSequence.value = sequence
  loading.value = true
  clearFailure()
  try {
    const loaded = await Promise.all(
      stationIds.map(
        async (stationId) =>
          [stationId, await readStationTemplateConfiguration(stationId)] as const,
      ),
    )
    if (sequence !== loadSequence.value) {
      return
    }
    configurations.value = new Map(loaded)
  } catch (error) {
    if (sequence === loadSequence.value) {
      recordFailure(error)
    }
  } finally {
    if (sequence === loadSequence.value) {
      loading.value = false
    }
  }
}

async function ensureVersions(): Promise<void> {
  if (!mayViewVersions.value || versionsLoaded.value || busyAction.value === 'versions') {
    return
  }
  versionsLoading.value = true
  busyAction.value = 'versions'
  try {
    versions.value = (await readTemplateVersions()).items
    versionsLoaded.value = true
  } catch (error) {
    recordFailure(error)
  } finally {
    versionsLoading.value = false
    busyAction.value = ''
  }
}

function openStation(station: StationView, editable: boolean): void {
  const configuration = configurations.value.get(station.id) ?? null
  target.value = {
    stationId: station.id,
    stationRevision: configuration?.station_revision ?? station.revision,
    label: `${station.name}（${station.code}）`,
    configuration,
    preserveRuntimeParameters: false,
  }
  versionId.value = configuration?.desired?.version_id ?? configuration?.version?.id ?? ''
  knownVersionId.value = versionId.value
  runtimeMode.value = configuration?.runtime_parameter_mode ?? 'follow_template'
  resetRuntimeDraft(configuration)
  preview.value = null
  previewKey.value = ''
  clearFailure()
  dialogVisible.value = true
  if (editable) {
    void ensureVersions()
  }
}

function openKnownStation(): void {
  const stationId = knownStationId.value.trim()
  const revision = Number(knownStationRevision.value.trim())
  const knownVersion = knownVersionId.value.trim()
  if (!stationId) {
    failure.value = '请输入工位 ID'
    return
  }
  if (!Number.isInteger(revision) || revision < 1) {
    failure.value = '工位修订号必须是正整数'
    return
  }
  if (!knownVersion) {
    failure.value = '请输入模板版本 ID'
    return
  }
  target.value = {
    stationId,
    stationRevision: revision,
    label: stationId,
    configuration: null,
    preserveRuntimeParameters: true,
  }
  versionId.value = knownVersion
  runtimeMode.value = 'follow_template'
  resetRuntimeDraft(null)
  preview.value = null
  previewKey.value = ''
  clearFailure()
  dialogVisible.value = true
}

function markRuntimeParametersEdited(): void {
  if (target.value !== null) {
    target.value.preserveRuntimeParameters = false
  }
}

function runtimeParameters(): RuntimeParametersInput | null {
  if (runtimeMode.value === 'follow_template') {
    return null
  }
  const idle = Number(runtimeDraft.idle_timeout_seconds.trim())
  const deadline = Number(runtimeDraft.step_deadline_seconds.trim())
  const policy = runtimeDraft.disposition_policy.trim()
  if (!Number.isFinite(idle) || idle <= 0) {
    failure.value = '空闲时限必须是正数'
    return null
  }
  if (!Number.isFinite(deadline) || deadline <= 0) {
    failure.value = '步骤时限必须是正数'
    return null
  }
  if (!policy) {
    failure.value = '处置策略不能为空'
    return null
  }
  return {
    idle_timeout_seconds: idle,
    step_deadline_seconds: deadline,
    disposition_policy: policy,
  }
}

function requestFromDraft(): TemplateBindingInput | null {
  const currentTarget = target.value
  const selectedVersion = versionId.value.trim()
  if (currentTarget === null || !selectedVersion) {
    return null
  }
  if (currentTarget.preserveRuntimeParameters) {
    return {
      station_id: currentTarget.stationId,
      version_id: selectedVersion,
      runtime_parameter_mode: null,
      runtime_parameters: null,
    }
  }
  const parameters =
    runtimeMode.value === 'follow_template'
      ? null
      : {
          idle_timeout_seconds: Number(runtimeDraft.idle_timeout_seconds.trim()),
          step_deadline_seconds: Number(runtimeDraft.step_deadline_seconds.trim()),
          disposition_policy: runtimeDraft.disposition_policy.trim(),
        }
  return {
    station_id: currentTarget.stationId,
    version_id: selectedVersion,
    runtime_parameter_mode: runtimeMode.value,
    runtime_parameters: parameters,
  }
}

function bindingRequest(): TemplateBindingInput | null {
  const currentTarget = target.value
  if (currentTarget === null) {
    return null
  }
  const selectedVersion = versionId.value.trim()
  if (!selectedVersion) {
    failure.value = '请选择或输入模板版本 ID'
    return null
  }
  if (currentTarget.preserveRuntimeParameters) {
    return {
      station_id: currentTarget.stationId,
      version_id: selectedVersion,
      runtime_parameter_mode: null,
      runtime_parameters: null,
    }
  }
  const parameters = runtimeParameters()
  if (runtimeMode.value === 'custom' && parameters === null) {
    return null
  }
  return {
    station_id: currentTarget.stationId,
    version_id: selectedVersion,
    runtime_parameter_mode: runtimeMode.value,
    runtime_parameters: parameters,
  }
}

function requestKey(request: TemplateBindingInput): string {
  return JSON.stringify(request)
}

async function runPreview(request: TemplateBindingInput): Promise<boolean> {
  clearFailure()
  busyAction.value = 'preview'
  try {
    preview.value = await validateTemplateBinding(request)
    previewKey.value = requestKey(request)
    return preview.value.accepted
  } catch (error) {
    recordFailure(error)
    return false
  } finally {
    busyAction.value = ''
  }
}

async function previewBinding(): Promise<void> {
  const request = bindingRequest()
  if (request === null) {
    return
  }
  await runPreview(request)
}

async function bind(): Promise<void> {
  const currentTarget = target.value
  const request = bindingRequest()
  if (currentTarget === null || request === null) {
    return
  }
  const key = requestKey(request)
  if (previewKey.value !== key || preview.value?.accepted !== true) {
    if (!(await runPreview(request))) {
      return
    }
  }
  clearFailure()
  busyAction.value = 'bind'
  try {
    const updated = await bindTemplateVersion(request, currentTarget.stationRevision)
    configurations.value.set(updated.station_id, updated)
    target.value = {
      ...currentTarget,
      stationRevision: updated.station_revision,
      configuration: updated,
    }
    ElMessage.success('工位模板期望配置已保存')
    dialogVisible.value = false
  } catch (error) {
    recordFailure(error)
    if (error instanceof ControlPlaneError && error.status === 409) {
      await reloadTarget(currentTarget.stationId)
    }
  } finally {
    busyAction.value = ''
  }
}

async function saveRuntimeParameters(): Promise<void> {
  const currentTarget = target.value
  if (currentTarget === null) {
    return
  }
  const parameters = runtimeParameters()
  if (runtimeMode.value === 'custom' && parameters === null) {
    return
  }
  const request: RuntimeParametersUpdateInput = {
    mode: runtimeMode.value,
    parameters,
  }
  clearFailure()
  busyAction.value = 'runtime'
  try {
    const updated = await updateStationRuntimeParameters(
      currentTarget.stationId,
      request,
      currentTarget.stationRevision,
    )
    configurations.value.set(updated.station_id, updated)
    target.value = {
      ...currentTarget,
      stationRevision: updated.station_revision,
      configuration: updated,
    }
    resetRuntimeDraft(updated)
    preview.value = null
    previewKey.value = ''
    ElMessage.success('工位运行参数已保存')
  } catch (error) {
    recordFailure(error)
    if (error instanceof ControlPlaneError && error.status === 409) {
      await reloadTarget(currentTarget.stationId)
    }
  } finally {
    busyAction.value = ''
  }
}

async function reloadTarget(stationId: string): Promise<void> {
  try {
    const updated = await readStationTemplateConfiguration(stationId)
    configurations.value.set(stationId, updated)
    if (target.value?.stationId === stationId) {
      target.value = {
        ...target.value,
        stationRevision: updated.station_revision,
        configuration: updated,
      }
    }
    failure.value = '工位配置已变化，已刷新当前事实；请重新预检后重试。'
    fieldErrors.value = []
  } catch (error) {
    recordFailure(error)
  }
}

function statusLabel(status: string): string {
  switch (status) {
    case 'unbound':
      return '未绑定'
    case 'not_confirmed':
      return '尚未确认'
    case 'waiting':
      return '等待推理机应用'
    case 'confirmed':
      return '已确认'
    case 'digest_mismatch':
      return '摘要不一致'
    case 'rejected':
      return '上报被拒绝'
    case 'topology_invalid':
      return '拓扑无效'
    default:
      return `未知状态（${status}）`
  }
}

function statusTag(status: string): 'success' | 'danger' | 'warning' | 'info' {
  switch (status) {
    case 'confirmed':
      return 'success'
    case 'digest_mismatch':
    case 'rejected':
    case 'topology_invalid':
      return 'danger'
    case 'waiting':
    case 'not_confirmed':
      return 'warning'
    case 'unbound':
      return 'info'
    default:
      return 'warning'
  }
}

function backendStatusLabel(backend: BackendConfigurationStatusView): string {
  const version = backend.reported_version_id ? backend.reported_version_id.slice(0, 8) : '无版本'
  const revision =
    backend.reported_config_revision === null
      ? '无修订'
      : `修订 ${backend.reported_config_revision}`
  return `${statusLabel(backend.status)} · ${version} · ${revision}`
}

function formatDate(value: string | null): string {
  if (!value) {
    return '未上报'
  }
  return new Date(value).toLocaleString('zh-CN', {
    dateStyle: 'short',
    timeStyle: 'short',
    timeZone: 'Asia/Shanghai',
  })
}

function versionDigest(configuration: StationTemplateConfigurationView): string {
  return configuration.desired?.sha256 ?? configuration.version?.sha256 ?? '无摘要'
}

function effectiveRuntime(configuration: StationTemplateConfigurationView): string {
  const values = configuration.effective_runtime_parameters
  if (values === null) {
    return '未绑定模板'
  }
  return `空闲 ${values.idle_timeout_seconds} 秒 · 步骤 ${values.step_deadline_seconds} 秒 · ${values.disposition_policy}`
}

watch(
  () => props.stations.map((station) => `${station.id}:${station.revision}`).join(','),
  () => void loadConfigurations(),
  { immediate: true },
)
</script>

<template>
  <section class="station-template" aria-labelledby="station-template-heading">
    <header class="station-template__header">
      <div>
        <p class="station-template__eyebrow">工位交付 / 对账</p>
        <h2 id="station-template-heading" class="station-template__heading">工位模板与运行参数</h2>
        <p class="station-template__intro">
          中心只记录期望版本；reported 是推理机签名上报的现场事实，二者不混为一次成功。
        </p>
      </div>
    </header>

    <p v-if="failure" class="station-template__failure" role="alert">{{ failure }}</p>

    <div
      v-if="!mayViewStations && mayEditStations"
      class="station-template__direct"
      aria-labelledby="known-station-heading"
    >
      <h3 id="known-station-heading">按已知工位操作</h3>
      <p>当前没有工位查看权限。不会读取工位列表或配置，请使用已知工位 ID、修订号和版本 ID。</p>
      <div class="station-template__direct-fields">
        <ElFormItem label="工位 ID">
          <ElInput v-model="knownStationId" name="known-station-id" autocomplete="off" />
        </ElFormItem>
        <ElFormItem label="工位修订号">
          <ElInput
            v-model="knownStationRevision"
            name="known-station-revision"
            type="number"
            inputmode="numeric"
            autocomplete="off"
          />
        </ElFormItem>
        <ElFormItem label="模板版本 ID">
          <ElInput v-model="knownVersionId" name="known-template-version-id" autocomplete="off" />
        </ElFormItem>
      </div>
      <ElButton type="primary" @click="openKnownStation">打开配置</ElButton>
    </div>

    <div v-else-if="mayViewStations" class="station-template__surface">
      <p v-if="loading" class="station-template__loading">正在读取工位模板配置…</p>
      <table v-else class="station-template__table">
        <caption class="station-template__caption">
          工位模板期望版本与推理机现场上报对账
        </caption>
        <thead>
          <tr>
            <th scope="col">工位</th>
            <th scope="col">绑定状态</th>
            <th scope="col">期望版本</th>
            <th scope="col">后端 reported</th>
            <th scope="col">有效运行参数</th>
            <th scope="col">操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="station in props.stations" :key="station.id">
            <th scope="row">
              <span>{{ station.name }}</span>
              <small>{{ station.code }}</small>
            </th>
            <td>
              <template v-if="configurations.get(station.id)">
                <ElTag
                  :type="statusTag(configurations.get(station.id)!.status)"
                  disable-transitions
                >
                  {{ statusLabel(configurations.get(station.id)!.status) }}
                </ElTag>
                <small v-if="configurations.get(station.id)!.status_detail">
                  {{ configurations.get(station.id)!.status_detail }}
                </small>
              </template>
              <span v-else class="station-template__muted">配置未读取</span>
            </td>
            <td>
              <template v-if="configurations.get(station.id)">
                <code v-if="configurations.get(station.id)!.desired">
                  {{ configurations.get(station.id)!.desired!.version_id }}
                </code>
                <span v-else>未绑定</span>
                <small
                  >配置修订：{{
                    configurations.get(station.id)!.desired?.config_revision ?? '—'
                  }}</small
                >
                <small class="station-template__digest">{{
                  versionDigest(configurations.get(station.id)!)
                }}</small>
              </template>
            </td>
            <td>
              <ul
                v-if="configurations.get(station.id)?.backends.length"
                class="station-template__backend-list"
              >
                <li
                  v-for="backend in configurations.get(station.id)!.backends"
                  :key="backend.backend_id"
                >
                  <span>{{ backendStatusLabel(backend) }}</span>
                  <small>{{ formatDate(backend.reported_at) }}</small>
                  <small v-if="backend.rejection_code">
                    拒绝原因：{{ backend.rejection_code }}（请核对版本、摘要和配置修订）
                  </small>
                  <small v-if="backend.rejection_detail">{{ backend.rejection_detail }}</small>
                </li>
              </ul>
              <span v-else class="station-template__muted">无有效后端</span>
            </td>
            <td>
              <span v-if="configurations.get(station.id)">
                {{ effectiveRuntime(configurations.get(station.id)!) }}
              </span>
              <small v-if="configurations.get(station.id)">
                来源：{{
                  configurations.get(station.id)!.runtime_parameter_mode === 'custom'
                    ? '工位自定义'
                    : '跟随模板'
                }}
                · 修订 {{ configurations.get(station.id)!.runtime_parameters_revision }}
              </small>
            </td>
            <td class="station-template__actions">
              <ElButton link type="primary" @click="openStation(station, false)">查看配置</ElButton>
              <ElButton
                v-if="mayEditStations"
                link
                type="primary"
                @click="openStation(station, true)"
              >
                绑定 / 编辑
              </ElButton>
            </td>
          </tr>
          <tr v-if="props.stations.length === 0">
            <td colspan="6" class="station-template__empty">还没有工位配置。</td>
          </tr>
        </tbody>
      </table>
    </div>

    <p v-else class="station-template__empty-state">当前没有工位模板查看权限。</p>

    <ElDialog v-model="dialogVisible" title="工位模板与运行参数" width="52rem">
      <div v-if="target" class="station-template__dialog">
        <p class="station-template__dialog-note">
          {{ target.label }} · If-Match 工位修订
          {{ target.stationRevision }}。正式绑定会再次读取并校验当前拓扑。
        </p>
        <ElForm label-position="top" @submit.prevent="previewBinding">
          <ElFormItem label="不可变模板版本">
            <ElSelect
              v-if="mayViewVersions"
              v-model="versionId"
              class="station-template__select"
              :loading="versionsLoading"
              filterable
              placeholder="选择已发布版本"
            >
              <ElOption
                v-for="version in versions"
                :key="version.id"
                :label="`${version.id} · ${version.sha256.slice(0, 12)}`"
                :value="version.id"
              />
            </ElSelect>
            <ElInput
              v-else
              v-model="versionId"
              name="known-template-version-id-dialog"
              autocomplete="off"
              placeholder="输入已知模板版本 ID"
            />
            <p v-if="fieldError('version_id')" class="station-template__field-error" role="alert">
              {{ fieldError('version_id') }}
            </p>
          </ElFormItem>

          <ElFormItem label="运行参数来源">
            <ElRadioGroup
              v-model="runtimeMode"
              name="runtime-parameter-mode"
              @change="markRuntimeParametersEdited"
            >
              <ElRadio value="follow_template">跟随模板默认值</ElRadio>
              <ElRadio value="custom">工位自定义</ElRadio>
            </ElRadioGroup>
          </ElFormItem>

          <div class="station-template__runtime-grid">
            <ElFormItem label="空闲时限（秒）">
              <ElInput
                v-model="runtimeDraft.idle_timeout_seconds"
                name="idle-timeout-seconds"
                type="number"
                inputmode="decimal"
                :disabled="runtimeMode !== 'custom'"
              />
            </ElFormItem>
            <ElFormItem label="步骤时限（秒）">
              <ElInput
                v-model="runtimeDraft.step_deadline_seconds"
                name="step-deadline-seconds"
                type="number"
                inputmode="decimal"
                :disabled="runtimeMode !== 'custom'"
              />
            </ElFormItem>
            <ElFormItem label="处置策略">
              <ElInput
                v-model="runtimeDraft.disposition_policy"
                name="disposition-policy"
                autocomplete="off"
                :disabled="runtimeMode !== 'custom'"
              />
            </ElFormItem>
          </div>
          <p v-if="fieldErrors.length" class="station-template__field-errors" role="alert">
            <span v-for="error in fieldErrors" :key="`${error.field}:${error.message}`">
              {{ error.field }}：{{ error.message }}
            </span>
          </p>
        </ElForm>

        <section
          v-if="preview"
          class="station-template__preview"
          aria-labelledby="binding-preview-heading"
        >
          <div class="station-template__preview-head">
            <h3 id="binding-preview-heading">绑定预检</h3>
            <ElTag :type="preview.accepted ? 'success' : 'danger'" disable-transitions>
              {{ preview.accepted ? '可以提交' : '不能提交' }}
            </ElTag>
          </div>
          <ul v-if="preview.reasons.length" class="station-template__issues">
            <li v-for="reason in preview.reasons" :key="`${reason.code}:${reason.field}`">
              {{ reason.field }}：{{ reason.message }}
            </li>
          </ul>
          <p v-else class="station-template__muted">当前版本、边界、拓扑和运行参数均通过预检。</p>
        </section>
      </div>
      <template #footer>
        <ElButton @click="dialogVisible = false">关闭</ElButton>
        <ElButton
          v-if="mayEditStations && !target?.preserveRuntimeParameters"
          :loading="busyAction === 'runtime'"
          :disabled="busyAction !== ''"
          @click="saveRuntimeParameters"
        >
          仅保存运行参数
        </ElButton>
        <ElButton
          v-if="mayEditStations"
          :loading="busyAction === 'preview'"
          :disabled="busyAction !== ''"
          @click="previewBinding"
        >
          预检绑定
        </ElButton>
        <ElButton
          v-if="mayEditStations"
          type="primary"
          :loading="busyAction === 'bind'"
          :disabled="
            busyAction !== '' ||
            preview?.accepted !== true ||
            previewKey !== requestKey(requestFromDraft() ?? { station_id: '', version_id: '' })
          "
          @click="bind"
        >
          正式绑定
        </ElButton>
      </template>
    </ElDialog>
  </section>
</template>

<style scoped>
.station-template {
  margin-top: 1.5rem;
}

.station-template__header {
  margin-bottom: 1rem;
}

.station-template__eyebrow {
  margin: 0 0 0.35rem;
  color: var(--el-color-primary);
  font-size: 0.8rem;
}

.station-template__heading {
  margin: 0 0 0.35rem;
  font-size: 1.2rem;
}

.station-template__intro,
.station-template__muted,
.station-template__loading,
.station-template__empty-state,
.station-template__direct p,
.station-template__dialog-note {
  margin: 0;
  color: var(--el-text-color-secondary);
  line-height: 1.5;
}

.station-template__failure {
  margin: 0 0 0.8rem;
  padding: 0.65rem 0.9rem;
  border-left: 3px solid var(--el-color-danger);
  background: var(--el-color-danger-light-9);
  color: var(--el-color-danger);
}

.station-template__surface,
.station-template__direct {
  overflow: auto;
  border: 1px solid var(--el-border-color-lighter);
  background: var(--el-bg-color);
}

.station-template__direct {
  padding: 1.1rem 1.25rem;
}

.station-template__direct h3 {
  margin: 0 0 0.35rem;
  font-size: 1rem;
}

.station-template__direct-fields {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 0 1rem;
  max-width: 58rem;
  margin: 1rem 0;
}

.station-template__table {
  width: 100%;
  min-width: 78rem;
  border-collapse: collapse;
}

.station-template__caption {
  position: absolute;
  width: 1px;
  height: 1px;
  overflow: hidden;
  clip: rect(0 0 0 0);
  white-space: nowrap;
}

.station-template__table th,
.station-template__table td {
  padding: 0.75rem 1rem;
  border-bottom: 1px solid var(--el-border-color-lighter);
  text-align: left;
  vertical-align: top;
}

.station-template__table thead th {
  background: var(--el-fill-color-lighter);
  color: var(--el-text-color-secondary);
  font-size: 0.82rem;
  font-weight: 500;
}

.station-template__table th[scope='row'] span,
.station-template__table th[scope='row'] small,
.station-template__table td small {
  display: block;
}

.station-template__table th[scope='row'] small,
.station-template__table td small {
  margin-top: 0.25rem;
  color: var(--el-text-color-secondary);
  font-weight: 400;
}

.station-template__digest {
  max-width: 16rem;
  overflow-wrap: anywhere;
}

.station-template__backend-list,
.station-template__issues {
  margin: 0;
  padding-left: 1.15rem;
}

.station-template__backend-list li,
.station-template__issues li {
  margin-bottom: 0.35rem;
}

.station-template__backend-list li:last-child,
.station-template__issues li:last-child {
  margin-bottom: 0;
}

.station-template__backend-list small {
  margin-top: 0.1rem;
}

.station-template__actions {
  display: flex;
  flex-wrap: wrap;
  gap: 0.25rem;
  min-width: 9rem;
}

.station-template__empty {
  padding: 2rem 1rem;
  text-align: center;
  color: var(--el-text-color-secondary);
}

.station-template__dialog {
  display: grid;
  gap: 1rem;
}

.station-template__dialog-note {
  padding: 0.7rem 0.85rem;
  border-left: 3px solid var(--el-color-warning);
  background: var(--el-color-warning-light-9);
}

.station-template__select {
  width: 100%;
}

.station-template__runtime-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 0 1rem;
}

.station-template__field-error,
.station-template__field-errors {
  margin: 0.25rem 0 0;
  color: var(--el-color-danger);
}

.station-template__field-errors {
  display: grid;
  gap: 0.25rem;
}

.station-template__preview {
  padding: 0.9rem 1rem;
  border: 1px solid var(--el-border-color-lighter);
  background: var(--el-fill-color-lighter);
}

.station-template__preview-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 1rem;
}

.station-template__preview h3 {
  margin: 0;
  font-size: 1rem;
}

.station-template :focus-visible {
  outline: 2px solid var(--el-color-primary);
  outline-offset: 2px;
}

@media (max-width: 780px) {
  .station-template__direct-fields,
  .station-template__runtime-grid {
    grid-template-columns: 1fr;
  }
}
</style>
