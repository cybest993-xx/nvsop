<script setup lang="ts">
import { ElButton } from 'element-plus'
import { computed, onUnmounted, ref, watch } from 'vue'

import {
  ControlPlaneError,
  enqueueRecordingCompression,
  readRecordingCompressionCommand,
  type CompressionCommandView,
  type InferenceHostView,
} from '@/api/controlPlane'
import { useSessionStore } from '@/session/store'

const props = defineProps<{ hosts: InferenceHostView[] }>()
const session = useSessionStore()
const mayView = computed(() => session.may('device.inference_host.view'))
const mayEdit = computed(() => mayView.value && session.may('device.inference_host.edit'))
const hostId = ref('')
const command = ref<CompressionCommandView | null>(null)
const failure = ref('')
const loading = ref(false)
const retry = ref<{ action: 'inspect' | 'pause' | 'resume' | 'abort'; key: string } | null>(null)
let generation = 0
const pending = computed(
  () => command.value?.status === 'pending' || command.value?.status === 'claimed',
)
const observed = computed(() => command.value?.observation)
const stateLabels: Record<string, string> = {
  idle: '等待下一轮',
  running: '正在压缩',
  pausing: '等待进程暂停',
  paused: '已暂停',
  resuming: '等待进程恢复',
  aborting: '等待进程退出',
  aborted: '已中止',
  busy: '已有任务占用素材',
  unavailable: '本机压缩不可用',
}
const label = computed(() =>
  observed.value ? (stateLabels[observed.value.state] ?? observed.value.state) : '尚未取得实际状态',
)
const observedAt = computed(() =>
  command.value?.observed_at
    ? new Intl.DateTimeFormat('zh-CN', {
        timeZone: 'Asia/Shanghai',
        dateStyle: 'short',
        timeStyle: 'medium',
      }).format(new Date(command.value.observed_at))
    : '',
)

function clear() {
  generation++
  command.value = null
  retry.value = null
  failure.value = ''
  loading.value = false
}
watch([hostId, mayView], clear)
onUnmounted(clear)
watch(
  () => props.hosts,
  (hosts) => {
    if (!hosts.some((host) => host.id === hostId.value)) hostId.value = hosts[0]?.id ?? ''
  },
  { immediate: true },
)

async function submit(action: 'inspect' | 'pause' | 'resume' | 'abort') {
  if (!mayEdit.value || !hostId.value || pending.value) return
  const current = ++generation
  loading.value = true
  failure.value = ''
  if (retry.value?.action !== action) retry.value = { action, key: crypto.randomUUID() }
  try {
    const result = await enqueueRecordingCompression(hostId.value, action, retry.value.key)
    if (current === generation && mayView.value) {
      command.value = result
      retry.value = null
    }
  } catch (error) {
    if (current === generation)
      failure.value =
        error instanceof ControlPlaneError ? error.message : '请求未确认，请重试同一操作。'
  } finally {
    if (current === generation) loading.value = false
  }
}

async function query() {
  if (!command.value || !mayView.value) return
  const current = ++generation
  loading.value = true
  failure.value = ''
  try {
    const result = await readRecordingCompressionCommand(hostId.value, command.value.id)
    if (current === generation && mayView.value) command.value = result
  } catch (error) {
    if (current === generation)
      failure.value = error instanceof ControlPlaneError ? error.message : '查询失败，可重新查询。'
  } finally {
    if (current === generation) loading.value = false
  }
}
</script>

<template>
  <section v-if="mayView" class="compression" aria-labelledby="compression-heading">
    <div class="compression__header">
      <div>
        <p class="compression__eyebrow">推理机 / 录像管理</p>
        <h2 id="compression-heading">录像压缩归档</h2>
        <p>暂停保留当前进度；中止后从保留原件重新编码。录像仅在校验通过后替换。</p>
      </div>
      <label for="recording-compression-host" class="compression__host"
        >推理机
        <select id="recording-compression-host" v-model="hostId" aria-label="录像压缩推理机">
          <option v-if="!hosts.length" value="">暂无可用推理机</option>
          <option v-for="host in hosts" :key="host.id" :value="host.id">{{ host.name }}</option>
        </select>
      </label>
    </div>
    <div class="compression__status" role="status" aria-live="polite">
      <strong>{{ label }}</strong>
      <p v-if="pending">
        {{
          command?.status === 'pending'
            ? '请求已入队，等待推理机领取。'
            : '推理机已领取，等待实际进程确认。'
        }}
      </p>
      <p v-if="observedAt">来源机观测：{{ observedAt }}（上次查询结果）</p>
      <p v-if="observed?.segment">当前分段：{{ observed.segment }}</p>
      <p v-if="observed">
        输入读取速率：{{
          observed.readrate == null ? '未限制' : `${observed.readrate} 倍实时速率`
        }}（本机配置）
      </p>
      <p v-if="observed?.detail">{{ observed.detail }}</p>
      <p v-if="command?.failure_code">操作失败：{{ command.failure_code }}。</p>
    </div>
    <div class="compression__actions">
      <ElButton v-if="pending" :disabled="loading" @click="query">查询命令结果</ElButton>
      <template v-if="mayEdit">
        <ElButton :disabled="loading || pending || !hostId" @click="submit('inspect')"
          >刷新实际状态</ElButton
        >
        <ElButton :disabled="loading || pending || !hostId" @click="submit('pause')"
          >暂停压缩</ElButton
        >
        <ElButton :disabled="loading || pending || !hostId" @click="submit('resume')"
          >恢复压缩</ElButton
        >
        <ElButton
          type="danger"
          plain
          :disabled="loading || pending || !hostId"
          @click="submit('abort')"
          >中止压缩</ElButton
        >
      </template>
    </div>
    <p v-if="failure" class="compression__failure" role="alert">{{ failure }}</p>
  </section>
</template>

<style scoped>
.compression {
  margin-block: 1.5rem;
  padding: 1.5rem;
  border: 1px solid #dce3eb;
  border-radius: 12px;
  background: #fff;
}
.compression__header {
  display: flex;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 1rem;
}
.compression__eyebrow {
  color: #52657a;
  font-size: 0.75rem;
  letter-spacing: 0.08em;
}
h2 {
  margin-block: 0.25rem 0.75rem;
  font-size: 1.15rem;
}
p {
  color: #52657a;
  line-height: 1.6;
}
.compression__host {
  display: grid;
  align-content: start;
  gap: 0.5rem;
  font-size: 0.875rem;
}
select {
  min-width: 13rem;
  padding: 0.6rem;
  border: 1px solid #b8c6d6;
  border-radius: 6px;
  background: #fff;
}
.compression__status {
  margin-block: 1rem;
  padding: 1rem;
  border-radius: 8px;
  background: #f3f6fa;
}
.compression__status p {
  margin-block: 0.35rem 0;
  overflow-wrap: anywhere;
}
.compression__actions {
  display: flex;
  flex-wrap: wrap;
  gap: 0.5rem;
}
.compression__actions .el-button {
  margin-left: 0;
}
.compression__failure {
  color: #b42318;
}
</style>
