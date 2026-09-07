<script setup lang="ts">
import { computed, onMounted, reactive, watch } from 'vue'

import * as api from '@/api/controlPlane'
import type { ConnectorView, PointConfiguration, PointView, StationView } from '@/api/controlPlane'
import FieldControl, { type FieldOption } from './FieldControl.vue'

type Status = PointView['status']
type FieldKey = keyof PointConfiguration
type TargetKey = 'id' | 'revision'
type Props = {
  canView: boolean
  canEdit: boolean
  canDelete: boolean
  canViewStations: boolean
  canViewConnectors: boolean
  stations: StationView[]
  connectors: ConnectorView[]
}
type Action = { label: string; run?: () => unknown; text?: boolean }
type Field = readonly [FieldKey, string]
type FilterKey = 'station_id' | 'connector_id'
const props = defineProps<Props>()
const emit = defineEmits<{ changed: [] }>()
const state = reactive({
  points: [] as PointView[],
  page: 1,
  size: 20,
  total: 0,
  loading: false,
  failure: '',
  notice: '',
  dialog: null as 'point' | 'detail' | null,
  mode: 'create',
  editing: null as PointView | null,
  known: { id: '', revision: '' },
  filters: { station_id: '', connector_id: '' },
  draft: {} as PointConfiguration,
  detail: null as PointView | null,
  detailFailure: '',
})
const headers = ['标识符', '语义标签', '方向', '工位', '连接器', '状态', '更新时间 / 归属', '操作']
const fields: Field[] = [
  ['identifier', '标识符'],
  ['semantic_label', '语义标签'],
  ['direction', '方向'],
  ['station_id', '所属工位'],
  ['connector_id', '所属连接器'],
]
const targets = { id: '点位 ID', revision: '已知修订号' } as const
const directions: FieldOption[] = Object.entries({ input: '输入', output: '输出' })
const filterFields: readonly [FilterKey, string, string, string][] = [
  ['station_id', '工位', '按工位 ID 筛选', 'point-station-filter'],
  ['connector_id', '连接器', '按连接器 ID 筛选', 'point-connector-filter'],
]
const formatter = new Intl.DateTimeFormat('zh-CN', {
  timeZone: 'Asia/Shanghai',
  year: 'numeric',
  month: '2-digit',
  day: '2-digit',
  hour: '2-digit',
  minute: '2-digit',
})
const stationNames = computed(
  () => new Map(props.stations.map((item) => [item.id, `${item.name}（${item.code}）`])),
)
const connectorNames = computed(() => new Map(props.connectors.map((item) => [item.id, item.name])))
const pageCount = computed(() => Math.max(1, Math.ceil(state.total / state.size)))
const dialogTitle = computed(() =>
  state.dialog === 'detail' ? '点位详情' : state.mode === 'create' ? '新建点位' : '编辑点位',
)
const raw = (value: unknown): string =>
  typeof value === 'string'
    ? value
    : value == null || value === ''
      ? '未提供'
      : (JSON.stringify(value) ?? Object.prototype.toString.call(value))
const directionText = (value: unknown): string =>
  value === 'input' ? '输入' : value === 'output' ? '输出' : `未知方向（${raw(value)}）`
const statusText = (value: unknown): string =>
  value === 'active' ? '在用' : value === 'deactivated' ? '已停用' : `未知状态（${raw(value)}）`
const safe = (point: PointView): boolean =>
  ['input', 'output'].includes(point.direction) && ['active', 'deactivated'].includes(point.status)
function statusAction(value: unknown): { next: Status; label: string } | null {
  if (value === 'active') return { next: 'deactivated', label: '停用' }
  if (value === 'deactivated') return { next: 'active', label: '恢复' }
  return null
}
const time = (value: string): string => {
  const date = new Date(value)
  return Number.isNaN(date.valueOf()) ? `原始时间（${value}）` : formatter.format(date)
}
const targetFields = (prefix: string) =>
  Object.entries(targets).map(([key, label]) => ({
    fieldKey: key as TargetKey,
    id: `${prefix}-${key}`,
    label,
    type: key === 'revision' ? 'number' : 'text',
    required: true,
  }))
const errorMessage = (error: unknown): string => {
  if (!(error instanceof api.ControlPlaneError)) throw error
  return error.detail ?? error.message
}
function choices(key: FieldKey): FieldOption[] | undefined {
  if (key === 'direction') return directions
  if (key === 'station_id' && props.canViewStations)
    return props.stations.map((item) => [item.id, `${item.name}（${item.code}）`])
  if (key === 'connector_id' && props.canViewConnectors)
    return props.connectors.map((item) => [item.id, item.name])
}
const pointFields = computed(() =>
  fields.map(([key, label]) => ({
    fieldKey: key,
    id: key.replace('_id', '-id'),
    label,
    options: choices(key),
    required: key !== 'direction',
  })),
)
async function run(
  operation: () => Promise<unknown>,
  message: string,
  completion: 'close-dialog' | 'keep-dialog',
): Promise<void> {
  state.failure = ''
  state.notice = ''
  try {
    await operation()
    if (completion === 'close-dialog') state.dialog = null
    state.notice = message
    emit('changed')
    await load()
  } catch (error) {
    state.failure = errorMessage(error)
  }
}
async function load(): Promise<void> {
  if (!props.canView) {
    Object.assign(state, { points: [], total: 0 })
    return
  }
  state.loading = true
  try {
    const result = await api.readPoints({
      page: state.page,
      page_size: state.size,
      ...(state.filters.station_id.trim() && { station_id: state.filters.station_id.trim() }),
      ...(state.filters.connector_id.trim() && { connector_id: state.filters.connector_id.trim() }),
    })
    Object.assign(state, { points: result.items, page: result.page, total: result.total })
  } catch (error) {
    state.failure = errorMessage(error)
  } finally {
    state.loading = false
  }
}
watch([() => state.filters.station_id, () => state.filters.connector_id], () => {
  state.page = 1
  void load()
})
async function turn(delta: number): Promise<void> {
  const page = state.page + delta
  if (page < 1 || page > pageCount.value) return
  state.page = page
  await load()
}
function target(): { id: string; revision: number } | null {
  const id = state.known.id.trim()
  const revision = Number(state.known.revision.trim())
  state.failure = !id
    ? '请输入点位 ID'
    : !Number.isInteger(revision) || revision < 1
      ? '修订号必须是正整数'
      : ''
  return state.failure ? null : { id, revision }
}
function openPoint(mode: 'create' | 'edit' | 'known-edit', point?: PointView): void {
  Object.assign(state, { mode, editing: point ?? null, dialog: 'point', failure: '', notice: '' })
  Object.assign(state.known, { id: '', revision: '' })
  Object.assign(state.draft, {
    identifier: point?.identifier ?? '',
    semantic_label: point?.semantic_label ?? '',
    direction: point?.direction ?? 'input',
    station_id: point?.station_id ?? props.stations[0]?.id ?? '',
    connector_id: point?.connector_id ?? props.connectors[0]?.id ?? '',
  })
}
function payload(): PointConfiguration | null {
  const point = state.draft
  if (point.direction !== 'input' && point.direction !== 'output') {
    state.failure = `未知方向（${raw(point.direction)}），请选择输入或输出后保存`
    return null
  }
  if (!point.station_id.trim() || !point.connector_id.trim()) {
    state.failure = '工位 ID 和连接器 ID 均为必填项'
    return null
  }
  return {
    ...point,
    identifier: point.identifier.trim(),
    semantic_label: point.semantic_label.trim(),
    station_id: point.station_id.trim(),
    connector_id: point.connector_id.trim(),
  }
}
async function submit(): Promise<void> {
  const body = payload()
  if (!body) return
  if (state.mode === 'edit' && !state.editing) throw new Error('编辑模式缺少点位')
  const item = state.mode === 'known-edit' ? target() : state.editing
  if (state.mode !== 'create' && !item) return
  const operation =
    state.mode === 'create'
      ? () => api.createPoint(body)
      : () => api.editPoint(item!.id, body, item!.revision)
  await run(operation, state.mode === 'create' ? '点位已创建' : '点位已保存', 'close-dialog')
}
async function removeKnown(): Promise<void> {
  const item = target()
  if (item) await run(() => api.deletePoint(item.id, item.revision), '点位已删除', 'keep-dialog')
}
async function toggle(point: PointView): Promise<void> {
  const action = statusAction(point.status)
  if (!action || !safe(point)) {
    state.failure = '方向或状态未知，不能切换点位状态'
    return
  }
  await run(
    () => api.setPointStatus(point.id, action.next, point.revision),
    action.next === 'active' ? '点位已恢复' : '点位已停用',
    'keep-dialog',
  )
}
async function openDetail(point: PointView): Promise<void> {
  Object.assign(state, { dialog: 'detail', detail: null, detailFailure: '' })
  try {
    state.detail = await api.readPoint(point.id)
  } catch (error) {
    state.detailFailure = errorMessage(error)
  }
}
const rows = (point: PointView): string[][] => [
  ['点位 ID', point.id],
  ['标识符', point.identifier],
  ['语义标签', point.semantic_label],
  ['方向', directionText(point.direction)],
  ['工位', stationNames.value.get(point.station_id) ?? point.station_id],
  ['连接器', connectorNames.value.get(point.connector_id) ?? point.connector_id],
  ['状态', statusText(point.status)],
  ['修订号', String(point.revision)],
]
function rowActions(point: PointView): Action[] {
  const actions: Action[] = [{ label: '详情', run: () => openDetail(point) }]
  if (!safe(point)) return [...actions, { label: '方向或状态未知，已停用操作', text: true }]
  if (props.canEdit) actions.push({ label: '编辑', run: () => openPoint('edit', point) })
  const status = props.canEdit && statusAction(point.status)
  if (status) actions.push({ label: status.label, run: () => toggle(point) })
  if (props.canDelete)
    actions.push({
      label: '删除',
      run: () => run(() => api.deletePoint(point.id, point.revision), '点位已删除', 'keep-dialog'),
    })
  return actions
}
onMounted(() => void load())
</script>

<template>
  <p v-if="state.failure" role="alert">{{ state.failure }}</p>
  <p v-if="state.notice" role="status">{{ state.notice }}</p>
  <button v-if="canEdit" type="button" @click="openPoint('create')">新建点位</button>
  <section v-if="canView" aria-labelledby="point-list-heading">
    <h3 id="point-list-heading">点位清单</h3>
    <FieldControl
      v-for="f in filterFields"
      :id="f[3]"
      :key="f[0]"
      v-model="state.filters[f[0]]"
      :label="f[1]"
      :aria-label="f[2]"
    />
    <p v-if="state.loading">正在加载点位…</p>
    <table v-else aria-label="点位清单，包含停用记录">
      <thead>
        <tr>
          <th v-for="header in headers" :key="header" scope="col">{{ header }}</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="point in state.points" :key="point.id">
          <th scope="row">
            <button type="button" @click="openDetail(point)">{{ point.identifier }}</button>
          </th>
          <td v-for="row in rows(point).slice(2, 7)" :key="row[0]">{{ row[1] }}</td>
          <td>{{ time(point.updated_at) }} / {{ point.updated_by }}</td>
          <td>
            <template v-for="action in rowActions(point)" :key="action.label">
              <span v-if="action.text">{{ action.label }}</span>
              <button v-else type="button" @click="action.run?.()">{{ action.label }}</button>
            </template>
          </td>
        </tr>
        <tr v-if="!state.points.length">
          <td colspan="8">没有符合条件的点位。</td>
        </tr>
      </tbody>
    </table>
    <nav v-if="state.total" aria-label="点位分页">
      <button type="button" aria-label="上一页" :disabled="state.page === 1" @click="turn(-1)">
        上一页
      </button>
      <span aria-live="polite">第 {{ state.page }} / {{ pageCount }} 页</span>
      <button
        type="button"
        aria-label="下一页"
        :disabled="state.page === pageCount"
        @click="turn(1)"
      >
        下一页
      </button>
    </nav>
  </section>
  <section v-else-if="canEdit || canDelete">
    <h3>按点位标识操作</h3>
    <p>当前没有点位查看权限。不会读取点位列表或详情，请使用已知的点位 ID 和修订号。</p>
    <button v-if="canEdit" type="button" @click="openPoint('known-edit')">按标识编辑</button>
    <form v-if="canDelete" @submit.prevent="removeKnown">
      <FieldControl
        v-for="f in targetFields('known-point')"
        :key="f.fieldKey"
        v-model="state.known[f.fieldKey]"
        v-bind="f"
      />
      <button type="submit">删除</button>
    </form>
  </section>
  <p v-else role="status">当前没有点位权限；点位列表、表单和状态操作均不会请求。</p>
  <dialog v-if="state.dialog" open class="modal" aria-labelledby="point-dialog-heading">
    <h3 id="point-dialog-heading">{{ dialogTitle }}</h3>
    <template v-if="state.dialog === 'detail'">
      <p v-if="!state.detail && !state.detailFailure">正在加载详情…</p>
      <p v-if="state.detailFailure" role="alert">{{ state.detailFailure }}</p>
      <dl v-if="state.detail">
        <template v-for="row in rows(state.detail)" :key="row[0]"
          ><dt>{{ row[0] }}</dt>
          <dd>{{ row[1] }}</dd></template
        >
      </dl>
      <button type="button" @click="state.dialog = null">关闭</button>
    </template>
    <form v-else @submit.prevent="submit">
      <div v-if="state.mode === 'known-edit'">
        <FieldControl
          v-for="f in targetFields('known-point-edit')"
          :key="f.fieldKey"
          v-model="state.known[f.fieldKey]"
          v-bind="f"
        />
      </div>
      <FieldControl
        v-for="f in pointFields"
        :key="f.fieldKey"
        v-model="state.draft[f.fieldKey]"
        v-bind="f"
      />
      <button type="button" @click="state.dialog = null">取消</button>
      <button type="submit">{{ state.mode === 'create' ? '创建' : '保存' }}</button>
    </form>
  </dialog>
</template>
