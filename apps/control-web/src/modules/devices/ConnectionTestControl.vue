<script setup lang="ts">
import { ElButton, ElTag } from 'element-plus'
import { computed, onBeforeUnmount, ref } from 'vue'

import {
  ControlPlaneError,
  enqueueConnectorConnectionTest,
  readDeviceCommand,
  type PendingCommandView,
} from '@/api/controlPlane'

const COMMAND_POLL_INTERVAL_MS = 100
const TERMINAL_STATUSES = new Set(['succeeded', 'failed', 'rejected'])
const KNOWN_FAILURE_CODES = new Set([
  'COMMAND_TARGET_NOT_FOUND',
  'COMMAND_CONFIGURATION_CHANGED',
  'COMMAND_CREDENTIALS_NOT_CONFIGURED',
  'COMMAND_RESULT_INVALID',
  'COMMAND_TARGET_DEACTIVATED',
])

type TagType = 'success' | 'danger' | 'warning' | 'info'

const props = withDefaults(
  defineProps<{
    connectorId: string
    connectorName: string
    mayView: boolean
    showButton?: boolean
  }>(),
  { showButton: true },
)

const emit = defineEmits<{
  completed: []
}>()

const command = ref<PendingCommandView | null>(null)
const submissionFailure = ref('')
const pollingFailure = ref('')
const pollingStopped = ref(false)
const submitting = ref(false)
const retryIdempotencyKey = ref<string | null>(null)
let pollTimer: number | null = null
let disposed = false

const isActive = computed(() => {
  const status = command.value?.status
  return status !== undefined && !TERMINAL_STATUSES.has(status)
})

const isTerminal = computed(() => {
  const status = command.value?.status
  return status !== undefined && TERMINAL_STATUSES.has(status)
})

const presentation = computed(() => presentCommand(command.value))
const unknownFailureCode = computed(() => {
  const code = command.value?.failure_code
  return code !== null && code !== undefined && !KNOWN_FAILURE_CODES.has(code)
})
const buttonLabel = computed(() => {
  if (pollingStopped.value) {
    return '重试读取状态'
  }
  if (submitting.value || isActive.value) {
    return '测试中'
  }
  if (isTerminal.value) {
    return '重新测试连接'
  }
  return '测试连接'
})

function newIdempotencyKey(): string {
  const secureCrypto = globalThis.crypto
  if (typeof secureCrypto.randomUUID === 'function') {
    return secureCrypto.randomUUID()
  }
  const bytes = new Uint8Array(16)
  secureCrypto.getRandomValues(bytes)
  bytes[6] = (bytes[6]! & 0x0f) | 0x40
  bytes[8] = (bytes[8]! & 0x3f) | 0x80
  const hex = Array.from(bytes, (byte) => byte.toString(16).padStart(2, '0')).join('')
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`
}

function displayError(error: unknown): string {
  if (error instanceof ControlPlaneError) {
    return error.detail ?? error.message
  }
  return '请求未能完成，请稍后重试'
}

function clearPollTimer(): void {
  if (pollTimer !== null) {
    window.clearTimeout(pollTimer)
    pollTimer = null
  }
}

function schedulePoll(commandId: string, delay = COMMAND_POLL_INTERVAL_MS): void {
  clearPollTimer()
  pollTimer = window.setTimeout(() => {
    pollTimer = null
    void refresh(commandId)
  }, delay)
}

async function refresh(commandId: string): Promise<void> {
  if (disposed || !props.mayView || command.value?.id !== commandId) {
    return
  }
  try {
    const latest = await readDeviceCommand(commandId)
    if (disposed || command.value?.id !== commandId) {
      return
    }
    command.value = latest
    pollingFailure.value = ''
    pollingStopped.value = false
    if (TERMINAL_STATUSES.has(latest.status)) {
      emit('completed')
      return
    }
    schedulePoll(commandId)
  } catch (error) {
    if (disposed || command.value?.id !== commandId) {
      return
    }
    if (error instanceof ControlPlaneError && (error.status === 401 || error.status === 403)) {
      pollingStopped.value = true
      pollingFailure.value = error.status === 403 ? '当前账号没有查看结果的权限' : '登录状态已失效'
      clearPollTimer()
      return
    }
    pollingFailure.value = displayError(error)
    schedulePoll(commandId)
  }
}

async function retryPolling(): Promise<void> {
  const current = command.value
  if (current === null) {
    return
  }
  clearPollTimer()
  pollingStopped.value = false
  pollingFailure.value = ''
  await refresh(current.id)
}

async function startTest(): Promise<void> {
  if (!props.connectorId.trim()) {
    submissionFailure.value = '请输入连接器 ID'
    return
  }
  if (submitting.value || (isActive.value && !pollingStopped.value)) {
    return
  }
  clearPollTimer()
  submissionFailure.value = ''
  pollingFailure.value = ''
  pollingStopped.value = false
  const idempotencyKey = retryIdempotencyKey.value ?? newIdempotencyKey()
  retryIdempotencyKey.value = idempotencyKey
  submitting.value = true
  try {
    const accepted = await enqueueConnectorConnectionTest(props.connectorId, idempotencyKey)
    command.value = accepted
    // 一旦中心确认入队，后续重新测试必须使用新的幂等键；网络失败则保留旧键，确保重试
    // 不会在请求已到达中心但响应丢失时创建第二条命令。
    retryIdempotencyKey.value = null
    if (TERMINAL_STATUSES.has(accepted.status)) {
      emit('completed')
      return
    }
    if (!props.mayView) {
      return
    }
    // 延迟一次读取，让“已提交”状态先对操作者可见，也避免把异步命令伪装成同步测试。
    schedulePoll(accepted.id, 0)
  } catch (error) {
    submissionFailure.value = displayError(error)
  } finally {
    submitting.value = false
  }
}

defineExpose({ startTest })

function presentCommand(value: PendingCommandView | null): {
  label: string
  tag: TagType
  detail: string
} {
  if (value === null) {
    return { label: '', tag: 'info', detail: '' }
  }
  switch (value.status) {
    case 'pending':
      return { label: '连接测试已提交，等待推理机执行', tag: 'warning', detail: '' }
    case 'claimed':
      return { label: '推理机正在执行连接测试', tag: 'warning', detail: '' }
    case 'succeeded':
      return {
        label: value.result === 'reachable' ? '连接测试成功' : '连接测试完成，但结果未知',
        tag: value.result === 'reachable' ? 'success' : 'warning',
        detail: value.result_detail ?? '',
      }
    case 'failed':
      return {
        label: value.result === 'unreachable' ? '连接测试失败' : '连接测试结束，但结果未知',
        tag: 'danger',
        detail: value.result_detail ?? '',
      }
    case 'rejected':
      return {
        label: '连接测试被拒绝',
        tag: 'warning',
        detail: value.result_detail ?? '中心未提供拒绝原因',
      }
    default:
      return {
        label: `连接测试状态未知（${String(value.status)}）`,
        tag: 'warning',
        detail: value.result_detail ?? '',
      }
  }
}

onBeforeUnmount(() => {
  disposed = true
  clearPollTimer()
})
</script>

<template>
  <div class="connection-test">
    <ElButton
      v-if="props.showButton"
      type="primary"
      plain
      :disabled="submitting || (isActive && !pollingStopped) || !props.connectorId.trim()"
      :aria-label="`${buttonLabel}：${props.connectorName}`"
      @click="pollingStopped ? retryPolling() : startTest()"
    >
      {{ buttonLabel }}
    </ElButton>

    <p v-if="submissionFailure" class="connection-test__failure" role="alert">
      {{ submissionFailure }}
      <span class="connection-test__hint">命令未确认入队，可安全重试。</span>
    </p>

    <div v-else-if="command" class="connection-test__result">
      <p class="connection-test__status" role="status" aria-live="polite">
        <ElTag :type="presentation.tag" disable-transitions>{{ presentation.label }}</ElTag>
      </p>
      <p v-if="presentation.detail" class="connection-test__detail">
        {{ presentation.detail }}
      </p>
      <p v-if="command.failure_code" class="connection-test__code">
        拒绝码：{{ command.failure_code }}
      </p>
      <p v-if="unknownFailureCode" class="connection-test__hint">请查看命令详情或联系管理员。</p>
      <p v-if="!props.mayView && !isTerminal" class="connection-test__hint" role="status">
        命令已提交；当前账号没有查看结果的权限。
      </p>
      <p v-if="pollingFailure" class="connection-test__failure" role="alert">
        {{ pollingFailure }}；
        <span v-if="pollingStopped">已停止读取命令状态。</span>
        <span v-else>正在安全重试读取命令状态。</span>
      </p>
    </div>
  </div>
</template>

<style scoped>
.connection-test {
  display: flex;
  min-width: 15rem;
  flex-direction: column;
  align-items: flex-start;
  gap: 0.35rem;
}

.connection-test__status,
.connection-test__detail,
.connection-test__code,
.connection-test__failure,
.connection-test__hint {
  margin: 0;
  line-height: 1.45;
}

.connection-test__detail,
.connection-test__code,
.connection-test__hint {
  color: var(--el-text-color-secondary);
  font-size: 0.8rem;
  white-space: normal;
}

.connection-test__failure {
  color: var(--el-color-danger);
  font-size: 0.8rem;
  white-space: normal;
}

.connection-test__hint {
  display: block;
}
</style>
