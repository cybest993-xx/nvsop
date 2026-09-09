<script setup lang="ts">
import {
  ElButton,
  ElDialog,
  ElForm,
  ElFormItem,
  ElInput,
  ElMessage,
  ElOption,
  ElSelect,
  ElTag,
} from 'element-plus'
import { computed, nextTick, onMounted, reactive, ref } from 'vue'

import {
  ControlPlaneError,
  createConnector,
  deleteConnector,
  editConnector,
  readConnector,
  readConnectors,
  readInferenceHosts,
  readStations,
  setConnectorStatus,
  type ConnectorPlacement,
  type ConnectorView,
  type InferenceHostView,
  type StationView,
  type FieldError,
} from '@/api/controlPlane'
import { useSessionStore } from '@/session/store'

import {
  connectorTypeLabel,
  reachabilityPresentation,
  statusPresentation,
} from './devicesPresentation'
import ConnectionTestControl from './ConnectionTestControl.vue'
import PointManagement from './PointManagement.vue'
import StationTemplateConfiguration from './StationTemplateConfiguration.vue'

interface ConnectorDraft {
  name: string
  connector_type: string
  address: string
  port: string
  station_id: string
  host_id: string
}

const session = useSessionStore()
const connectors = ref<ConnectorView[]>([])
const hosts = ref<InferenceHostView[]>([])
const stations = ref<StationView[]>([])
const loading = ref(true)
const failure = ref('')
const fieldErrors = ref<FieldError[]>([])

const mayViewConnectors = computed(() => session.may('device.connector.view'))
const mayViewHosts = computed(() => session.may('device.inference_host.view'))
const mayViewStations = computed(() => session.may('device.station.view'))
const mayEditConnectors = computed(() => session.may('device.connector.edit'))
const mayDeleteConnectors = computed(() => session.may('device.connector.delete'))
const hostNames = computed(() => new Map(hosts.value.map((host) => [host.id, host.name])))
const stationNames = computed(
  () => new Map(stations.value.map((station) => [station.id, station.name])),
)
const parentFields = computed(() => [
  {
    key: 'station_id' as const,
    label: '所属工位',
    canView: mayViewStations.value,
    options: stations.value.map((station) => ({
      value: station.id,
      label: `${station.name}（${station.code}）`,
    })),
  },
  {
    key: 'host_id' as const,
    label: '所属推理机',
    canView: mayViewHosts.value,
    options: hosts.value.map((host) => ({ value: host.id, label: host.name })),
  },
])
const basicFields = [
  { key: 'name' as const, label: '名称', type: 'text' as const },
  { key: 'connector_type' as const, label: '类型', type: 'select' as const },
  { key: 'address' as const, label: '地址', type: 'text' as const },
  { key: 'port' as const, label: '端口（可选）', type: 'number' as const },
]
const knownTargetFields = [
  { key: 'connectorId' as const, label: '连接器 ID' },
  { key: 'revision' as const, label: '已知修订号' },
]

function resetFailure(): void {
  failure.value = ''
  fieldErrors.value = []
}

function fieldError(name: string): string {
  const error = fieldErrors.value.find(({ field }) => field === name || field.endsWith(`.${name}`))
  return error?.message ?? ''
}

function recordFailure(error: unknown): void {
  if (!(error instanceof ControlPlaneError)) {
    throw error
  }
  failure.value = error.detail ?? error.message
  fieldErrors.value = error.fieldErrors
}

interface LoadOptions {
  showLoading: boolean
}

async function load({ showLoading }: LoadOptions = { showLoading: true }): Promise<void> {
  if (showLoading) {
    loading.value = true
  }
  failure.value = ''
  try {
    const [connectorPage, hostPage, stationPage] = await Promise.all([
      mayViewConnectors.value ? readConnectors() : Promise.resolve(null),
      mayViewHosts.value ? readInferenceHosts() : Promise.resolve(null),
      mayViewStations.value ? readStations() : Promise.resolve(null),
    ])
    connectors.value = connectorPage?.items ?? []
    hosts.value = hostPage?.items ?? []
    stations.value = stationPage?.items ?? []
  } catch (error) {
    recordFailure(error)
  } finally {
    if (showLoading) {
      loading.value = false
    }
  }
}

function refreshAfterConnectionTest(): void {
  void load({ showLoading: false })
}

async function attempt(operation: () => Promise<unknown>): Promise<boolean> {
  resetFailure()
  try {
    await operation()
    return true
  } catch (error) {
    recordFailure(error)
    return false
  }
}

type ConnectorDialogMode = 'create' | 'edit' | 'known-edit'

const connectorDialog = ref(false)
const connectorDialogMode = ref<ConnectorDialogMode>('create')
const editingConnector = ref<ConnectorView | null>(null)
const knownTargetDraft = reactive({ connectorId: '', revision: '' })
const knownConnectionTarget = reactive({ connectorId: '' })
const knownConnectionTest = ref<{ startTest: () => Promise<void> } | null>(null)
const draft = ref<ConnectorDraft>(newDraft())

function newDraft(connector?: ConnectorView): ConnectorDraft {
  return {
    name: connector?.name ?? '',
    connector_type: connector?.connector_type ?? 'hikvision_isapi',
    address: connector?.configuration.address ?? '',
    port: connector?.configuration.port?.toString() ?? '',
    station_id: connector?.station_id ?? stations.value[0]?.id ?? '',
    host_id: connector?.host_id ?? hosts.value[0]?.id ?? '',
  }
}

function openConnectorDialog(mode: ConnectorDialogMode, connector?: ConnectorView): void {
  connectorDialogMode.value = mode
  editingConnector.value = connector ?? null
  knownTargetDraft.connectorId = ''
  knownTargetDraft.revision = ''
  draft.value = newDraft(connector)
  resetFailure()
  connectorDialog.value = true
}

function readKnownTarget(): { id: string; revision: number } | null {
  const id = knownTargetDraft.connectorId.trim()
  const revision = Number(knownTargetDraft.revision.trim())
  if (!Number.isInteger(revision) || revision < 1) {
    failure.value = '修订号必须是正整数'
    return null
  }
  if (!id) {
    failure.value = '请输入连接器 ID'
    return null
  }
  return { id, revision }
}

function placementFromDraft(): ConnectorPlacement | null {
  const portText = draft.value.port.trim()
  if (portText !== '') {
    const port = Number(portText)
    if (!Number.isInteger(port) || port < 1 || port > 65535) {
      failure.value = '端口必须是 1 至 65535 的整数'
      return null
    }
  }
  const connectorType = draft.value.connector_type
  if (connectorType !== 'hikvision_isapi' && connectorType !== 'board_card') {
    failure.value = `未知连接器类型（${connectorType}），请选择已知类型后保存`
    return null
  }

  return {
    name: draft.value.name.trim(),
    connector_type: connectorType,
    configuration: {
      address: draft.value.address.trim(),
      ...(portText === '' ? {} : { port: Number(portText) }),
    },
    station_id: draft.value.station_id.trim(),
    host_id: draft.value.host_id.trim(),
  }
}

async function submitConnector(): Promise<void> {
  const placement = placementFromDraft()
  if (placement === null) {
    return
  }
  let ok = false
  switch (connectorDialogMode.value) {
    case 'create':
      ok = await attempt(() => createConnector(placement))
      break
    case 'edit': {
      const connector = editingConnector.value
      if (connector === null) {
        throw new Error('编辑模式缺少连接器')
      }
      ok = await attempt(() => editConnector(connector.id, placement, connector.revision))
      break
    }
    case 'known-edit': {
      const target = readKnownTarget()
      if (target === null) {
        return
      }
      ok = await attempt(() => editConnector(target.id, placement, target.revision))
      break
    }
    default:
      throw new Error(`未处理的连接器对话框模式: ${String(connectorDialogMode.value)}`)
  }
  if (!ok) {
    return
  }
  const wasCreate = connectorDialogMode.value === 'create'
  connectorDialog.value = false
  ElMessage.success(wasCreate ? '连接器已创建' : '连接器已保存')
  await load()
}

async function submitKnownConnectionTest(): Promise<void> {
  resetFailure()
  if (!knownConnectionTarget.connectorId.trim()) {
    failure.value = '请输入连接器 ID'
    return
  }
  await nextTick()
  await knownConnectionTest.value?.startTest()
}

async function submitKnownDelete(): Promise<void> {
  const target = readKnownTarget()
  if (target === null) {
    return
  }
  if (!(await attempt(() => deleteConnector(target.id, target.revision)))) {
    return
  }
  ElMessage.success('连接器已删除')
  await load()
}

async function toggleStatus(connector: ConnectorView): Promise<void> {
  const action = statusPresentation(connector.status).action
  if (action === null) {
    failure.value = '状态未知，不能切换'
    return
  }
  if (!(await attempt(() => setConnectorStatus(connector.id, action.next, connector.revision)))) {
    return
  }
  ElMessage.success(action.successMessage)
  await load()
}

async function removeConnector(connector: ConnectorView): Promise<void> {
  if (!(await attempt(() => deleteConnector(connector.id, connector.revision)))) {
    return
  }
  ElMessage.success('连接器已删除')
  await load()
}

const detailDialog = ref(false)
const detailLoading = ref(false)
const detailFailure = ref('')
const detailConnector = ref<ConnectorView | null>(null)
const detailRows = computed(() => {
  const connector = detailConnector.value
  if (connector === null) {
    return []
  }
  const address = connector.configuration.port
    ? `${connector.configuration.address}:${connector.configuration.port}`
    : connector.configuration.address
  return [
    ['名称', connector.name],
    ['类型', connectorTypeLabel(connector.connector_type)],
    ['所属工位', stationNames.value.get(connector.station_id) ?? connector.station_id],
    ['所属推理机', hostNames.value.get(connector.host_id) ?? connector.host_id],
    ['地址', address],
    ['凭据', connector.credentials_configured ? '已配置' : '未配置'],
    ['连接状态', reachabilityPresentation(connector.reachability).label],
    ['状态', statusPresentation(connector.status).label],
    ['修订号', String(connector.revision)],
  ].map(([label, value]) => ({ label, value }))
})

async function openDetail(connector: ConnectorView): Promise<void> {
  detailDialog.value = true
  detailLoading.value = true
  detailFailure.value = ''
  detailConnector.value = null
  try {
    detailConnector.value = await readConnector(connector.id)
  } catch (error) {
    if (!(error instanceof ControlPlaneError)) {
      throw error
    }
    detailFailure.value = error.detail ?? error.message
  } finally {
    detailLoading.value = false
  }
}

onMounted(load)
</script>

<template>
  <section aria-labelledby="devices-heading">
    <header class="devices__header">
      <div>
        <p class="devices__eyebrow">配置中心 / 设备</p>
        <h1 id="devices-heading" class="devices__heading">工位与设备</h1>
        <p class="devices__intro">管理连接器与点位的归属、语义和非秘密配置；实测能力单独登记。</p>
      </div>
      <ElButton v-if="mayEditConnectors" type="primary" @click="openConnectorDialog('create')">
        新建连接器
      </ElButton>
    </header>

    <p v-if="failure" class="devices__failure" role="alert">{{ failure }}</p>
    <p v-if="loading" class="devices__loading">正在加载连接器…</p>

    <div v-else-if="mayViewConnectors" class="devices__surface">
      <div class="devices__surface-head">
        <div>
          <h2 class="devices__title">连接器</h2>
          <p class="devices__meta">{{ connectors.length }} 个配置</p>
        </div>
        <p class="devices__surface-note">中心不保存设备凭据</p>
      </div>

      <table class="devices__table">
        <caption class="devices__caption">
          已配置的连接器，含已停用记录
        </caption>
        <thead>
          <tr>
            <th scope="col">名称</th>
            <th scope="col">类型</th>
            <th scope="col">所属工位</th>
            <th scope="col">推理机</th>
            <th scope="col">连接参数</th>
            <th scope="col">凭据</th>
            <th scope="col">连接状态</th>
            <th scope="col">状态</th>
            <th scope="col">操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="connector in connectors" :key="connector.id">
            <th scope="row">
              <ElButton
                link
                type="primary"
                class="devices__connector-link"
                @click="openDetail(connector)"
              >
                {{ connector.name }}
              </ElButton>
            </th>
            <td>{{ connectorTypeLabel(connector.connector_type) }}</td>
            <td>{{ stationNames.get(connector.station_id) ?? connector.station_id }}</td>
            <td>{{ hostNames.get(connector.host_id) ?? connector.host_id }}</td>
            <td>
              <span>{{ connector.configuration.address }}</span>
              <span v-if="connector.configuration.port"> :{{ connector.configuration.port }} </span>
            </td>
            <td>
              <ElTag
                :type="connector.credentials_configured ? 'success' : 'info'"
                disable-transitions
              >
                {{ connector.credentials_configured ? '已配置' : '未配置' }}
              </ElTag>
            </td>
            <td>
              <ElTag
                :type="reachabilityPresentation(connector.reachability).tag"
                disable-transitions
              >
                {{ reachabilityPresentation(connector.reachability).label }}
              </ElTag>
            </td>
            <td>
              <ElTag :type="statusPresentation(connector.status).tag" disable-transitions>
                {{ statusPresentation(connector.status).label }}
              </ElTag>
            </td>
            <td class="devices__row-actions">
              <ElButton link type="primary" @click="openDetail(connector)">详情</ElButton>
              <ElButton
                v-if="mayEditConnectors"
                link
                type="primary"
                @click="openConnectorDialog('edit', connector)"
              >
                编辑
              </ElButton>
              <ElButton
                v-if="mayEditConnectors && statusPresentation(connector.status).action"
                link
                type="primary"
                @click="toggleStatus(connector)"
              >
                {{ statusPresentation(connector.status).action?.label }}
              </ElButton>
              <span v-else-if="mayEditConnectors" class="devices__meta"> 状态未知，不能切换 </span>
              <ElButton
                v-if="mayDeleteConnectors"
                link
                type="danger"
                @click="removeConnector(connector)"
              >
                删除
              </ElButton>
              <ConnectionTestControl
                v-if="mayEditConnectors"
                :connector-id="connector.id"
                :connector-name="connector.name"
                :may-view="mayViewConnectors"
                @completed="refreshAfterConnectionTest"
              />
            </td>
          </tr>
          <tr v-if="connectors.length === 0">
            <td colspan="9" class="devices__empty">还没有连接器配置。</td>
          </tr>
        </tbody>
      </table>
    </div>

    <div
      v-else-if="mayEditConnectors || mayDeleteConnectors"
      class="devices__surface devices__direct"
    >
      <h2 class="devices__title">按连接器标识操作</h2>
      <p class="devices__form-note">
        当前没有连接器查看权限。不会读取连接器列表或详情，请使用已知的连接器 ID 和修订号。
      </p>
      <div class="devices__row-actions">
        <ElButton
          v-if="mayEditConnectors"
          type="primary"
          @click="openConnectorDialog('known-edit')"
        >
          按标识编辑
        </ElButton>
        <ElForm
          v-if="mayEditConnectors"
          inline
          aria-label="按标识测试连接"
          @submit.prevent="submitKnownConnectionTest"
        >
          <ElFormItem label="连接器 ID">
            <ElInput
              id="known-test-connector-id"
              v-model="knownConnectionTarget.connectorId"
              name="known-test-connector-id"
              autocomplete="off"
            />
          </ElFormItem>
          <ElButton type="primary" native-type="submit">测试连接</ElButton>
        </ElForm>
        <ConnectionTestControl
          v-if="mayEditConnectors && knownConnectionTarget.connectorId.trim()"
          :key="knownConnectionTarget.connectorId.trim()"
          ref="knownConnectionTest"
          :connector-id="knownConnectionTarget.connectorId.trim()"
          :connector-name="knownConnectionTarget.connectorId.trim()"
          :may-view="mayViewConnectors"
          :show-button="false"
          @completed="refreshAfterConnectionTest"
        />
        <ElForm v-if="mayDeleteConnectors" inline @submit.prevent="submitKnownDelete">
          <ElFormItem v-for="field in knownTargetFields" :key="field.key" :label="field.label">
            <ElInput
              :id="`known-delete-${field.key === 'connectorId' ? 'connector-id' : 'revision'}`"
              v-model="knownTargetDraft[field.key]"
              :name="field.key === 'connectorId' ? 'known-connector-id' : 'known-revision'"
              :type="field.key === 'revision' ? 'number' : 'text'"
              :inputmode="field.key === 'revision' ? 'numeric' : undefined"
              autocomplete="off"
            />
          </ElFormItem>
          <ElButton type="danger" native-type="submit">删除</ElButton>
        </ElForm>
      </div>
    </div>

    <ElDialog
      v-model="connectorDialog"
      :title="connectorDialogMode === 'create' ? '新建连接器' : '编辑连接器'"
      width="38rem"
    >
      <p class="devices__form-note">这里只保存地址和端口等非秘密参数；凭据由推理机本地配置。</p>
      <ElForm label-position="top" @submit.prevent="submitConnector">
        <div v-if="connectorDialogMode === 'known-edit'" class="devices__form-grid">
          <ElFormItem v-for="field in knownTargetFields" :key="field.key" :label="field.label">
            <ElInput
              :id="`known-${field.key === 'connectorId' ? 'connector-id' : 'revision'}`"
              v-model="knownTargetDraft[field.key]"
              :name="field.key === 'connectorId' ? 'known-connector-id' : 'known-revision'"
              :type="field.key === 'revision' ? 'number' : 'text'"
              :inputmode="field.key === 'revision' ? 'numeric' : undefined"
              autocomplete="off"
            />
          </ElFormItem>
        </div>
        <div class="devices__form-grid">
          <ElFormItem v-for="field in basicFields" :key="field.key" :label="field.label">
            <ElSelect
              v-if="field.type === 'select'"
              v-model="draft[field.key]"
              aria-label="连接器类型"
              class="devices__select"
            >
              <ElOption label="海康 ISAPI" value="hikvision_isapi" />
              <ElOption label="板卡连接器" value="board_card" />
            </ElSelect>
            <ElInput
              v-else
              :id="`connector-${field.key}`"
              v-model="draft[field.key]"
              :name="field.key"
              :type="field.type"
              :inputmode="field.key === 'port' ? 'numeric' : undefined"
              autocomplete="off"
            />
            <p v-if="fieldError(field.key)" class="devices__field-error" role="alert">
              {{ fieldError(field.key) }}
            </p>
          </ElFormItem>
          <ElFormItem v-for="parent in parentFields" :key="parent.key" :label="parent.label">
            <ElSelect
              v-if="parent.canView"
              v-model="draft[parent.key]"
              :aria-label="parent.label"
              class="devices__select"
            >
              <ElOption
                v-for="option in parent.options"
                :key="option.value"
                :label="option.label"
                :value="option.value"
              />
            </ElSelect>
            <ElInput
              v-else
              :id="parent.key"
              v-model="draft[parent.key]"
              :name="parent.key.replace('_id', '-id')"
              autocomplete="off"
              :readonly="connectorDialogMode === 'edit'"
            />
            <p v-if="fieldError(parent.key)" class="devices__field-error" role="alert">
              {{ fieldError(parent.key) }}
            </p>
          </ElFormItem>
        </div>
      </ElForm>
      <template #footer>
        <ElButton @click="connectorDialog = false">取消</ElButton>
        <ElButton type="primary" @click="submitConnector">
          {{ connectorDialogMode === 'create' ? '创建' : '保存' }}
        </ElButton>
      </template>
    </ElDialog>

    <ElDialog v-model="detailDialog" title="连接器详情" width="34rem">
      <p v-if="detailLoading" class="devices__loading">正在加载详情…</p>
      <p v-else-if="detailFailure" class="devices__failure" role="alert">{{ detailFailure }}</p>
      <dl v-else-if="detailConnector" class="devices__details">
        <template v-for="row in detailRows" :key="row.label">
          <dt>{{ row.label }}</dt>
          <dd>{{ row.value }}</dd>
        </template>
      </dl>
    </ElDialog>

    <StationTemplateConfiguration :stations="stations" />
    <PointManagement :connectors="connectors" :stations="stations" @changed="load" />
  </section>
</template>

<style scoped>
.devices__header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 1.5rem;
  margin-bottom: 1.5rem;
}

.devices__eyebrow {
  margin: 0 0 0.5rem;
  color: var(--el-color-primary);
  font-size: 0.8rem;
}

.devices__heading {
  margin: 0 0 0.5rem;
  font-size: 1.6rem;
  letter-spacing: -0.02em;
}

.devices__intro,
.devices__meta,
.devices__surface-note,
.devices__loading,
.devices__empty,
.devices__form-note {
  margin: 0;
  color: var(--el-text-color-secondary);
}

.devices__failure {
  margin: 0 0 1rem;
  padding: 0.65rem 0.9rem;
  border-left: 3px solid var(--el-color-danger);
  background: var(--el-color-danger-light-9);
  color: var(--el-color-danger);
}

.devices__surface {
  overflow: auto;
  border: 1px solid var(--el-border-color-lighter);
  background: var(--el-bg-color);
}

.devices__direct {
  padding: 1.25rem;
}

.devices__surface-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 1rem;
  padding: 1rem 1.25rem;
  border-bottom: 1px solid var(--el-border-color-lighter);
}

.devices__title {
  display: inline;
  margin: 0 0.75rem 0 0;
  font-size: 1.05rem;
}

.devices__meta,
.devices__surface-note {
  display: inline;
  font-size: 0.85rem;
}

.devices__table {
  width: 100%;
  min-width: 72rem;
  border-collapse: collapse;
}

.devices__caption {
  position: absolute;
  width: 1px;
  height: 1px;
  overflow: hidden;
  clip: rect(0 0 0 0);
  white-space: nowrap;
}

.devices__table th,
.devices__table td {
  padding: 0.8rem 1.25rem;
  border-bottom: 1px solid var(--el-border-color-lighter);
  text-align: left;
  vertical-align: middle;
  white-space: nowrap;
}

.devices__table thead th {
  background: var(--el-fill-color-lighter);
  color: var(--el-text-color-secondary);
  font-size: 0.82rem;
  font-weight: 500;
}

.devices__connector-link {
  padding-left: 0.7rem;
  border-left: 3px solid var(--el-color-primary);
  font-weight: 600;
}

.devices__row-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 0.35rem;
}

.devices__empty {
  padding: 2.5rem 1.25rem;
  text-align: center;
}

.devices__form-note {
  margin-bottom: 1.25rem;
  line-height: 1.5;
}

.devices__form-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 0 1rem;
}

.devices__select {
  width: 100%;
}

.devices__field-error {
  width: 100%;
  margin: 0.25rem 0 0;
  color: var(--el-color-danger);
}

.devices__details {
  display: grid;
  grid-template-columns: 7rem 1fr;
  gap: 0.75rem 1rem;
  margin: 0;
}

.devices__details dd {
  margin: 0;
  word-break: break-word;
}

.devices__header :focus-visible,
.devices__surface :focus-visible {
  outline: 2px solid var(--el-color-primary);
  outline-offset: 2px;
}

@media (max-width: 720px) {
  .devices__header,
  .devices__surface-head {
    flex-direction: column;
  }

  .devices__form-grid {
    grid-template-columns: 1fr;
  }
}
</style>
