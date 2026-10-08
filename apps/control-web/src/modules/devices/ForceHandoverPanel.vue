<script setup lang="ts">
/**
 * 强制改绑双人确认（§5.17）。
 *
 * 独立小面板：建立请求即操作者第一确认，第二确认必须由另一名当前具备强制改绑权限的用户
 * 完成。风险原文来自服务器（`readHandoverRisk` / 请求记录），前端不自拟第二段；客户端只
 * 负责展示与禁用，后端仍是唯一权威。面板不修改工位归属或物理执行权租约。
 *
 * 工位/旧机/目标机在查看权限可用时从 DevicesView 已加载的工位与推理机列表选择，否则按
 * 已知 ID 填写——与设备页既有的“没有查看权限可按已知 ID 操作”一致。
 */
import { ElButton, ElCheckbox, ElForm, ElFormItem, ElInput, ElOption, ElSelect } from 'element-plus'
import { computed, onMounted, ref, watch } from 'vue'

import {
  ControlPlaneError,
  confirmHandover,
  createHandover,
  readHandover,
  readHandoverRisk,
  type HandoverView,
  type InferenceHostView,
  type StationView,
} from '@/api/controlPlane'
import { useSessionStore } from '@/session/store'

const props = withDefaults(
  defineProps<{
    stations?: StationView[]
    hosts?: InferenceHostView[]
  }>(),
  { stations: () => [], hosts: () => [] },
)

const session = useSessionStore()

const mayForce = computed(() => session.may('execution.handover.edit'))

const riskStatement = ref('')
const riskFailure = ref('')
const riskLoading = ref(true)

const createDraft = ref({ station_id: '', from_host_id: '', to_host_id: '' })
const createAck = ref(false)
const creating = ref(false)

const requestIdInput = ref('')
const readAck = ref(false)
const reading = ref(false)
const confirming = ref(false)

const record = ref<HandoverView | null>(null)
const failure = ref('')
const statusMessage = ref('')

let riskSequence = 0
let readSequence = 0

const riskReady = computed(() => riskStatement.value !== '')
const createReady = computed(
  () =>
    riskReady.value &&
    createDraft.value.station_id.trim() !== '' &&
    createDraft.value.from_host_id.trim() !== '' &&
    createDraft.value.to_host_id.trim() !== '' &&
    createAck.value,
)
const secondConfirmed = computed(() => record.value?.second_operator_id != null)
const sameOperator = computed(
  () => record.value !== null && record.value.operator_id === session.current?.user_id,
)
const maySecond = computed(
  () => record.value !== null && !secondConfirmed.value && !sameOperator.value,
)
const confirmReady = computed(() => maySecond.value && readAck.value && riskReady.value)

function messageOf(error: unknown): string {
  if (!(error instanceof ControlPlaneError)) {
    throw error
  }
  return error.detail ?? error.message
}

function recordFailure(error: unknown): void {
  failure.value = messageOf(error)
}

function clearFailure(): void {
  failure.value = ''
}

async function loadRisk(): Promise<void> {
  const sequence = riskSequence + 1
  riskSequence = sequence
  riskLoading.value = true
  riskFailure.value = ''
  try {
    const view = await readHandoverRisk()
    if (sequence !== riskSequence) {
      return
    }
    riskStatement.value = view.risk_statement
  } catch (error) {
    if (sequence !== riskSequence) {
      return
    }
    if (!(error instanceof ControlPlaneError)) {
      throw error
    }
    riskStatement.value = ''
    riskFailure.value = error.detail ?? error.message
  } finally {
    if (sequence === riskSequence) {
      riskLoading.value = false
    }
  }
}

async function submitCreate(): Promise<void> {
  clearFailure()
  statusMessage.value = ''
  if (!createReady.value) {
    return
  }
  creating.value = true
  try {
    const created = await createHandover({
      station_id: createDraft.value.station_id.trim(),
      from_host_id: createDraft.value.from_host_id.trim(),
      to_host_id: createDraft.value.to_host_id.trim(),
      risk_acknowledgement: riskStatement.value,
    })
    record.value = created
    createAck.value = false
    readAck.value = false
    statusMessage.value = '等待另一名有权用户确认'
  } catch (error) {
    recordFailure(error)
  } finally {
    creating.value = false
  }
}

async function loadRecord(): Promise<void> {
  clearFailure()
  statusMessage.value = ''
  const handoverId = requestIdInput.value.trim()
  if (handoverId === '') {
    failure.value = '请输入请求 ID'
    return
  }
  const sequence = readSequence + 1
  readSequence = sequence
  reading.value = true
  readAck.value = false
  try {
    const loaded = await readHandover(handoverId)
    // 输入已变（或又发起了一次读取）时丢弃旧响应，不把旧请求覆盖到当前输入。
    if (sequence !== readSequence || requestIdInput.value.trim() !== handoverId) {
      return
    }
    record.value = loaded
    statusMessage.value =
      loaded.second_operator_id === null ? '等待另一名有权用户确认' : '审批已满足，尚未切换执行权'
  } catch (error) {
    if (sequence !== readSequence) {
      return
    }
    if (!(error instanceof ControlPlaneError)) {
      throw error
    }
    record.value = null
    failure.value = error.detail ?? error.message
  } finally {
    if (sequence === readSequence) {
      reading.value = false
    }
  }
}

async function submitConfirm(): Promise<void> {
  clearFailure()
  const current = record.value
  if (current === null || !confirmReady.value) {
    return
  }
  confirming.value = true
  try {
    // 只提交被展示的同一冻结内容：工位/旧机/目标机取自记录，不随输入框或草稿重拼。
    const confirmed = await confirmHandover(current.handover_id, {
      station_id: current.station_id,
      from_host_id: current.from_host_id,
      to_host_id: current.to_host_id,
      risk_acknowledgement: current.risk_statement,
    })
    record.value = confirmed
    readAck.value = false
    statusMessage.value = '审批已满足，尚未切换执行权'
  } catch (error) {
    recordFailure(error)
  } finally {
    confirming.value = false
  }
}

const stationLabel = (id: string): string =>
  props.stations.find((station) => station.id === id)?.name ?? id
const hostLabel = (id: string): string => props.hosts.find((host) => host.id === id)?.name ?? id
const time = (value: string): string =>
  new Date(value).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai' })

// 请求 ID 输入变化（或读取另一请求）时清掉旧确认：旧 ack 不能指向新 ID。
watch(requestIdInput, (value) => {
  if (record.value !== null && value.trim() === record.value.handover_id) {
    return
  }
  record.value = null
  readAck.value = false
})

// 草稿内容变化必须重新勾选风险确认。
watch(
  () => [
    createDraft.value.station_id,
    createDraft.value.from_host_id,
    createDraft.value.to_host_id,
  ],
  () => {
    createAck.value = false
  },
)

// 切换登录身份（登出/登录会重新挂载，这里再兜底一次）不沿用任何旧确认。
watch(
  () => session.current?.user_id,
  () => {
    createAck.value = false
    readAck.value = false
    record.value = null
    requestIdInput.value = ''
    statusMessage.value = ''
    clearFailure()
  },
)

onMounted(() => {
  if (mayForce.value) {
    void loadRisk()
  }
})
</script>

<template>
  <section v-if="mayForce" class="handover" aria-labelledby="handover-heading">
    <header>
      <div>
        <h2 id="handover-heading">强制改绑双人确认</h2>
        <p>
          旧机永久失联时的强制通道：操作者建立请求即第一确认，另一名具备强制改绑权限的用户
          再确认同一内容。审批满足不切换执行权，也不改变工位归属。
        </p>
      </div>
    </header>

    <p v-if="riskLoading" class="handover__muted" role="status" aria-live="polite">
      正在读取服务器风险原文…
    </p>
    <p v-else-if="riskFailure" class="handover__failure" role="alert">
      无法读取服务器风险原文，暂不能提交：{{ riskFailure }}
    </p>

    <template v-if="riskReady">
      <p class="handover__warning">{{ riskStatement }}</p>

      <ElForm label-position="top" @submit.prevent="submitCreate">
        <h3>建立请求（操作者第一确认）</h3>
        <ElFormItem label="工位">
          <ElSelect
            v-if="stations.length"
            v-model="createDraft.station_id"
            aria-label="工位"
            class="handover__select"
          >
            <ElOption
              v-for="station in stations"
              :key="station.id"
              :label="`${station.name}（${station.code}）`"
              :value="station.id"
            />
          </ElSelect>
          <ElInput
            v-else
            id="handover-station-id"
            v-model="createDraft.station_id"
            name="station_id"
            autocomplete="off"
          />
        </ElFormItem>
        <ElFormItem label="旧机（当前持有物理执行权的推理机）">
          <ElSelect
            v-if="hosts.length"
            v-model="createDraft.from_host_id"
            aria-label="旧机"
            class="handover__select"
          >
            <ElOption v-for="host in hosts" :key="host.id" :label="host.name" :value="host.id" />
          </ElSelect>
          <ElInput
            v-else
            id="handover-from-host-id"
            v-model="createDraft.from_host_id"
            name="from_host_id"
            autocomplete="off"
          />
        </ElFormItem>
        <ElFormItem label="目标机（接管的推理机）">
          <ElSelect
            v-if="hosts.length"
            v-model="createDraft.to_host_id"
            aria-label="目标机"
            class="handover__select"
          >
            <ElOption v-for="host in hosts" :key="host.id" :label="host.name" :value="host.id" />
          </ElSelect>
          <ElInput
            v-else
            id="handover-to-host-id"
            v-model="createDraft.to_host_id"
            name="to_host_id"
            autocomplete="off"
          />
        </ElFormItem>
        <ElCheckbox v-model="createAck" :disabled="!riskReady">
          我已阅读并确认上述风险，建立强制改绑请求
        </ElCheckbox>
        <div>
          <ElButton
            type="primary"
            :disabled="!createReady || creating"
            :loading="creating"
            @click="submitCreate"
          >
            建立并完成第一确认
          </ElButton>
        </div>
      </ElForm>
    </template>

    <ElForm label-position="top" @submit.prevent="loadRecord">
      <h3>第二人确认</h3>
      <ElFormItem label="请求 ID">
        <ElInput
          id="handover-request-id"
          v-model="requestIdInput"
          name="handover_id"
          autocomplete="off"
        />
      </ElFormItem>
      <ElButton
        :disabled="requestIdInput.trim() === '' || reading"
        :loading="reading"
        @click="loadRecord"
      >
        读取请求
      </ElButton>
    </ElForm>

    <section v-if="record" class="handover__record" aria-label="强制改绑请求内容">
      <h3>请求 {{ record.handover_id }}</h3>
      <p class="handover__muted">
        将此请求 ID 分享给另一名具备强制改绑权限的用户；对方输入同一 ID 读取本内容后再确认。
      </p>
      <dl>
        <dt>工位</dt>
        <dd>{{ stationLabel(record.station_id) }}</dd>
        <dt>旧机 / 目标机</dt>
        <dd>{{ hostLabel(record.from_host_id) }} / {{ hostLabel(record.to_host_id) }}</dd>
        <dt>操作者</dt>
        <dd>{{ record.operator_id }}</dd>
        <dt>第一确认时间</dt>
        <dd>{{ time(record.operator_confirmed_at) }}</dd>
        <dt>第二确认</dt>
        <dd>
          {{
            record.second_operator_id === null
              ? '等待另一名有权用户确认'
              : `${record.second_operator_id}（${record.second_confirmed_at === null ? '' : time(record.second_confirmed_at)}）`
          }}
        </dd>
      </dl>
      <p class="handover__warning">{{ record.risk_statement }}</p>

      <div v-if="maySecond" class="handover__confirm">
        <ElCheckbox v-model="readAck" :disabled="!riskReady">
          我已阅读上述风险原文，确认该强制改绑请求
        </ElCheckbox>
        <ElButton
          type="danger"
          :disabled="!confirmReady || confirming"
          :loading="confirming"
          @click="submitConfirm"
        >
          完成第二确认
        </ElButton>
      </div>
      <p v-else-if="secondConfirmed" class="handover__explain">
        该请求已完成两人确认；审批已满足，尚未切换执行权。
      </p>
      <p v-else-if="sameOperator" class="handover__explain">
        你是本请求的操作者；必须由另一名当前具备强制改绑权限的用户确认。
      </p>
    </section>

    <p v-if="failure" class="handover__failure" role="alert">{{ failure }}</p>
    <p v-if="statusMessage" class="handover__status" role="status" aria-live="polite">
      {{ statusMessage }}
    </p>
  </section>
</template>

<style scoped>
.handover {
  margin-top: 1.5rem;
  border: 1px solid var(--el-border-color);
  border-radius: var(--el-border-radius-base);
  padding: 1rem 1.25rem;
}
.handover h3 {
  margin: 0 0 0.5rem;
  font-size: 1rem;
}
.handover__warning {
  margin: 0.5rem 0;
  padding: 0.6rem 0.75rem;
  border-left: 3px solid var(--el-color-warning);
  background: var(--el-color-warning-light-9);
  color: var(--el-text-color-primary);
  font-weight: 600;
  overflow-wrap: anywhere;
}
.handover__select {
  width: 100%;
}
.handover__record dl {
  display: grid;
  grid-template-columns: minmax(7rem, auto) 1fr;
  gap: 0.4rem 0.8rem;
  margin: 0.5rem 0;
}
.handover__record dd {
  margin: 0;
  overflow-wrap: anywhere;
}
.handover__muted {
  color: var(--el-text-color-secondary);
}
.handover__explain {
  color: var(--el-text-color-secondary);
}
.handover__failure {
  color: var(--el-color-danger);
}
.handover__status {
  font-weight: 600;
}
.handover__confirm {
  display: flex;
  align-items: center;
  gap: 1rem;
  flex-wrap: wrap;
  margin-top: 0.75rem;
}
</style>
