<script setup lang="ts">
import { ElButton, ElDialog, ElForm, ElFormItem, ElInput, ElOption, ElSelect } from 'element-plus'
import { computed, reactive, watch } from 'vue'

import {
  editTemplateDraft,
  type TemplateDraftConfiguration,
  type TemplateDraftView,
} from '@/api/controlPlane'
import type { DraftEditorTarget } from './TemplateDraftEditor.types'

interface EditorStep {
  number: number
  name: string
  description: string
}

interface EditorSignal {
  kind: 'action' | 'external'
  value: string
}

interface EditorForm {
  id: string
  revision: string
  steps: EditorStep[]
  ordering: string
  idle_timeout_seconds: string
  step_deadline_seconds: string
  disposition_policy: string
  start_signal_declared: boolean
  start_signal_kind: EditorSignal['kind']
  start_signal_value: string
  end_signals_declared: boolean
  end_signals: EditorSignal[]
  boundary_dirty: boolean
}

const props = defineProps<{
  modelValue: boolean
  target: DraftEditorTarget | null
}>()

const emit = defineEmits<{
  'update:modelValue': [value: boolean]
  saved: []
  failure: [error: unknown]
}>()

const editor = reactive<EditorForm>(newEditor())
const visible = computed({
  get: () => props.modelValue,
  set: (value: boolean) => emit('update:modelValue', value),
})

function newEditor(): EditorForm {
  return {
    id: '',
    revision: '',
    steps: [{ number: 1, name: '', description: '(1)' }],
    ordering: 'strict',
    idle_timeout_seconds: '',
    step_deadline_seconds: '',
    disposition_policy: '',
    start_signal_declared: false,
    start_signal_kind: 'action',
    start_signal_value: '1',
    end_signals_declared: false,
    end_signals: [],
    boundary_dirty: false,
  }
}

function signalForEditor(signal: TemplateDraftView['start_signal']): EditorSignal {
  if (signal === null || signal === undefined) {
    return { kind: 'action', value: '1' }
  }
  if (signal.kind === 'action') {
    return {
      kind: 'action',
      value: signal.action_number === undefined ? '1' : String(signal.action_number),
    }
  }
  if (signal?.kind === 'external') {
    return { kind: 'external', value: signal.semantic_label }
  }
  throw new Error('未知边界信号种类')
}

function loadTarget(target: DraftEditorTarget | null): void {
  if (target === null) {
    return
  }
  if (target.draft === null) {
    Object.assign(editor, newEditor(), { id: target.id, revision: target.revision })
    return
  }
  const draft = target.draft
  const endSignals = draft.end_signals ?? []
  const startSignal = signalForEditor(draft.start_signal)
  Object.assign(editor, {
    id: draft.id,
    revision: String(draft.revision),
    steps: draft.steps.map((step) => ({ ...step })),
    ordering: draft.ordering,
    idle_timeout_seconds:
      draft.runtime_defaults.idle_timeout_seconds === null
        ? ''
        : String(draft.runtime_defaults.idle_timeout_seconds),
    step_deadline_seconds:
      draft.runtime_defaults.step_deadline_seconds === null
        ? ''
        : String(draft.runtime_defaults.step_deadline_seconds),
    disposition_policy: draft.runtime_defaults.disposition_policy ?? '',
    start_signal_declared: draft.start_signal !== undefined && draft.start_signal !== null,
    start_signal_kind: startSignal.kind,
    start_signal_value: startSignal.value,
    end_signals_declared: draft.end_signals !== undefined && draft.end_signals !== null,
    end_signals: endSignals.map(signalForEditor),
    boundary_dirty: false,
  })
}

watch(() => props.target, loadTarget, { immediate: true })

function addStep(): void {
  const number = editor.steps.length + 1
  editor.steps.push({ number, name: '', description: `(${number})` })
}

function addEndSignal(): void {
  editor.end_signals.push({ kind: 'action', value: '1' })
  editor.end_signals_declared = true
  markBoundaryDirty()
}

function removeEndSignal(index: number): void {
  editor.end_signals.splice(index, 1)
  markBoundaryDirty()
}

function removeStep(index: number): void {
  if (editor.steps.length <= 1) {
    return
  }
  editor.steps.splice(index, 1)
  editor.steps.forEach((step, position) => {
    step.number = position + 1
    step.description = `(${step.number})${step.name}`
  })
}

function syncDescription(index: number): void {
  const step = editor.steps[index]
  if (step !== undefined) {
    step.description = `(${step.number})${step.name}`
  }
}

function markBoundaryDirty(): void {
  editor.boundary_dirty = true
}

function signalFromEditor(
  signal: EditorSignal,
  label: string,
): { kind: 'action'; action_number: number } | { kind: 'external'; semantic_label: string } | null {
  if (signal.kind === 'action') {
    const actionNumber = Number(signal.value.trim())
    if (!Number.isInteger(actionNumber) || actionNumber <= 0) {
      emit('failure', new Error(`${label}必须是大于 0 的整数`))
      return null
    }
    return { kind: 'action', action_number: actionNumber }
  }
  if (signal.kind !== 'external') {
    emit('failure', new Error(`${label}包含未知信号种类`))
    return null
  }
  const semanticLabel = signal.value.trim()
  if (!semanticLabel) {
    emit('failure', new Error(`${label}不能为空`))
    return null
  }
  return { kind: 'external', semantic_label: semanticLabel }
}

function duration(value: string, label: string): number | null {
  const clean = value.trim()
  if (clean === '') {
    return null
  }
  const parsed = Number(clean)
  if (!Number.isFinite(parsed) || parsed <= 0) {
    emit('failure', new Error(`${label}必须是大于 0 的数字`))
    return null
  }
  return parsed
}

function configurationFromEditor(): TemplateDraftConfiguration | null {
  if (editor.ordering !== 'strict' && editor.ordering !== 'unordered') {
    emit('failure', new Error(`未知顺序声明（${editor.ordering}），不能编辑`))
    return null
  }
  const idle = duration(editor.idle_timeout_seconds, '空闲时限')
  if (editor.idle_timeout_seconds.trim() !== '' && idle === null) {
    return null
  }
  const deadline = duration(editor.step_deadline_seconds, '步骤时限')
  if (editor.step_deadline_seconds.trim() !== '' && deadline === null) {
    return null
  }
  const configuration: TemplateDraftConfiguration = {
    steps: editor.steps.map((step) => ({ ...step })),
    ordering: editor.ordering,
    runtime_defaults: {
      idle_timeout_seconds: idle,
      step_deadline_seconds: deadline,
      disposition_policy: editor.disposition_policy.trim() || null,
    },
  }
  if (!editor.boundary_dirty) {
    return configuration
  }
  if (editor.start_signal_declared) {
    const startSignal = signalFromEditor(
      { kind: editor.start_signal_kind, value: editor.start_signal_value },
      '开始信号',
    )
    if (startSignal === null) {
      return null
    }
    configuration.start_signal = startSignal
  } else {
    configuration.start_signal = null
  }
  if (editor.end_signals_declared) {
    const endSignals: Array<NonNullable<ReturnType<typeof signalFromEditor>>> = []
    for (const [index, signal] of editor.end_signals.entries()) {
      const converted = signalFromEditor(signal, `结束信号 ${index + 1}`)
      if (converted === null) {
        return null
      }
      endSignals.push(converted)
    }
    configuration.end_signals = endSignals
  } else {
    configuration.end_signals = null
  }
  return configuration
}

async function submitEditor(): Promise<void> {
  const configuration = configurationFromEditor()
  if (configuration === null) {
    return
  }
  const revision = Number(editor.revision)
  if (!editor.id.trim() || !Number.isInteger(revision) || revision < 1) {
    emit('failure', new Error('草稿 ID 和修订号必须填写正确'))
    return
  }
  try {
    await editTemplateDraft(editor.id.trim(), configuration, revision)
    visible.value = false
    emit('saved')
  } catch (error) {
    emit('failure', error)
  }
}
</script>

<template>
  <ElDialog v-model="visible" title="编辑模板草稿" width="48rem">
    <ElForm
      class="template-management__edit-form"
      label-position="top"
      @submit.prevent="submitEditor"
    >
      <div class="template-management__edit-meta">
        <ElFormItem label="草稿 ID">
          <ElInput v-model="editor.id" name="draft-id" autocomplete="off" />
        </ElFormItem>
        <ElFormItem label="修订号">
          <ElInput v-model="editor.revision" name="draft-revision" type="number" />
        </ElFormItem>
        <ElFormItem label="顺序声明">
          <ElSelect v-model="editor.ordering" name="ordering" class="template-management__select">
            <ElOption label="严格顺序" value="strict" />
            <ElOption label="不要求顺序" value="unordered" />
          </ElSelect>
        </ElFormItem>
      </div>

      <fieldset class="template-management__steps">
        <legend>步骤内容</legend>
        <div
          v-for="(step, index) in editor.steps"
          :key="index"
          class="template-management__step-row"
        >
          <span class="template-management__step-number">{{ step.number }}</span>
          <ElInput
            v-model="step.name"
            :name="`step-name-${index}`"
            aria-label="步骤名称"
            placeholder="步骤名称"
            @input="syncDescription(index)"
          />
          <ElInput
            v-model="step.description"
            :name="`step-description-${index}`"
            aria-label="步骤描述"
            placeholder="(步骤号)描述"
          />
          <ElButton
            link
            type="danger"
            :disabled="editor.steps.length <= 1"
            @click="removeStep(index)"
          >
            移除
          </ElButton>
        </div>
        <ElButton link type="primary" @click="addStep">添加步骤</ElButton>
      </fieldset>

      <fieldset class="template-management__boundary">
        <legend>实例边界</legend>
        <label for="start-signal-declared" class="template-management__check">
          <input
            id="start-signal-declared"
            v-model="editor.start_signal_declared"
            name="start-signal-declared"
            type="checkbox"
            @change="markBoundaryDirty"
          />
          声明开始信号
        </label>
        <p class="template-management__boundary-note">
          仅使用动作号作为开始信号时，无法确认起始动作是否漏记；如果现场需要确认作业起点，请使用已配置的外部信号。
        </p>
        <div v-if="editor.start_signal_declared" class="template-management__signal-row">
          <label for="start-signal-kind">
            <span>类型</span>
            <select
              id="start-signal-kind"
              v-model="editor.start_signal_kind"
              name="start-signal-kind"
              @change="markBoundaryDirty"
            >
              <option value="action">动作号</option>
              <option value="external">外部信号</option>
            </select>
          </label>
          <label for="start-signal-value">
            <span>{{ editor.start_signal_kind === 'action' ? '动作号' : '语义标签' }}</span>
            <input
              id="start-signal-value"
              v-model="editor.start_signal_value"
              name="start-signal-value"
              type="text"
              @input="markBoundaryDirty"
            />
          </label>
        </div>

        <label for="end-signals-declared" class="template-management__check">
          <input
            id="end-signals-declared"
            v-model="editor.end_signals_declared"
            name="end-signals-declared"
            type="checkbox"
            @change="markBoundaryDirty"
          />
          声明结束信号列表
        </label>
        <div v-if="editor.end_signals_declared" class="template-management__end-signals">
          <p v-if="editor.end_signals.length === 0" class="template-management__muted">
            已明确声明为空，将由空闲时限闭合实例。
          </p>
          <div
            v-for="(signal, index) in editor.end_signals"
            :key="index"
            class="template-management__signal-row"
          >
            <label :for="`end-signal-kind-${index}`">
              <span>类型</span>
              <select
                :id="`end-signal-kind-${index}`"
                v-model="signal.kind"
                @change="markBoundaryDirty"
              >
                <option value="action">动作号</option>
                <option value="external">外部信号</option>
              </select>
            </label>
            <label :for="`end-signal-value-${index}`">
              <span>{{ signal.kind === 'action' ? '动作号' : '语义标签' }}</span>
              <input
                :id="`end-signal-value-${index}`"
                v-model="signal.value"
                type="text"
                @input="markBoundaryDirty"
              />
            </label>
            <ElButton link type="danger" @click="removeEndSignal(index)">移除</ElButton>
          </div>
          <ElButton link type="primary" @click="addEndSignal">添加结束信号</ElButton>
        </div>
      </fieldset>

      <div class="template-management__edit-meta">
        <ElFormItem label="空闲时限（秒）">
          <ElInput
            v-model="editor.idle_timeout_seconds"
            name="idle-timeout-seconds"
            type="number"
          />
        </ElFormItem>
        <ElFormItem label="步骤时限（秒）">
          <ElInput
            v-model="editor.step_deadline_seconds"
            name="step-deadline-seconds"
            type="number"
          />
        </ElFormItem>
        <ElFormItem label="处置策略">
          <ElInput v-model="editor.disposition_policy" name="disposition-policy" />
        </ElFormItem>
      </div>
    </ElForm>
    <template #footer>
      <ElButton @click="visible = false">取消</ElButton>
      <ElButton type="primary" @click="submitEditor">保存草稿</ElButton>
    </template>
  </ElDialog>
</template>

<style scoped>
.template-management :focus-visible {
  outline: 2px solid #4d8b84;
  outline-offset: 2px;
}

.template-management__edit-meta {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 1rem;
  margin: 1.25rem 0;
}

.template-management__steps,
.template-management__boundary {
  margin: 0 0 1.25rem;
  padding: 1rem;
  border: 1px solid #d9d4c7;
}

.template-management__step-row {
  display: grid;
  grid-template-columns: 2rem minmax(8rem, 1fr) minmax(12rem, 1.5fr) auto;
  align-items: center;
  gap: 0.65rem;
  margin-bottom: 0.75rem;
}

.template-management__step-number {
  display: grid;
  place-items: center;
  width: 1.8rem;
  height: 1.8rem;
  border-radius: 50%;
  background: #d6a13d;
  color: #17222f;
  font-weight: 700;
}

.template-management__boundary {
  display: grid;
  gap: 0.85rem;
  background: #fbfaf6;
}

.template-management__boundary-note {
  margin: 0;
  color: #68717c;
  font-size: 0.85rem;
  line-height: 1.5;
}

.template-management__check {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  font-weight: 600;
}

.template-management__signal-row {
  display: grid;
  grid-template-columns: minmax(8rem, 0.7fr) minmax(12rem, 1.5fr) auto;
  align-items: end;
  gap: 0.75rem;
  padding-left: 1.5rem;
}

.template-management__signal-row label {
  display: grid;
  gap: 0.3rem;
  color: #68717c;
  font-size: 0.82rem;
}

.template-management__signal-row input,
.template-management__signal-row select {
  min-height: 2.15rem;
  padding: 0.35rem 0.5rem;
  border: 1px solid #b9b5aa;
  border-radius: 0;
  background: #fff;
  color: #17222f;
  font: inherit;
}

.template-management__end-signals {
  display: grid;
  gap: 0.75rem;
  padding-left: 1.5rem;
}

@media (max-width: 780px) {
  .template-management__edit-meta {
    grid-template-columns: 1fr;
  }

  .template-management__step-row {
    grid-template-columns: 2rem 1fr;
  }

  .template-management__step-row .el-input:nth-child(3),
  .template-management__step-row .el-button {
    grid-column: 2;
  }

  .template-management__signal-row {
    grid-template-columns: 1fr;
    padding-left: 0;
  }

  .template-management__end-signals {
    padding-left: 0;
  }
}
</style>
