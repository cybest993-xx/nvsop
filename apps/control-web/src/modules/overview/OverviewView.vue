<script setup lang="ts">
/**
 * The overview is a real, permission-trimmed projection.  It never invents online, healthy,
 * or judgment state: the monitor section explicitly says when it only has reported observations.
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

const sectionLinks: Record<string, string> = {
  device: '/devices',
  template: '/templates',
  dataset: '/training-datasets',
}

function sectionLink(key: string): string | undefined {
  return sectionLinks[key]
}

function statusLabel(status: OverviewSection['status']): string {
  switch (status) {
    case 'available':
      return '可用'
    case 'partial':
      return '部分可用'
    case 'unavailable':
      return '暂不可用'
    case 'not_permitted':
      return '无权限'
    default:
      return '状态未知'
  }
}

function statusTone(status: OverviewSection['status']): string {
  return ['available', 'partial', 'unavailable', 'not_permitted'].includes(status)
    ? status
    : 'unknown'
}

function displayValue(value: unknown): string {
  if (typeof value === 'string') return value
  return JSON.stringify(value)
}

function reasonHint(code: string): string {
  const known: Record<string, string> = {
    STREAM_LOST: '视频流中断，无法可靠判定',
    INFERENCE_TIMEOUT: '推理超时，无法可靠判定',
    MISSED_STEP: '确认步骤未出现',
    WRONG_STEP: '出现了错误步骤',
    OUT_OF_ORDER: '步骤顺序不符',
    DEADLINE_EXCEEDED: '步骤等待超过时限',
  }
  return known[code] ?? `未知原因码：${code}`
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
    // An invalid projection is ignored by the dashboard; the raw center mirror remains durable.
  }
}

function toStrings(value: unknown): string[] {
  return Array.isArray(value)
    ? value.filter((item): item is string => typeof item === 'string')
    : []
}

onMounted(async () => {
  try {
    overview.value = await readOverview()
    if (overview.value.monitor.status === 'available') {
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
        <p v-if="section.detail" class="overview__notice">{{ section.detail }}</p>
        <RouterLink v-if="sectionLink(key)" :to="sectionLink(key)!" class="overview__link">
          打开管理页
        </RouterLink>
        <dl v-if="Object.keys(section.data).length" class="overview__summary">
          <template v-for="(value, name) in section.data" :key="name">
            <dt>{{ name }}</dt>
            <dd>{{ displayValue(value) }}</dd>
          </template>
        </dl>
        <p v-else-if="section.status === 'not_permitted'" class="overview__notice">
          当前账号无权查看此模块。
        </p>
        <p v-else class="overview__notice">当前模块没有可显示的数据。</p>
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
        <ul class="overview__events">
          <li v-for="event in monitorEvents" :key="`${event.kind}:${event.id}`">
            <strong>{{ event.kind }} · {{ event.id }}</strong>
            <span v-if="event.verdict">结论 {{ event.verdict }}</span>
            <span v-if="event.reasons.length">
              <span v-for="reason in event.reasons" :key="reason" class="overview__reason">
                {{ reason }} — {{ reasonHint(reason) }}
              </span>
            </span>
            <span v-if="event.templateVersion">模板 {{ event.templateVersion }}</span>
            <span v-if="event.modelIds.length">模型 {{ event.modelIds.join(', ') }}</span>
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
  grid-template-columns: minmax(7rem, 12rem) 1fr;
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

.overview__section-header span[data-status='partial'],
.overview__section-header span[data-status='unavailable'] {
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
