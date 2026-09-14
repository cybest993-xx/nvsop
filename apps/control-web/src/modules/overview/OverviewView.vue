<script setup lang="ts">
/**
 * 概览 in this slice: the session the caller actually holds, and nothing invented.
 *
 * §5.4 is explicit that the overview shows only real configuration summary — C7 composes that
 * from each module's `summary()` once those modules exist. Until then this page reports the one
 * fact the backend can already answer for, rather than standing in for it with a mock.
 */
import { computed, onMounted, onUnmounted, ref } from 'vue'

import { useSessionStore } from '@/session/store'

const session = useSessionStore()
const monitorEvents = ref<MonitorEvent[]>([])
let eventSource: EventSource | null = null

const canViewMonitor = computed(() =>
  session.current?.permissions?.includes('monitor.report.view') ?? false,
)

interface MonitorEvent {
  id: string
  verdict: string | null
  reasons: string[]
  templateVersion: string | null
  modelIds: string[]
}

function reasonHint(code: string): string {
  const known: Record<string, string> = {
    STREAM_LOST: '视频流中断，无法可靠判定',
    INFERENCE_TIMEOUT: '推理超时，无法可靠判定',
    MISSED_STEP: '确认步骤未出现',
    WRONG_STEP: '出现了错误步骤',
  }
  return known[code] ?? `未知原因码：${code}`
}

function strings(value: unknown): string[] {
  return Array.isArray(value)
    ? value.filter((item): item is string => typeof item === 'string')
    : []
}

function addMonitorEvent(event: MessageEvent<string>): void {
  try {
    const value: unknown = JSON.parse(event.data)
    if (typeof value !== 'object' || value === null) return
    const record = value as Record<string, unknown>
    monitorEvents.value.unshift({
      id: typeof record.event_id === 'string' ? record.event_id : event.lastEventId,
      verdict: typeof record.verdict === 'string' ? record.verdict : null,
      reasons: strings(record.reason_codes ?? (record.reason_code ? [record.reason_code] : [])),
      templateVersion:
        typeof record.template_version_id === 'string' ? record.template_version_id : null,
      modelIds: strings(record.model_ids),
    })
    monitorEvents.value = monitorEvents.value.slice(0, 20)
  } catch {
    // 非法事件不影响中心已持久化的镜像。
  }
}

onMounted(() => {
  if (!canViewMonitor.value) return
  eventSource = new EventSource('/api/v1/monitor/stream')
  eventSource.addEventListener('decision', (event) =>
    addMonitorEvent(event as MessageEvent<string>),
  )
})

onUnmounted(() => {
  eventSource?.close()
})

// §5.15: instants are UTC on the wire and rendered in Asia/Shanghai.
const formatter = new Intl.DateTimeFormat('zh-CN', {
  timeZone: 'Asia/Shanghai',
  dateStyle: 'medium',
  timeStyle: 'short',
})

const expiresAt = computed(() => {
  const raw = session.current?.expires_at
  return raw ? formatter.format(new Date(raw)) : ''
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

    <p class="overview__pending">配置摘要随各模块上线后显示。</p>

    <article v-if="canViewMonitor" class="overview__monitor" aria-labelledby="monitor-heading">
      <h2 id="monitor-heading">实时上报镜像</h2>
      <p v-if="!monitorEvents.length" class="overview__pending">等待推理机上报…</p>
      <ul v-else class="overview__events">
        <li v-for="event in monitorEvents" :key="event.id">
          <strong>{{ event.id }}</strong>
          <span v-if="event.verdict">结论 {{ event.verdict }}</span>
          <span v-for="reason in event.reasons" :key="reason">
            {{ reason }} — {{ reasonHint(reason) }}
          </span>
          <span v-if="event.templateVersion">模板 {{ event.templateVersion }}</span>
          <span v-if="event.modelIds.length">模型 {{ event.modelIds.join(', ') }}</span>
        </li>
      </ul>
    </article>
    <p v-else class="overview__pending">当前账号无权查看运行上报镜像。</p>
  </section>
</template>

<style scoped>
.overview__heading {
  margin: 0 0 1.25rem;
  font-size: 1.25rem;
}

.overview__facts {
  display: grid;
  grid-template-columns: 7rem 1fr;
  gap: 0.5rem 1rem;
  margin: 0 0 1.5rem;
  max-width: 32rem;
}

.overview__facts dt {
  color: var(--el-text-color-secondary);
}

.overview__facts dd {
  margin: 0;
}

.overview__pending {
  color: var(--el-text-color-secondary);
}

.overview__monitor {
  margin-top: 1.5rem;
  border: 1px solid var(--el-border-color-light);
  border-radius: 0.5rem;
  padding: 1rem;
}

.overview__monitor h2 {
  margin: 0 0 0.75rem;
  font-size: 1rem;
}

.overview__events {
  display: grid;
  gap: 0.5rem;
  margin: 0;
  padding-left: 1.25rem;
}

.overview__events li {
  display: grid;
  gap: 0.25rem;
}
</style>
