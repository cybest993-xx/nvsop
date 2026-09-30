<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import {
  openRuntimeProjection,
  type RuntimeStationProjection,
  type StationView,
} from '@/api/controlPlane'
import { useSessionStore } from '@/session/store'

const props = defineProps<{ stations: StationView[] }>()
const session = useSessionStore()
const projections = ref(new Map<string, RuntimeStationProjection>())
const connected = ref(false)
const hasConnected = ref(false)
let closeStream: (() => void) | undefined
const connectionLabel = computed(() =>
  connected.value
    ? '实时连接正常'
    : hasConnected.value
      ? '连接中断，保留上次镜像等待恢复'
      : '正在连接运行镜像…',
)
const stationLabel = (id: string) =>
  session.may('device.station.view')
    ? (props.stations.find((station) => station.id === id)?.name ?? id)
    : id
const verdicts: Record<string, string> = { pass: '通过', fail: '不通过', indeterminate: '不可判定' }
const reasons: Record<string, string> = {
  STREAM_LOST: '观测流中断',
  INFERENCE_BACKEND_UNREACHABLE: '推理后端不可达',
  INFERENCE_TIMEOUT: '推理超时',
  TIMESTAMP_DISCONTINUITY: '时间轴不连续',
  CHUNK_BACKLOG_EXCEEDED: '观测积压超限',
  ACTION_ID_UNKNOWN: '动作编号不在模板中',
  INFERENCE_HOST_DOWN: '推理机失联',
  RUN_INTERRUPTED: '运行被中断',
  IO_SIGNAL_LOST: '外部信号不可达',
  IO_TIME_UNALIGNED: '外部信号时间未对齐',
  MISSED_STEP: '缺少必做步骤',
  WRONG_STEP: '出现错误步骤',
  OUT_OF_ORDER: '步骤顺序错误',
  DEADLINE_EXCEEDED: '步骤超时',
}
const reasonLabel = (code: string) =>
  reasons[code] ? `${code} — ${reasons[code]}` : `未知原因码：${code} — 该原因码尚无对应说明。`
const time = (value: string | number | null) =>
  value === null
    ? '无'
    : typeof value === 'number'
      ? `${value}（推理机时间轴）`
      : new Date(value).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai' })
const models = (sources: unknown) =>
  ((sources ?? []) as { backend_id: string; model_ids: string[] }[])
    .map((source) => `${source.backend_id}：${source.model_ids.join('、') || '无模型标识'}`)
    .join('；') || '未提供'
function update(value: RuntimeStationProjection): void {
  const current = projections.value.get(value.station_id)
  if (current && JSON.stringify(current) === JSON.stringify(value)) return
  projections.value = new Map(projections.value).set(value.station_id, value)
}
onMounted(() => {
  if (session.may('monitor.report.view'))
    closeStream = openRuntimeProjection(update, (value) => {
      connected.value = value
      hasConnected.value ||= value
    })
})
onUnmounted(() => closeStream?.())
</script>

<template>
  <section
    v-if="session.may('monitor.report.view')"
    class="runtime"
    aria-labelledby="runtime-heading"
  >
    <header>
      <div>
        <h2 id="runtime-heading">工位运行镜像</h2>
        <p>只读中心已持久化的推理机事实；不代表配置期望或新的判定。</p>
      </div>
      <p role="status" aria-live="polite">{{ connectionLabel }}</p>
    </header>
    <p v-if="!projections.size" class="muted">尚无运行镜像；连接恢复后会继续读取最新事实。</p>
    <article v-for="p in projections.values()" :key="p.station_id">
      <h3>
        工位 {{ stationLabel(p.station_id) }} <small>{{ p.station_id }}</small>
      </h3>
      <div class="runtime__grid">
        <section aria-label="SOP 实例与实际来源">
          <h4>SOP 实例与实际来源</h4>
          <template v-if="p.instance">
            <p>状态：{{ p.instance.closed_at === null ? '运行中' : '已结束' }}</p>
            <p v-if="p.instance.close_reason">关闭原因：{{ p.instance.close_reason }}</p>
            <p>推理机 / 实例：{{ p.instance.host_id }} / {{ p.instance.instance_id }}</p>
            <p>实际模板：{{ p.instance.template_version_id }}</p>
            <p>实际模型：{{ models(p.instance.backend_provenance) }}</p>
            <details>
              <summary>实例完整上报</summary>
              <pre>{{ JSON.stringify(p.instance, null, 2) }}</pre>
            </details>
          </template>
          <p v-if="!p.instance" class="muted">尚无实例镜像</p>
        </section>
        <section aria-label="最新观测">
          <h4>最新观测</h4>
          <template v-if="p.observation">
            <p>来源 / 信号：{{ p.observation.source }} / {{ p.observation.signal }}</p>
            <p>实例 / 时间：{{ p.observation.instance_id }} / {{ p.observation.observed_at }}</p>
            <p>实际模板：{{ p.observation.template_version_id }}</p>
            <p>实际来源：{{ models(p.observation.backend ? [p.observation.backend] : []) }}</p>
            <details>
              <summary>观测完整上报</summary>
              <pre>{{ JSON.stringify(p.observation, null, 2) }}</pre>
            </details>
          </template>
          <p v-if="!p.observation" class="muted">尚无观测镜像</p>
        </section>
        <section aria-label="最新判定">
          <h4>最新判定</h4>
          <dl v-if="p.decision">
            <dt>三值结论</dt>
            <dd>{{ verdicts[p.decision.verdict] ?? `未知结论（${p.decision.verdict}）` }}</dd>
            <dt>原因码</dt>
            <dd>{{ p.decision.reason_codes.map(reasonLabel).join('；') || '无（通过）' }}</dd>
            <dt>实例 / 生命周期 / 上报</dt>
            <dd>
              {{ p.decision.instance_id }} / {{ p.decision.lifecycle }} /
              {{ time(p.decision.reported_at) }}
            </dd>
            <dt>实际模板 / 摘要</dt>
            <dd>
              {{ p.decision.template_version_id ?? '未提供' }} /
              {{ p.decision.template_sha256 ?? '无' }}
            </dd>
            <dt>实际后端 / 模型</dt>
            <dd>
              {{
                p.decision.backend_provenance
                  ? models(p.decision.backend_provenance)
                  : `${p.decision.backend_id ?? '未提供'}：${p.decision.model_ids?.join('、') || '无模型标识'}`
              }}
            </dd>
          </dl>
          <details v-if="p.decision">
            <summary>判定完整上报（含证据及锁存违规）</summary>
            <pre>{{ JSON.stringify(p.decision, null, 2) }}</pre>
          </details>
          <p v-if="!p.decision" class="muted">尚无判定镜像</p>
        </section>
        <section aria-label="各路观测健康">
          <h4>各路观测健康</h4>
          <article v-for="v in p.health ?? []" :key="v.event_id">
            <strong>{{ v.stream_id ?? '未标识流' }}：{{ v.status }}</strong>
            <p>
              原因 / 说明：{{ v.reason_code ? reasonLabel(v.reason_code) : '未提供' }} /
              {{ v.detail ?? '无补充说明' }}
            </p>
            <p>
              发生 / 时间锚 / 偏移：{{ time(v.occurred_at) }} / {{ time(v.source_anchor) }} /
              {{ time(v.anchor_offset) }}
            </p>
          </article>
          <p v-if="!p.health?.length" class="muted">尚无流健康事实；不推断为健康。</p>
        </section>
      </div>
    </article>
  </section>
</template>

<style scoped>
.runtime {
  margin-top: 1.5rem;
}
.runtime__grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 1rem 1.5rem;
}
.runtime__grid section {
  min-width: 0;
  overflow-wrap: anywhere;
}
.runtime dl {
  display: grid;
  grid-template-columns: minmax(7rem, auto) 1fr;
  gap: 0.4rem 0.8rem;
  margin: 0;
}
.runtime dd {
  margin: 0;
}
.runtime .muted {
  color: var(--el-text-color-secondary);
}
@media (max-width: 720px) {
  .runtime__grid {
    grid-template-columns: 1fr;
  }
}
</style>
