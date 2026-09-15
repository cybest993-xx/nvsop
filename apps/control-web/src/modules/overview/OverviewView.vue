<script setup lang="ts">
/**
 * 概览是已持久化配置和上报观测的权限裁剪投影；没有中心镜像事实时，不推断对象在线、健康或判定状态。

 */
import { computed, onMounted, onUnmounted, ref } from 'vue'

import { type OverviewDocument, type OverviewSection, readOverview } from '@/api/controlPlane'
import { useSessionStore } from '@/session/store'

const session = useSessionStore()
const overview = ref<OverviewDocument | null>(null)
const loading = ref(true)
const error = ref<string | null>(null)
const monitorEvents = ref<MonitorEvent[]>([])
let eventSource: EventSource | null = null

const formatter = new Intl.DateTimeFormat('zh-CN', {
  timeZone: 'Asia/Shanghai',
  dateStyle: 'medium',
  timeStyle: 'short',
})

const expiresAt = computed(() => {
  const raw = session.current?.expires_at
  return raw ? formatter.format(new Date(raw)) : ''
})

const sections = computed(() => {
  if (overview.value === null) return []
  return [
    ['device', '设备拓扑', overview.value.device],
    ['template', 'SOP 模板', overview.value.template],
    ['dataset', '训练数据集', overview.value.dataset],
    ['monitor', '运行观测', overview.value.monitor],
  ] as const
})

interface MonitorEvent {
  id: string
  kind: string
  verdict: string | null
  reasons: string[]
  templateVersion: string | null
  modelIds: string[]
}

interface SummaryEntry {
  label: string
  value: string
}

const sectionLinks: Record<string, string> = {
  device: '/devices',
  template: '/templates',
  dataset: '/training-datasets',
}

const labels: Record<string, string> = {
  inference_hosts: '推理机',
  inference_backends: '推理后端',
  stations: '工位',
  cameras: '相机',
  connectors: '连接器',
  points: '点位',
  drafts: '模板草稿',
  imports: '导入记录',
  published_versions: '已发布模板版本',
  datasets: '训练数据集',
  members: '视频成员',
  total: '总数',
  active: '启用',
  deactivated: '停用',
  unknown: '未知',
  by_status: '状态分布',
  connection_states: '连接验证状态',
  reachability: '可达性观测',
  verified: '已验证',
  unverified: '未验证',
  success: '连接成功',
  failure: '连接失败',
  reachable: '可达',
  unreachable: '不可达',
  credentials_configured: '凭据已配置',
  credentials_not_configured: '凭据未配置',
  sha256_verified: '摘要已记录',
  sha256_unverified: '摘要未记录',
  recent_decisions: '最近判定观测',
  recent_health: '最近健康观测',
  runtime_status: '观测说明',
  registered: '已登记',
  pending_upload: '等待上传',
  pending_validation: '等待校验',
  validating: '校验中',
  failed: '失败',
  succeeded: '成功',
}

const enumLabels: Record<string, string> = {
  reported_observations_only: '仅显示推理机上报的观测',
  active: '启用',
  deactivated: '停用',
  unverified: '未验证',
  success: '连接成功',
  failure: '连接失败',
  reachable: '可达',
  unreachable: '不可达',
  configured: '已配置',
  not_configured: '未配置',
  decision: '判定',
  health: '健康',
  pass: '通过',
  fail: '不通过',
  indeterminate: '不可判定',
}

function fieldLabel(value: string): string {
  return labels[value] ?? value
}

function enumLabel(value: string): string {
  return enumLabels[value] ?? value
}

function reasonText(code: string): string {
  const known: Record<string, string> = {
    STREAM_LOST: '视频流中断，无法可靠判定',
    INFERENCE_TIMEOUT: '推理超时，无法可靠判定',
    MISSED_STEP: '确认步骤未出现',
    WRONG_STEP: '出现了错误步骤',
    OUT_OF_ORDER: '步骤顺序不符',
    DEADLINE_EXCEEDED: '步骤等待超过时限',
  }
  return `${code} — ${known[code] ?? `未知原因码：${code}`}`
}

function summaryEntries(data: Record<string, unknown>): SummaryEntry[] {
  const entries: SummaryEntry[] = []

  function visit(value: unknown, path: string[]): void {
    if (value === null || value === undefined) return
    if (Array.isArray(value)) {
      entries.push({ label: path.map(fieldLabel).join(' / '), value: value.join('、') })
      return
    }
    if (typeof value === 'object') {
      for (const [key, nested] of Object.entries(value as Record<string, unknown>)) {
        visit(nested, [...path, key])
      }
      return
    }
    const rendered =
      typeof value === 'string'
        ? enumLabel(value)
        : typeof value === 'number' || typeof value === 'boolean' || typeof value === 'bigint'
          ? value.toString()
          : '未知值'
    entries.push({ label: path.map(fieldLabel).join(' / '), value: rendered })
  }

  for (const [key, value] of Object.entries(data)) visit(value, [key])
  return entries
}

function sectionLink(key: string, status: OverviewSection['status']): string | undefined {
  if (status === 'not_permitted' || status === 'unavailable' || status === 'failed')
    return undefined
  return sectionLinks[key]
}

function statusLabel(status: OverviewSection['status']): string {
  switch (status) {
    case 'available':
      return '有真实数据'
    case 'no_data':
      return '无数据'
    case 'partial':
      return '部分失败'
    case 'unavailable':
      return '暂不可用'
    case 'not_permitted':
      return '无权限'
    case 'failed':
      return '读取失败'
    default:
      return '状态未知'
  }
}

function statusTone(status: OverviewSection['status']): string {
  return ['available', 'no_data', 'partial', 'unavailable', 'not_permitted', 'failed'].includes(
    status,
  )
    ? status
    : 'unknown'
}

function sectionMessage(section: OverviewSection): string | null {
  if (section.status === 'not_permitted') return '当前账号无权查看此模块。'
  if (section.status === 'no_data') return '当前模块暂无已登记或已上报的数据。'
  if (section.status === 'partial') {
    return section.detail ?? '该模块摘要部分失败；页面只显示已读取的真实数据。'
  }
  if (section.status === 'unavailable') {
    return section.detail ?? '该模块摘要暂时不可用。'
  }
  if (section.status === 'failed') {
    return section.detail ?? '该模块摘要读取失败。'
  }
  return section.detail ?? null
}

function addMonitorEvent(kind: string, event: MessageEvent<string>): void {
  try {
    const value: unknown = JSON.parse(event.data)
    if (typeof value !== 'object' || value === null) return
    const record = value as Record<string, unknown>
    monitorEvents.value.unshift({
      id: typeof record.event_id === 'string' ? record.event_id : event.lastEventId,
      kind,
      verdict: typeof record.verdict === 'string' ? record.verdict : null,
      reasons: toStrings(record.reason_codes ?? (record.reason_code ? [record.reason_code] : [])),
      templateVersion:
        typeof record.template_version_id === 'string' ? record.template_version_id : null,
      modelIds: toStrings(record.model_ids),
    })
    monitorEvents.value = monitorEvents.value.slice(0, 20)
  } catch {
    // 无效事件数据不会改变中心已持久化的镜像。
  }
}

function toStrings(value: unknown): string[] {
  return Array.isArray(value)
    ? value.filter((item): item is string => typeof item === 'string')
    : []
}

function canOpenMonitorStream(status: OverviewSection['status']): boolean {
  return ['available', 'no_data'].includes(status)
}

onMounted(async () => {
  try {
    overview.value = await readOverview()
    if (canOpenMonitorStream(overview.value.monitor.status)) {
      eventSource = new EventSource('/api/v1/monitor/stream')
      eventSource.addEventListener('decision', (event) =>
        addMonitorEvent('decision', event as MessageEvent<string>),
      )
      eventSource.addEventListener('health', (event) =>
        addMonitorEvent('health', event as MessageEvent<string>),
      )
    }
  } catch (caught) {
    error.value = caught instanceof Error ? caught.message : '概览暂时不可用'
  } finally {
    loading.value = false
  }
})

onUnmounted(() => {
  eventSource?.close()
})
</script>

<template>
  <section aria-labelledby="overview-heading">
    <h1 id="overview-heading" class="overview__heading">概览</h1>

    <dl v-if="session.current" class="overview__facts">
      <dt>登录名</dt>
      <dd>{{ session.current.login_name }}</dd>
      <dt>姓名</dt>
      <dd>{{ session.current.display_name }}</dd>
      <dt>会话到期</dt>
      <dd>{{ expiresAt }}</dd>
    </dl>

    <p v-if="loading" class="overview__notice">正在读取权限范围内的真实配置摘要…</p>
    <p v-else-if="error" class="overview__notice overview__notice--error">{{ error }}</p>

    <div v-else class="overview__sections">
      <article v-for="[key, title, section] in sections" :key="key" class="overview__section">
        <div class="overview__section-header">
          <h2>{{ title }}</h2>
          <span :data-status="statusTone(section.status)">{{ statusLabel(section.status) }}</span>
        </div>
        <p v-if="sectionMessage(section)" class="overview__notice">
          {{ sectionMessage(section) }}
        </p>
        <RouterLink
          v-if="sectionLink(key, section.status)"
          :to="sectionLink(key, section.status)!"
          class="overview__link"
        >
          打开管理页
        </RouterLink>
        <dl v-if="summaryEntries(section.data).length" class="overview__summary">
          <template
            v-for="entry in summaryEntries(section.data)"
            :key="`${entry.label}:${entry.value}`"
          >
            <dt>{{ entry.label }}</dt>
            <dd>{{ entry.value }}</dd>
          </template>
        </dl>
      </article>

      <article
        v-if="monitorEvents.length"
        class="overview__section"
        aria-labelledby="monitor-events-heading"
      >
        <div class="overview__section-header">
          <h2 id="monitor-events-heading">实时上报镜像</h2>
          <span data-status="available">SSE</span>
        </div>
        <ul class="overview__events" aria-live="polite">
          <li v-for="event in monitorEvents" :key="`${event.kind}:${event.id}`">
            <strong>{{ enumLabel(event.kind) }} · {{ event.id }}</strong>
            <span v-if="event.verdict">结论 {{ enumLabel(event.verdict) }}</span>
            <span v-if="event.reasons.length">
              <span v-for="reason in event.reasons" :key="reason" class="overview__reason">
                {{ reasonText(reason) }}
              </span>
            </span>
            <span v-if="event.templateVersion">模板 {{ event.templateVersion }}</span>
            <span v-if="event.modelIds.length">模型 {{ event.modelIds.join('、') }}</span>
          </li>
        </ul>
      </article>
    </div>
  </section>
</template>

<style scoped>
.overview__heading {
  margin: 0 0 1.25rem;
  font-size: 1.25rem;
}

.overview__facts,
.overview__summary {
  display: grid;
  grid-template-columns: minmax(9rem, 15rem) 1fr;
  gap: 0.5rem 1rem;
  margin: 0;
}

.overview__facts {
  margin-bottom: 1.5rem;
  max-width: 40rem;
}

.overview__facts dt,
.overview__summary dt {
  color: var(--el-text-color-secondary);
}

.overview__facts dd,
.overview__summary dd {
  margin: 0;
  overflow-wrap: anywhere;
}

.overview__link {
  display: inline-block;
  margin-bottom: 0.75rem;
  color: var(--el-color-primary);
}

.overview__sections {
  display: grid;
  gap: 1rem;
}

.overview__section {
  border: 1px solid var(--el-border-color-light);
  border-radius: 0.5rem;
  padding: 1rem;
}

.overview__section-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 1rem;
  margin-bottom: 0.75rem;
}

.overview__section-header h2 {
  margin: 0;
  font-size: 1rem;
}

.overview__section-header span {
  color: var(--el-color-success);
  font-size: 0.875rem;
}

.overview__section-header span[data-status='no_data'],
.overview__section-header span[data-status='partial'],
.overview__section-header span[data-status='unavailable'],
.overview__section-header span[data-status='failed'] {
  color: var(--el-color-warning);
}

.overview__section-header span[data-status='not_permitted'],
.overview__section-header span[data-status='unknown'] {
  color: var(--el-text-color-secondary);
}

.overview__notice {
  color: var(--el-text-color-secondary);
}

.overview__notice--error {
  color: var(--el-color-danger);
}

.overview__events {
  display: grid;
  gap: 0.75rem;
  margin: 0;
  padding-left: 1.25rem;
}

.overview__events li {
  display: grid;
  gap: 0.25rem;
}

.overview__reason {
  display: block;
}
</style>
