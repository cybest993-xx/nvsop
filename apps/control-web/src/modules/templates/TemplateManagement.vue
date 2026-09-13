<script setup lang="ts">
import { ElButton, ElDialog, ElFormItem, ElInput, ElMessage, ElTag } from 'element-plus'
import { computed, onMounted, ref } from 'vue'

import {
  ControlPlaneError,
  downloadTemplateImport,
  downloadTemplateVersionArtifact,
  importTemplateDraft,
  publishTemplateVersion,
  readTemplateDrafts,
  readTemplateImports,
  readTemplateVersions,
  type FieldError,
  type TemplateArtifactName,
  type TemplateDraftView,
  type TemplateImportView,
  type TemplateVersionView,
} from '@/api/controlPlane'
import { useSessionStore } from '@/session/store'

import TemplateDraftEditor from './TemplateDraftEditor.vue'
import type { DraftEditorTarget } from './TemplateDraftEditor.types'

const session = useSessionStore()
const drafts = ref<TemplateDraftView[]>([])
const imports = ref<TemplateImportView[]>([])
const versions = ref<TemplateVersionView[]>([])
const loading = ref(true)
const downloadingArtifact = ref('')
const failure = ref('')
const fieldErrors = ref<FieldError[]>([])
const selectedFile = ref('')

const mayView = computed(() => session.may('template.draft.view'))
const mayEdit = computed(() => session.may('template.draft.edit'))
const knownDraftId = ref('')
const knownRevision = ref('')
const editorVisible = ref(false)
const editorTarget = ref<DraftEditorTarget | null>(null)
const publishingDraftId = ref('')
const publishConfirmationVisible = ref(false)
const publishTarget = ref<TemplateDraftView | null>(null)
const versionDetailVisible = ref(false)
const detailVersion = ref<TemplateVersionView | null>(null)

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
  if (error instanceof Error) {
    failure.value = error.message
    fieldErrors.value = []
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
    const [draftPage, importPage, versionPageResult] = await Promise.all([
      readTemplateDrafts(),
      readTemplateImports(),
      readTemplateVersions(),
    ])
    drafts.value = draftPage.items
    imports.value = importPage.items
    versions.value = versionPageResult.items
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
    window.setTimeout(() => URL.revokeObjectURL(url), 0)
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

function requestPublish(draft: TemplateDraftView): void {
  if (publishingDraftId.value !== '') {
    return
  }
  resetFailure()
  publishTarget.value = draft
  publishConfirmationVisible.value = true
}

function cancelPublish(): void {
  publishConfirmationVisible.value = false
  publishTarget.value = null
}

async function confirmPublish(): Promise<void> {
  const draft = publishTarget.value
  if (draft === null || publishingDraftId.value !== '') {
    return
  }
  publishingDraftId.value = draft.id
  try {
    await publishTemplateVersion(draft.id, draft.revision)
    ElMessage.success('模板版本已发布')
    cancelPublish()
    await load()
  } catch (error) {
    if (error instanceof ControlPlaneError && error.status === 409) {
      await handleRevisionConflict()
    } else {
      recordFailure(error)
      cancelPublish()
    }
  } finally {
    publishingDraftId.value = ''
  }
}

async function handleRevisionConflict(): Promise<void> {
  editorVisible.value = false
  editorTarget.value = null
  cancelPublish()
  if (mayView.value) {
    await load()
    failure.value = '草稿修订已变化，列表已刷新；请重新打开并核对后重试。'
    fieldErrors.value = []
    return
  }
  resetFailure()
  failure.value = '草稿修订已变化，请重新核对后重试。'
}

function openVersionDetail(version: TemplateVersionView): void {
  resetFailure()
  detailVersion.value = version
  versionDetailVisible.value = true
}

async function downloadVersionArtifact(
  version: TemplateVersionView,
  name: TemplateArtifactName,
): Promise<void> {
  resetFailure()
  downloadingArtifact.value = `${version.id}:${name}`
  try {
    const artifact = await downloadTemplateVersionArtifact(version.id, name)
    const url = URL.createObjectURL(artifact)
    const anchor = window.document.createElement('a')
    anchor.href = url
    anchor.download = `${version.id}-${name}`
    anchor.click()
    window.setTimeout(() => URL.revokeObjectURL(url), 0)
  } catch (error) {
    recordFailure(error)
  } finally {
    downloadingArtifact.value = ''
  }
}

function signalLabel(signal: unknown): string {
  if (signal === null || signal === undefined) {
    return '未声明'
  }
  if (typeof signal !== 'object') {
    return `未知边界信号（${displayUnknown(signal)}）`
  }
  const candidate = signal as {
    kind?: unknown
    action_number?: unknown
    semantic_label?: unknown
  }
  if (candidate.kind === 'action') {
    return candidate.action_number === null || candidate.action_number === undefined
      ? '动作（未知编号）'
      : `动作 ${displayUnknown(candidate.action_number)}`
  }
  if (candidate.kind === 'external') {
    return candidate.semantic_label === null ||
      candidate.semantic_label === undefined ||
      (typeof candidate.semantic_label === 'string' && candidate.semantic_label.trim() === '')
      ? '外部：未知标签'
      : `外部：${displayUnknown(candidate.semantic_label)}`
  }
  return `未知边界信号（${displayUnknown(candidate.kind)}）`
}

function signalListLabel(signals: TemplateDraftView['end_signals']): string {
  if (signals === null || signals === undefined) {
    return '未声明'
  }
  return signals.length === 0 ? '无（空闲时限闭合）' : signals.map(signalLabel).join('、')
}

function displayUnknown(value: unknown): string {
  return typeof value === 'object' && value !== null
    ? (JSON.stringify(value) ?? '未知值')
    : String(value)
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
  const signals = [draft.start_signal, ...(draft.end_signals ?? [])]
  const hasSupportedSignals = signals.every(
    (signal) =>
      signal === null ||
      signal === undefined ||
      signal.kind === 'action' ||
      signal.kind === 'external',
  )
  return (
    mayEdit.value &&
    (draft.ordering === 'strict' || draft.ordering === 'unordered') &&
    hasSupportedSignals
  )
}

function openEditor(draft: TemplateDraftView): void {
  resetFailure()
  editorTarget.value = { draft, id: draft.id, revision: String(draft.revision) }
  editorVisible.value = true
}

function openKnownEditor(): void {
  resetFailure()
  editorTarget.value = {
    draft: null,
    id: knownDraftId.value.trim(),
    revision: knownRevision.value.trim(),
  }
  editorVisible.value = true
}

async function editorSaved(): Promise<void> {
  ElMessage.success('模板草稿已保存')
  await load()
}

async function editorFailure(error: unknown): Promise<void> {
  if (error instanceof ControlPlaneError && error.status === 409) {
    await handleRevisionConflict()
    return
  }
  recordFailure(error)
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
              <td class="template-management__actions">
                <template v-if="mayEdit">
                  <ElButton
                    v-if="canEditDraft(draft)"
                    link
                    type="primary"
                    @click="openEditor(draft)"
                  >
                    编辑草稿
                  </ElButton>
                  <ElButton
                    v-if="canEditDraft(draft)"
                    link
                    type="warning"
                    :loading="publishingDraftId === draft.id"
                    :disabled="publishingDraftId !== ''"
                    @click="requestPublish(draft)"
                  >
                    发布版本
                  </ElButton>
                  <span v-if="!canEditDraft(draft)" class="template-management__muted">
                    未知值，不能编辑或发布
                  </span>
                </template>
                <span v-else class="template-management__muted">只读</span>
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

      <section class="template-management__versions" aria-labelledby="versions-heading">
        <div class="template-management__section-head">
          <div>
            <h2 id="versions-heading">已发布版本</h2>
            <p>版本一旦发布便不可修改；运行端消费的制品和摘要留在这里。</p>
          </div>
          <span class="template-management__count">{{ versions.length }} 个版本</span>
        </div>
        <table class="template-management__table template-management__table--versions">
          <caption class="template-management__caption">
            不可变 SOP 版本及其可复验制品
          </caption>
          <thead>
            <tr>
              <th scope="col">版本</th>
              <th scope="col">来源草稿</th>
              <th scope="col">发布时间</th>
              <th scope="col">模板语义与默认值</th>
              <th scope="col">摘要</th>
              <th scope="col">制品</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="version in versions" :key="version.id">
              <th scope="row">
                <code>{{ version.id }}</code>
                <small>模板 {{ version.template_id }}</small>
                <ElButton link type="primary" @click="openVersionDetail(version)">
                  查看详情
                </ElButton>
              </th>
              <td>
                <code>{{ version.source_draft_id }}</code>
                <small>修订 {{ version.source_draft_revision }}</small>
                <small>来源导入记录：{{ version.source_import_id }}</small>
              </td>
              <td>
                {{
                  new Date(version.published_at).toLocaleString('zh-CN', {
                    dateStyle: 'medium',
                    timeStyle: 'short',
                    timeZone: 'Asia/Shanghai',
                  })
                }}
                <small>发布人：{{ version.published_by }}</small>
              </td>
              <td>
                <ElTag :type="orderingTag(version.ordering)" disable-transitions>
                  {{ orderingLabel(version.ordering) }}
                </ElTag>
                <small>开始：{{ signalLabel(version.start_signal) }}</small>
                <small>结束：{{ signalListLabel(version.end_signals) }}</small>
                <small>步骤：{{ version.steps.map((step) => step.description).join('、') }}</small>
                <small>
                  默认：空闲 {{ version.runtime_defaults.idle_timeout_seconds }} 秒 · 步骤
                  {{ version.runtime_defaults.step_deadline_seconds }} 秒 ·
                  {{ version.runtime_defaults.disposition_policy ?? '未填写处置策略' }}
                </small>
              </td>
              <td>
                <code class="template-management__digest">{{ version.sha256 }}</code>
              </td>
              <td>
                <ul class="template-management__artifacts">
                  <li v-for="artifact in version.artifacts" :key="artifact.name">
                    <span>
                      <strong>{{ artifact.name }}</strong>
                      <small>{{ artifact.byte_length }} 字节 · {{ artifact.sha256 }}</small>
                    </span>
                    <ElButton
                      link
                      type="primary"
                      :loading="downloadingArtifact === `${version.id}:${artifact.name}`"
                      @click="downloadVersionArtifact(version, artifact.name)"
                    >
                      下载 {{ artifact.name }}
                    </ElButton>
                  </li>
                </ul>
              </td>
            </tr>
            <tr v-if="versions.length === 0">
              <td colspan="6" class="template-management__empty">还没有已发布版本。</td>
            </tr>
          </tbody>
        </table>
      </section>
    </div>

    <p v-else class="template-management__empty-state">当前没有模板查看权限。</p>

    <ElDialog v-model="publishConfirmationVisible" title="确认发布" width="42rem">
      <div v-if="publishTarget" class="template-management__dialog-content">
        <p>将发布草稿修订 {{ publishTarget.revision }}，生成新的不可变模板版本。</p>
        <dl class="template-management__details">
          <div>
            <dt>工位</dt>
            <dd>{{ publishTarget.station_name }}（{{ publishTarget.station_code }}）</dd>
          </div>
          <div>
            <dt>步骤</dt>
            <dd>{{ publishTarget.steps.map((step) => step.description).join('、') }}</dd>
          </div>
          <div>
            <dt>顺序声明</dt>
            <dd>{{ orderingLabel(publishTarget.ordering) }}</dd>
          </div>
          <div>
            <dt>开始信号</dt>
            <dd>{{ signalLabel(publishTarget.start_signal) }}</dd>
          </div>
          <div>
            <dt>结束信号</dt>
            <dd>{{ signalListLabel(publishTarget.end_signals) }}</dd>
          </div>
          <div>
            <dt>运行参数默认值</dt>
            <dd>
              空闲 {{ publishTarget.runtime_defaults.idle_timeout_seconds }} 秒 · 步骤
              {{ publishTarget.runtime_defaults.step_deadline_seconds }} 秒 ·
              {{ publishTarget.runtime_defaults.disposition_policy ?? '未填写处置策略' }}
            </dd>
          </div>
        </dl>
        <p class="template-management__dialog-note">
          发布只表示中心后台保存了版本，不代表工位已绑定或推理机已经生效。版本发布后不可修改。
        </p>
      </div>
      <template #footer>
        <ElButton :disabled="publishingDraftId !== ''" @click="cancelPublish">取消</ElButton>
        <ElButton
          type="primary"
          :loading="publishingDraftId !== ''"
          :disabled="publishTarget === null"
          @click="confirmPublish"
        >
          确认发布
        </ElButton>
      </template>
    </ElDialog>

    <ElDialog v-model="versionDetailVisible" title="版本详情" width="48rem">
      <div v-if="detailVersion" class="template-management__dialog-content">
        <p class="template-management__dialog-note">模板版本发布后不可修改。</p>
        <dl class="template-management__details">
          <div>
            <dt>版本 ID</dt>
            <dd>
              <code>{{ detailVersion.id }}</code>
            </dd>
          </div>
          <div>
            <dt>模板 ID</dt>
            <dd>
              <code>{{ detailVersion.template_id }}</code>
            </dd>
          </div>
          <div>
            <dt>来源导入记录</dt>
            <dd>
              <code>{{ detailVersion.source_import_id }}</code>
            </dd>
          </div>
          <div>
            <dt>来源草稿</dt>
            <dd>
              <code>{{ detailVersion.source_draft_id }}</code
              >（修订 {{ detailVersion.source_draft_revision }}）
            </dd>
          </div>
          <div>
            <dt>发布人</dt>
            <dd>
              <code>{{ detailVersion.published_by }}</code>
            </dd>
          </div>
          <div>
            <dt>发布时间（UTC）</dt>
            <dd>{{ detailVersion.published_at }}</dd>
          </div>
          <div>
            <dt>内容 sha256</dt>
            <dd>
              <code class="template-management__digest">{{ detailVersion.sha256 }}</code>
            </dd>
          </div>
          <div>
            <dt>顺序声明</dt>
            <dd>{{ orderingLabel(detailVersion.ordering) }}</dd>
          </div>
          <div>
            <dt>开始信号</dt>
            <dd>{{ signalLabel(detailVersion.start_signal) }}</dd>
          </div>
          <div>
            <dt>结束信号</dt>
            <dd>{{ signalListLabel(detailVersion.end_signals) }}</dd>
          </div>
          <div>
            <dt>运行参数默认值</dt>
            <dd>
              空闲 {{ detailVersion.runtime_defaults.idle_timeout_seconds }} 秒 · 步骤
              {{ detailVersion.runtime_defaults.step_deadline_seconds }} 秒 ·
              {{ detailVersion.runtime_defaults.disposition_policy ?? '未填写处置策略' }}
            </dd>
          </div>
        </dl>
        <h3>步骤</h3>
        <ol>
          <li v-for="step in detailVersion.steps" :key="step.number">
            {{ step.number }}. {{ step.name }}：{{ step.description }}
          </li>
        </ol>
      </div>
      <template #footer>
        <ElButton @click="versionDetailVisible = false">关闭</ElButton>
      </template>
    </ElDialog>

    <TemplateDraftEditor
      v-model="editorVisible"
      :target="editorTarget"
      @failure="editorFailure"
      @saved="editorSaved"
    />
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

.template-management__versions {
  border-top: 3px solid var(--template-teal);
  background: #fff;
}

.template-management__dialog-content {
  display: grid;
  gap: 1rem;
  line-height: 1.55;
}

.template-management__dialog-content h3 {
  margin: 0;
  font-size: 1rem;
}

.template-management__details {
  display: grid;
  gap: 0.75rem;
  margin: 0;
}

.template-management__details > div {
  display: grid;
  grid-template-columns: 8rem minmax(0, 1fr);
  gap: 0.75rem;
}

.template-management__details dt {
  color: #68717c;
  font-weight: 600;
}

.template-management__details dd {
  min-width: 0;
  margin: 0;
  overflow-wrap: anywhere;
}

.template-management__dialog-note {
  margin: 0;
  padding: 0.75rem 0.9rem;
  border-left: 3px solid var(--template-amber);
  background: var(--template-paper);
}

.template-management__digest {
  display: block;
  max-width: 14rem;
  overflow-wrap: anywhere;
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

.template-management__table th[scope='row'] small,
.template-management__table td small {
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

.template-management__direct-fields {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 1rem;
  margin: 1.25rem 0;
}

.template-management__direct-fields {
  grid-template-columns: repeat(2, minmax(0, 1fr));
  max-width: 38rem;
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

  .template-management__direct-fields {
    grid-template-columns: 1fr;
  }
}
</style>
