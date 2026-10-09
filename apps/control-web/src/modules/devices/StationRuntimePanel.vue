<script setup lang="ts">
import { ElMessage } from 'element-plus'
import { computed, nextTick, onMounted, onUnmounted, ref } from 'vue'
import {
  openRuntimeProjection,
  readMonitorViolations,
  ControlPlaneError,
  type RuntimeViolation,
  type RuntimeStationProjection,
  type StationView,
} from '@/api/controlPlane'
import { useSessionStore } from '@/session/store'

const props = defineProps<{ stations: StationView[] }>()
const session = useSessionStore()
const projections = ref(new Map<string, RuntimeStationProjection>())
const connected = ref(false)
const hasConnected = ref(false)
const violations = ref<RuntimeViolation[]>([])
const violationsTotal = ref(0)
const violationsLoading = ref(true)
const violationsError = ref('')
const selectedViolation = ref<RuntimeViolation | null>(null)
const selectedHint = ref('')
const seenViolations = new Set<string>()
const pendingDecisions = new Set<string>()
// 实时资格由 Edge 本机单调钟与 Center 已认证首次入库联合确认；Web 不比较跨机墙钟。
let live = false
let active = true
let loadRevision = 0
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
const physicalSafetyLabels: Record<string, string> = {
  protected: '物理防错有效',
  stale: '物理防错状态未知',
  failed: '物理防错失效',
  unknown: '物理防错状态未知（未识别事实）',
}
const authorizationLabels: Record<string, string> = {
  active: 'Center 授权有效',
  expired: 'Center 授权已到期',
  missing: 'Center 无当前授权',
}
const edgeAuthorityLabels: Record<string, string> = {
  active: 'Edge 本地授权有效',
  expired: 'Edge 本地授权已到期',
  missing: 'Edge 本地无授权',
}
const writeStateLabels: Record<string, string> = {
  enabled: 'Edge 物理写入门禁已启用',
  stopped: 'Edge 已停止物理写入',
}
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
const physicalDisplayStatus = (value: RuntimeStationProjection) =>
  hasConnected.value && !connected.value ? 'stale' : (value.physical_safety?.status ?? 'unknown')
const physicalDisplayDetail = (value: RuntimeStationProjection) =>
  hasConnected.value && !connected.value
    ? 'Center 实时流已中断；以下仅为最后已知镜像，不表示仍可写。'
    : (value.physical_safety?.detail ?? '尚无物理执行权镜像')
const models = (sources: unknown) =>
  ((sources ?? []) as { backend_id: string; model_ids: string[] }[])
    .map((source) => `${source.backend_id}：${source.model_ids.join('、') || '无模型标识'}`)
    .join('；') || '未提供'
function update(value: RuntimeStationProjection): void {
  const current = projections.value.get(value.station_id)
  if (current && JSON.stringify(current) === JSON.stringify(value)) return
  projections.value = new Map(projections.value).set(value.station_id, value)
}
async function loadViolations(): Promise<void> {
  const revision = ++loadRevision
  violationsLoading.value = true
  violationsError.value = ''
  try {
    const result = await readMonitorViolations()
    if (!active || revision !== loadRevision) return
    const alerts = result.items.filter(
      (value) =>
        !seenViolations.has(value.event_id) && pendingDecisions.has(value.decision_event_id),
    )
    violations.value = result.items
    violationsTotal.value = result.total
    for (const value of result.items) seenViolations.add(value.event_id)
    pendingDecisions.clear()
    for (const value of alerts) {
      ElMessage.warning({
        message: `新上报违规：${reasonLabel(value.violation.reason_code)}；原确认时间 ${time(value.latched_at ?? null)}`,
        duration: 5000,
      })
    }
  } catch (error) {
    if (active && revision === loadRevision) {
      violationsError.value =
        error instanceof ControlPlaneError ? (error.detail ?? error.message) : '违规归档读取失败'
    }
  } finally {
    if (active && revision === loadRevision) violationsLoading.value = false
  }
}

async function locateViolation(value: RuntimeViolation): Promise<void> {
  selectedViolation.value = value
  const current = projections.value.get(value.station_id)?.decision
  selectedHint.value =
    current?.event_id === value.decision_event_id
      ? '已定位至当前运行镜像中的原判定。'
      : '该原判定不在当前最新运行镜像中；请使用下方稳定判定事件 ID 核对归档。'
  await nextTick()
  const id =
    current?.event_id === value.decision_event_id
      ? `runtime-decision-${value.station_id}`
      : 'violation-selection'
  document.getElementById(id)?.focus()
}

onMounted(() => {
  if (!session.may('monitor.report.view')) return
  void loadViolations()
  closeStream = openRuntimeProjection(
    update,
    (value) => {
      connected.value = value
      hasConnected.value ||= value
      if (!value) live = false
    },
    (value) => {
      if (!live || !value.realtime || !value.violations?.length) return
      pendingDecisions.add(value.event_id)
      void loadViolations()
    },
    () => {
      live = true
      // 断线补报只更新列表，不把本次连接的历史快照当成新告警。
      void loadViolations()
    },
  )
})
onUnmounted(() => {
  active = false
  closeStream?.()
})
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
    <section class="runtime__violations" aria-labelledby="violation-heading">
      <header class="runtime__violations-header">
        <div>
          <h3 id="violation-heading">违规告警归档</h3>
          <p class="muted">按中心归档时间排序；原发生时间以推理机证据时间轴为准。</p>
        </div>
        <button type="button" @click="loadViolations">刷新归档</button>
      </header>
      <p v-if="violationsLoading" class="muted">正在读取违规归档…</p>
      <p v-if="violationsError" role="alert">{{ violationsError }}</p>
      <p v-if="!violationsLoading && !violationsError && !violations.length" class="muted">
        尚无已归档违规；不推断工位合规。
      </p>
      <p v-if="violations.length" class="muted">
        显示最近 {{ violations.length }} / {{ violationsTotal }} 条归档事件。
      </p>
      <ul v-if="violations.length" class="runtime__violation-list">
        <li v-for="v in violations" :key="v.event_id">
          <strong>{{ reasonLabel(v.violation.reason_code) }}</strong>
          <p>
            工位 {{ stationLabel(v.station_id) }}（{{ v.station_id }}）/ 实例 {{ v.instance_id }}
          </p>
          <p>原确认：{{ v.latched_at ? time(v.latched_at) : '旧归档未记录墙钟时间' }}</p>
          <p>证据时间轴：{{ time(v.violation.evidence.anchor) }}</p>
          <p>来源判定上报：{{ time(v.reported_at) }} / 中心接收：{{ time(v.received_at) }}</p>
          <p>来源推理机：{{ v.host_id }} / 归档事件：{{ v.event_id }}</p>
          <button type="button" @click="locateViolation(v)">定位相关判定</button>
        </li>
      </ul>
      <div
        v-if="selectedViolation"
        id="violation-selection"
        tabindex="-1"
        role="status"
        aria-live="polite"
      >
        {{ selectedHint }} 判定事件：{{ selectedViolation.decision_event_id }}
      </div>
    </section>
    <p v-if="!projections.size" class="muted">尚无运行镜像；连接恢复后会继续读取最新事实。</p>
    <article v-for="p in projections.values()" :key="p.station_id">
      <h3>
        工位 {{ stationLabel(p.station_id) }} <small>{{ p.station_id }}</small>
      </h3>
      <div class="runtime__grid">
        <section aria-label="物理防错与执行权">
          <h4>物理防错与执行权</h4>
          <template v-if="p.physical_safety">
            <p>
              状态：
              <strong>{{
                physicalSafetyLabels[physicalDisplayStatus(p)] ??
                `未知状态（${physicalDisplayStatus(p)}）`
              }}</strong>
            </p>
            <p>说明：{{ physicalDisplayDetail(p) }}</p>
            <p>
              Center 来源：
              {{
                authorizationLabels[p.physical_safety.center_authorization.state] ??
                `未知授权状态（${p.physical_safety.center_authorization.state}）`
              }}
            </p>
            <p v-if="p.physical_safety.center_authorization.holder_host_id">
              授权推理机：{{ p.physical_safety.center_authorization.holder_host_id }}
            </p>
            <p v-if="p.physical_safety.center_authorization.lease_expires_at">
              授权到期：{{ time(p.physical_safety.center_authorization.lease_expires_at) }}
            </p>
            <p>Edge 来源：{{ p.physical_safety.edge_status.host_id ?? '未能确定来源推理机' }}</p>
            <p>
              Edge 授权：{{
                edgeAuthorityLabels[p.physical_safety.edge_status.authority_state] ??
                `未知授权状态（${p.physical_safety.edge_status.authority_state}）`
              }}
            </p>
            <p>
              Edge 写入：{{
                writeStateLabels[p.physical_safety.edge_status.write_state] ??
                `未知写入状态（${p.physical_safety.edge_status.write_state}）`
              }}
            </p>
            <p v-if="p.physical_safety.edge_status.reason_code">
              Edge 原因：{{ p.physical_safety.edge_status.reason_code }}
              <template v-if="p.physical_safety.edge_status.detail">
                — {{ p.physical_safety.edge_status.detail }}
              </template>
            </p>
            <p>
              Edge 已知时间：{{ time(p.physical_safety.edge_status.reported_at) }}； Center 接收：{{
                time(p.physical_safety.edge_status.received_at)
              }}
            </p>
            <p v-if="p.physical_safety.edge_status.stale || (hasConnected && !connected)">
              数据已过期；不会沿用最后一次正常状态表示仍可写。
            </p>
          </template>
          <p v-else class="muted">尚无物理执行权镜像；不推断为可写。</p>
        </section>
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
        <section :id="`runtime-decision-${p.station_id}`" aria-label="最新判定" tabindex="-1">
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
.runtime__violations {
  margin: 1rem 0;
  padding: 1rem 0;
  border-top: 1px solid var(--el-border-color);
}
.runtime__violations-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 1rem;
  flex-wrap: wrap;
}
.runtime__violation-list {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(min(100%, 20rem), 1fr));
  gap: 0.75rem;
  list-style: none;
  padding: 0;
}
.runtime__violation-list li {
  border: 1px solid var(--el-border-color);
  border-inline-start: 3px solid var(--el-color-warning);
  padding: 0.75rem;
  overflow-wrap: anywhere;
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
