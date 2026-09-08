<script setup lang="ts">
import {
  ElButton,
  ElDialog,
  ElForm,
  ElFormItem,
  ElInput,
  ElMessage,
  ElOption,
  ElSelect,
  ElTag,
} from 'element-plus'
import { computed, onMounted, reactive, ref } from 'vue'

import {
  ControlPlaneError,
  downloadTemplateImport,
  editTemplateDraft,
  importTemplateDraft,
  readTemplateDrafts,
  readTemplateImports,
  type FieldError,
  type TemplateDraftConfiguration,
  type TemplateDraftView,
  type TemplateImportView,
} from '@/api/controlPlane'
import { useSessionStore } from '@/session/store'

interface EditorStep {
  number: number
  name: string
  description: string
}

interface EditorForm {
  id: string
  revision: string
  steps: EditorStep[]
  ordering: string
  idle_timeout_seconds: string
  step_deadline_seconds: string
  disposition_policy: string
}

const session = useSessionStore()
const drafts = ref<TemplateDraftView[]>([])
const imports = ref<TemplateImportView[]>([])
const loading = ref(true)
const failure = ref('')
const fieldErrors = ref<FieldError[]>([])
const selectedFile = ref('')

const mayView = computed(() => session.may('template.draft.view'))
const mayEdit = computed(() => session.may('template.draft.edit'))
const knownDraftId = ref('')
const knownRevision = ref('')

const editDialog = ref(false)
const editor = reactive<EditorForm>(newEditor())

function newEditor(): EditorForm {
  return {
    id: '',
    revision: '',
    steps: [{ number: 1, name: '', description: '(1)' }],
    ordering: 'strict',
    idle_timeout_seconds: '',
    step_deadline_seconds: '',
    disposition_policy: '',
  }
}

function resetFailure(): void {
  failure.value = ''
  fieldErrors.value = []
}

function recordFailure(error: unknown): void {
  if (error instanceof ControlPlaneError) {
    failure.value = error.detail ?? error.message
    fieldErrors.value = error.fieldErrors
    return
  }
  failure.value = '请求未能完成，请稍后重试'
  fieldErrors.value = []
}

async function load(): Promise<void> {
  loading.value = true
  resetFailure()
  if (!mayView.value) {
    loading.value = false
    return
  }
  try {
    const [draftPage, importPage] = await Promise.all([readTemplateDrafts(), readTemplateImports()])
    drafts.value = draftPage.items
    imports.value = importPage.items
  } catch (error) {
    recordFailure(error)
  } finally {
    loading.value = false
  }
}

async function downloadImport(record: TemplateImportView): Promise<void> {
  resetFailure()
  try {
    const workbook = await downloadTemplateImport(record.id)
    const url = URL.createObjectURL(workbook)
    const anchor = window.document.createElement('a')
    anchor.href = url
    anchor.download = record.filename
    anchor.click()
    URL.revokeObjectURL(url)
  } catch (error) {
    recordFailure(error)
  }
}

async function importFile(event: Event): Promise<void> {
  resetFailure()
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  selectedFile.value = file?.name ?? ''
  if (!file) {
    return
  }
  try {
    await importTemplateDraft(file, file.name)
    ElMessage.success('模板已导入为草稿')
    await load()
  } catch (error) {
    recordFailure(error)
  } finally {
    input.value = ''
  }
}

function orderingLabel(ordering: string): string {
  switch (ordering) {
    case 'strict':
      return '严格顺序'
    case 'unordered':
      return '不要求顺序'
    default:
      return `未知顺序声明（${ordering}）`
  }
}

function orderingTag(ordering: string): 'success' | 'warning' {
  return ordering === 'strict' || ordering === 'unordered' ? 'success' : 'warning'
}

function importStatusLabel(status: string): string {
  switch (status) {
    case 'succeeded':
      return '已生成草稿'
    case 'failed':
      return '校验失败'
    default:
      return `未知导入状态（${status}）`
  }
}

function importStatusTag(status: string): 'success' | 'danger' | 'warning' {
  if (status === 'succeeded') {
    return 'success'
  }
  return status === 'failed' ? 'danger' : 'warning'
}

function canEditDraft(draft: TemplateDraftView): boolean {
  return mayEdit.value && (draft.ordering === 'strict' || draft.ordering === 'unordered')
}

function openEditor(draft: TemplateDraftView): void {
  resetFailure()
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
  })
  editDialog.value = true
}

function openKnownEditor(): void {
  resetFailure()
  Object.assign(editor, newEditor(), {
    id: knownDraftId.value.trim(),
    revision: knownRevision.value.trim(),
  })
  editDialog.value = true
}

function addStep(): void {
  const number = editor.steps.length + 1
  editor.steps.push({ number, name: '', description: `(${number})` })
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

function duration(value: string, label: string): number | null {
  const clean = value.trim()
  if (clean === '') {
    return null
  }
  const parsed = Number(clean)
  if (!Number.isFinite(parsed) || parsed <= 0) {
    failure.value = `${label}必须是大于 0 的数字`
    return null
  }
  return parsed
}

function configurationFromEditor(): TemplateDraftConfiguration | null {
  if (editor.ordering !== 'strict' && editor.ordering !== 'unordered') {
    failure.value = `未知顺序声明（${editor.ordering}），不能编辑`
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
  return {
    steps: editor.steps.map((step) => ({ ...step })),
    ordering: editor.ordering,
    runtime_defaults: {
      idle_timeout_seconds: idle,
      step_deadline_seconds: deadline,
      disposition_policy: editor.disposition_policy.trim() || null,
    },
  }
}

async function submitEditor(): Promise<void> {
  resetFailure()
  const configuration = configurationFromEditor()
  if (configuration === null) {
    return
  }
  const revision = Number(editor.revision)
  if (!editor.id.trim() || !Number.isInteger(revision) || revision < 1) {
    failure.value = '草稿 ID 和修订号必须填写正确'
    return
  }
  try {
    await editTemplateDraft(editor.id.trim(), configuration, revision)
    editDialog.value = false
    ElMessage.success('模板草稿已保存')
    await load()
  } catch (error) {
    recordFailure(error)
  }
}

onMounted(load)
</script>

<template>
  <section aria-labelledby="templates-heading" class="template-management">
    <header class="template-management__header">
      <div>
        <p class="template-management__eyebrow">配置中心 / SOP</p>
        <h1 id="templates-heading" class="template-management__heading">SOP 模板</h1>
        <p class="template-management__intro">
          把规范化 Excel 留作凭据，把可运行内容留在草稿；发布版本将在后续流程中产生。
        </p>
      </div>
      <div v-if="mayEdit" class="template-management__import-rail">
        <label for="template-file" class="template-management__file-label">
          导入规范化 Excel
          <input
            id="template-file"
            name="template-file"
            type="file"
            accept=".xlsx,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            @change="importFile"
          />
        </label>
        <span v-if="selectedFile" class="template-management__file-name">{{ selectedFile }}</span>
        <small>失败文件也会保留，便于定位工作表、行和字段。</small>
      </div>
    </header>

    <p v-if="failure" class="template-management__failure" role="alert">{{ failure }}</p>
    <ul v-if="fieldErrors.length" class="template-management__errors" aria-label="导入字段错误">
      <li v-for="error in fieldErrors" :key="`${error.field}:${error.message}`">
        {{ error.field }}：{{ error.message }}
      </li>
    </ul>

    <div
      v-if="!mayView && mayEdit"
      class="template-management__direct"
      aria-labelledby="known-draft-heading"
    >
      <h2 id="known-draft-heading">按标识编辑草稿</h2>
      <p>当前没有模板查看权限。不会读取草稿或导入记录，请使用已知 ID 和修订号。</p>
      <div class="template-management__direct-fields">
        <ElFormItem label="草稿 ID">
          <ElInput v-model="knownDraftId" name="known-draft-id" autocomplete="off" />
        </ElFormItem>
        <ElFormItem label="已知修订号">
          <ElInput
            v-model="knownRevision"
            name="known-draft-revision"
            type="number"
            inputmode="numeric"
            autocomplete="off"
          />
        </ElFormItem>
      </div>
      <ElButton type="primary" @click="openKnownEditor">按标识编辑</ElButton>
    </div>

    <div v-else-if="mayView" class="template-management__workbench">
      <section class="template-management__drafts" aria-labelledby="drafts-heading">
        <div class="template-management__section-head">
          <div>
            <h2 id="drafts-heading">可编辑草稿</h2>
            <p>每次导入都会生成新草稿，不覆盖已有内容。</p>
          </div>
          <span class="template-management__count">{{ drafts.length }} 个草稿</span>
        </div>
        <p v-if="loading" class="template-management__loading">正在加载模板…</p>
        <table v-else class="template-management__table">
          <caption class="template-management__caption">
            尚未发布的 SOP 草稿
          </caption>
          <thead>
            <tr>
              <th scope="col">工位</th>
              <th scope="col">步骤</th>
              <th scope="col">顺序声明</th>
              <th scope="col">修订号</th>
              <th scope="col">操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="draft in drafts" :key="draft.id">
              <th scope="row">
                <span>{{ draft.station_name }}</span>
                <small>{{ draft.station_code }}</small>
              </th>
              <td>{{ draft.steps.length }} 个步骤</td>
              <td>
                <ElTag :type="orderingTag(draft.ordering)" disable-transitions>
                  {{ orderingLabel(draft.ordering) }}
                </ElTag>
              </td>
              <td>{{ draft.revision }}</td>
              <td>
                <ElButton v-if="canEditDraft(draft)" link type="primary" @click="openEditor(draft)">
                  编辑草稿
                </ElButton>
                <span v-else class="template-management__muted">
                  {{ mayEdit ? '未知值，不能编辑' : '只读' }}
                </span>
              </td>
            </tr>
            <tr v-if="drafts.length === 0">
              <td colspan="5" class="template-management__empty">
                还没有草稿，从右上角导入第一份。
              </td>
            </tr>
          </tbody>
        </table>
      </section>

      <section class="template-management__history" aria-labelledby="imports-heading">
        <div class="template-management__section-head">
          <div>
            <h2 id="imports-heading">导入记录</h2>
            <p>原始文件只留在控制面，不出现在草稿表格里。</p>
          </div>
        </div>
        <table class="template-management__table template-management__table--history">
          <caption class="template-management__caption">
            成功和失败的规范化 Excel 导入
          </caption>
          <thead>
            <tr>
              <th scope="col">文件</th>
              <th scope="col">结果</th>
              <th scope="col">错误</th>
              <th scope="col">操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="record in imports" :key="record.id">
              <th scope="row">{{ record.filename }}</th>
              <td>
                <ElTag :type="importStatusTag(record.status)" disable-transitions>
                  {{ importStatusLabel(record.status) }}
                </ElTag>
              </td>
              <td>
                <ul v-if="record.errors.length" class="template-management__record-errors">
                  <li v-for="error in record.errors" :key="`${error.field}:${error.message}`">
                    {{ error.sheet }}<span v-if="error.row !== null">[{{ error.row }}]</span>.
                    {{ error.field }}：{{ error.message }}
                  </li>
                </ul>
                <span v-else class="template-management__muted">无</span>
              </td>
              <td>
                <ElButton link type="primary" @click="downloadImport(record)">
                  下载原文件
                </ElButton>
              </td>
            </tr>
            <tr v-if="imports.length === 0">
              <td colspan="4" class="template-management__empty">还没有导入记录。</td>
            </tr>
          </tbody>
        </table>
      </section>
    </div>

    <p v-else class="template-management__empty-state">当前没有模板查看权限。</p>

    <ElDialog v-model="editDialog" title="编辑模板草稿" width="48rem">
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
        <ElButton @click="editDialog = false">取消</ElButton>
        <ElButton type="primary" @click="submitEditor">保存草稿</ElButton>
      </template>
    </ElDialog>
  </section>
</template>

<style scoped>
.template-management {
  --template-ink: #17222f;
  --template-paper: #f7f4ec;
  --template-amber: #d6a13d;
  --template-teal: #4d8b84;
  --template-rule: #d9d4c7;
  color: var(--template-ink);
}

.template-management__header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 2rem;
  margin-bottom: 1.5rem;
}

.template-management__eyebrow {
  margin: 0 0 0.5rem;
  color: var(--template-teal);
  font-size: 0.8rem;
}

.template-management__heading {
  margin: 0 0 0.5rem;
  font-family: Georgia, 'Times New Roman', serif;
  font-size: 1.8rem;
  font-weight: 600;
  letter-spacing: -0.03em;
}

.template-management__intro {
  max-width: 38rem;
  margin: 0;
  color: #5f6872;
  line-height: 1.6;
}

.template-management__import-rail {
  display: grid;
  gap: 0.55rem;
  min-width: 17rem;
  padding: 1rem 1.15rem;
  border-left: 4px solid var(--template-amber);
  background: var(--template-paper);
}

.template-management__import-rail label {
  font-weight: 600;
}

.template-management__import-rail input {
  max-width: 15rem;
  font: inherit;
}

.template-management__import-rail small,
.template-management__file-name {
  color: #5f6872;
  line-height: 1.45;
}

.template-management__file-name {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.template-management__failure {
  margin: 0 0 0.8rem;
  padding: 0.7rem 0.9rem;
  border-left: 3px solid var(--el-color-danger);
  background: var(--el-color-danger-light-9);
  color: var(--el-color-danger);
}

.template-management__errors {
  margin: 0 0 1rem;
  padding: 0.65rem 0.9rem 0.65rem 2rem;
  border: 1px solid #e6caca;
  background: #fff8f8;
  color: var(--el-color-danger);
}

.template-management__workbench {
  display: grid;
  gap: 1.5rem;
}

.template-management__drafts,
.template-management__history,
.template-management__direct {
  border-top: 3px solid var(--template-ink);
  background: #fff;
}

.template-management__history {
  border-top-color: var(--template-teal);
}

.template-management__section-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 1rem;
  padding: 1rem 1.25rem;
  background: var(--template-paper);
}

.template-management__section-head h2,
.template-management__direct h2 {
  margin: 0 0 0.25rem;
  font-size: 1.05rem;
}

.template-management__section-head p,
.template-management__direct p,
.template-management__loading {
  margin: 0;
  color: #5f6872;
}

.template-management__count,
.template-management__muted {
  color: #68717c;
  font-size: 0.85rem;
}

.template-management__table {
  width: 100%;
  border-collapse: collapse;
}

.template-management__table th,
.template-management__table td {
  padding: 0.7rem 1.25rem;
  border-bottom: 1px solid var(--template-rule);
  text-align: left;
  vertical-align: middle;
}

.template-management__table th[scope='row'] {
  font-weight: 600;
}

.template-management__table th[scope='row'] small {
  display: block;
  margin-top: 0.2rem;
  color: #68717c;
  font-weight: 400;
}

.template-management__caption {
  padding: 0.75rem 1.25rem 0;
  color: #68717c;
  text-align: left;
}

.template-management__empty,
.template-management__empty-state {
  padding: 1.25rem;
  color: #68717c;
}

.template-management__record-errors {
  margin: 0;
  padding-left: 1.25rem;
  line-height: 1.5;
}

.template-management__direct {
  padding: 1.25rem;
}

.template-management__direct-fields,
.template-management__edit-meta {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 1rem;
  margin: 1.25rem 0;
}

.template-management__direct-fields {
  grid-template-columns: repeat(2, minmax(0, 1fr));
  max-width: 38rem;
}

.template-management__steps {
  margin: 0 0 1.25rem;
  padding: 1rem;
  border: 1px solid var(--template-rule);
}

.template-management__steps legend {
  padding: 0 0.4rem;
  font-weight: 600;
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
  background: var(--template-amber);
  color: var(--template-ink);
  font-weight: 700;
}

.template-management__select {
  width: 100%;
}

.template-management :focus-visible {
  outline: 2px solid var(--template-teal);
  outline-offset: 2px;
}

@media (max-width: 780px) {
  .template-management__header {
    display: block;
  }

  .template-management__import-rail {
    margin-top: 1.25rem;
  }

  .template-management__table {
    min-width: 46rem;
  }

  .template-management__direct-fields,
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
}
</style>
