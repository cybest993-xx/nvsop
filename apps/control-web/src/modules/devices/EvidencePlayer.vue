<script setup lang="ts">
import { computed, nextTick, onUnmounted, ref, watch } from 'vue'
import {
  ControlPlaneError,
  createEvidenceAccessContext,
  readEvidence,
  readEvidenceReviews,
  registerEvidenceReview,
  type EvidenceReference,
  type EvidenceReview,
  type ReviewConclusion,
} from '@/api/controlPlane'
import { useSessionStore } from '@/session/store'

const props = defineProps<{ hostId: string; stationId: string; instanceId: number }>()
const session = useSessionStore()
const mayView = computed(() => session.may('evidence.evidence.view'))
const mayReview = computed(() => session.may('evidence.review.edit'))
const items = ref<EvidenceReference[]>([])
const loading = ref(false)
const listError = ref('')
const selected = ref<EvidenceReference | null>(null)
const sourceUrl = ref('')
const failure = ref('')
const playing = ref(false)

// 人工复核：结论只追加；只有服务器同步回执才表示已保存。
const reviews = ref<EvidenceReview[]>([])
const reviewsLoading = ref(false)
const reviewsError = ref('')
const conclusion = ref<ReviewConclusion>('confirmed')
const saving = ref(false)
const saveError = ref('')
const savedNotice = ref('')
const savedRef = ref<HTMLElement | null>(null)
const conclusionLabels: Record<ReviewConclusion, string> = {
  confirmed: '确认（同意原判定）',
  rejected: '否定（不同意原判定）',
}
// 一次提交意图由（证据、代次、结论）定义；同一意图重试复用同一 review_id，换结论/证据即新意图。
let reviewToken = 0
let reviewId: string | null = null
let reviewIntent = ''

// 每次切换或重新播放都递增，迟到的响应不得复活已被替换的播放器。
let revision = 0
let controller: AbortController | undefined

// 来源错误码区分缺失、过期、不可达与权限；不把中心“缺对象”当作播放失败。
const failures: Record<string, string> = {
  media_missing: '来源推理机上已无该证据文件（可能已按保留策略删除）。',
  media_changed: '来源文件大小与登记不一致，拒绝播放。',
  not_registered: '中心未登记该证据引用。',
  context_expired: '访问票据已过期，请重新打开该证据。',
  reference_expired: '证据引用已过期，来源机不再保留该片段。',
  reference_unavailable: '证据引用当前不可用。',
  permission_denied: '当前会话无权读取该证据。',
  session_invalid: '会话已失效，请重新登录。',
  access_denied: '中心拒绝了该证据的读取。',
  source_auth_failed: '来源推理机身份认证失败。',
  center_unavailable: '来源推理机暂时无法联系中心。',
  origin_denied: '来源推理机拒绝了本页面来源。',
}
const failureText = (code: string) => failures[code] ?? `证据读取失败（${code || 'unknown'}）。`
const kindLabel = (item: EvidenceReference) => (item.kind === 'clip' ? '证据片段' : '锚点关键帧')
// 中心可能登记 pending/failed 的引用；它们不是“不存在”，要显式说明不可用原因。
const referenceState = (item: EvidenceReference) =>
  item.status === 'failed' ? '登记失败' : '尚未对账'
const reviewTime = (value: string) =>
  new Date(value).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai' })

/** 作废当前提交意图与迟到的复核响应；换证据/结论/权限/主体时调用。 */
function invalidateReview(): void {
  reviewToken += 1
  reviewId = null
  reviewIntent = ''
  saving.value = false
  saveError.value = ''
  savedNotice.value = ''
}

async function loadReviews(item: EvidenceReference, guard: number): Promise<void> {
  reviewsError.value = ''
  if (!mayView.value) return
  reviewsLoading.value = true
  try {
    const rows = await readEvidenceReviews(item.evidence_id)
    if (guard !== revision) return
    // 按 review_id 合并：已保存回执不被迟到的历史读取覆盖。
    reviews.value = mergeReviews(rows)
  } catch (error) {
    if (guard !== revision) return
    reviewsError.value =
      error instanceof ControlPlaneError ? (error.detail ?? error.message) : '复核历史读取失败'
  } finally {
    if (guard === revision) reviewsLoading.value = false
  }
}

/** 按 review_id 去重合并，保持复核时刻升序；本地已确认回执优先于可能过期的历史快照。 */
function mergeReviews(incoming: EvidenceReview[]): EvidenceReview[] {
  const merged = new Map(reviews.value.map((row) => [row.review_id, row]))
  for (const row of incoming) merged.set(row.review_id, row)
  return [...merged.values()].sort((a, b) =>
    a.reviewed_at === b.reviewed_at
      ? a.review_id.localeCompare(b.review_id)
      : a.reviewed_at.localeCompare(b.reviewed_at),
  )
}

async function submitReview(): Promise<void> {
  const item = selected.value
  if (!item || !mayReview.value || saving.value) return
  const intent = `${item.evidence_id}\u0000${item.generation}\u0000${conclusion.value}`
  if (reviewId === null || reviewIntent !== intent) {
    reviewId = crypto.randomUUID()
    reviewIntent = intent
  }
  const submittedId = reviewId
  const token = reviewToken
  saving.value = true
  saveError.value = ''
  savedNotice.value = ''
  try {
    const record = await registerEvidenceReview(item.evidence_id, {
      review_id: submittedId,
      generation: item.generation,
      conclusion: conclusion.value,
    })
    if (token !== reviewToken) return
    // 只有同步持久化回执才显示已保存；直接追加回执，不再多发一次 GET。
    reviews.value = mergeReviews([record])
    reviewId = null
    reviewIntent = ''
    savedNotice.value = `已保存复核结论：${conclusionLabels[record.conclusion]}（服务器时间 ${reviewTime(record.reviewed_at)}）。`
    await nextTick()
    savedRef.value?.focus()
  } catch (error) {
    if (token !== reviewToken) return
    saveError.value =
      error instanceof ControlPlaneError
        ? (error.detail ?? error.message)
        : '复核结论未能保存，请检查网络后按同一意图重试。'
  } finally {
    if (token === reviewToken) saving.value = false
  }
}

function teardown(): void {
  controller?.abort()
  controller = undefined
  sourceUrl.value = ''
  playing.value = false
  failure.value = ''
}

async function load(): Promise<void> {
  // 先递增：撤销权限或切换实例时，迟到的列表/探测结果一律作废。
  const current = ++revision
  teardown()
  invalidateReview()
  selected.value = null
  items.value = []
  listError.value = ''
  if (!mayView.value) return
  loading.value = true
  try {
    const rows = await readEvidence(props.stationId, props.instanceId)
    if (current !== revision) return
    // 只展示所选来源主机的引用；UUID 按小写规范比较，同实例在他机存在也不混入。
    const host = props.hostId.toLowerCase()
    items.value = rows.filter((row) => row.host_id.toLowerCase() === host)
  } catch (error) {
    if (current === revision) {
      listError.value =
        error instanceof ControlPlaneError ? (error.detail ?? error.message) : '证据列表读取失败'
    }
  } finally {
    if (current === revision) loading.value = false
  }
}

async function play(item: EvidenceReference): Promise<void> {
  teardown()
  invalidateReview()
  selected.value = item
  reviews.value = []
  reviewsError.value = ''
  reviewsLoading.value = false
  const current = ++revision
  // 复核编辑权限才自动读取历史；只查看权限经显式按钮查询。
  if (mayReview.value) void loadReviews(item, current)
  let context
  try {
    context = await createEvidenceAccessContext(item.evidence_id)
  } catch (error) {
    if (current !== revision) return
    failure.value =
      error instanceof ControlPlaneError && (error.status === 401 || error.status === 403)
        ? (error.detail ?? '当前会话无权读取该证据。')
        : '中心暂时无法签发访问票据。'
    return
  }
  if (current !== revision) return
  const url = `${context.origin}/evidence/${encodeURIComponent(item.evidence_id)}?context=${encodeURIComponent(context.context)}`
  controller = new AbortController()
  try {
    // 先探测来源，读出 X-Evidence-Error 再决定是否加载；凭据省略，不带中心 Cookie。
    // eslint-disable-next-line no-restricted-globals -- ADR-0012: 浏览器直连来源推理机读取证据媒体，不经中心控制面 API
    const probe = await fetch(url, {
      method: 'HEAD',
      credentials: 'omit',
      cache: 'no-store',
      redirect: 'error',
      signal: controller.signal,
    })
    if (current !== revision) return
    if (probe.ok) sourceUrl.value = url
    else failure.value = failureText(probe.headers.get('X-Evidence-Error') ?? '')
  } catch {
    if (current === revision && !controller?.signal.aborted) {
      failure.value = '来源推理机不可达。'
    }
  }
}

function onDecoded(): void {
  playing.value = true
  failure.value = ''
}

// 播放器错误不带来源错误码；清空后重探同一票据，区分票据过期、文件缺失与解码失败。
function onMediaError(): void {
  const url = sourceUrl.value
  sourceUrl.value = ''
  playing.value = false
  if (!url) return
  const current = revision
  controller = new AbortController()
  void (async () => {
    try {
      // eslint-disable-next-line no-restricted-globals -- ADR-0012: 浏览器直连来源推理机读取证据媒体，不经中心控制面 API
      const probe = await fetch(url, {
        method: 'HEAD',
        credentials: 'omit',
        cache: 'no-store',
        redirect: 'error',
        signal: controller?.signal,
      })
      if (current !== revision) return
      failure.value = probe.ok
        ? '来源媒体无法解码。'
        : failureText(probe.headers.get('X-Evidence-Error') ?? '')
    } catch {
      if (current === revision) failure.value = '来源推理机不可达。'
    }
  })()
}

watch(
  () =>
    [
      props.hostId,
      props.stationId,
      props.instanceId,
      mayView.value,
      mayReview.value,
      session.current?.user_id,
    ] as const,
  () => void load(),
  { immediate: true },
)
// 换结论是一次新提交意图：作废旧幂等键与迟到回执，不覆盖已保存历史。
watch(conclusion, () => invalidateReview())
// 卸载同样作废 revision：迟到的 createContext/readEvidence 不能再发起来源请求。
onUnmounted(() => {
  revision += 1
  invalidateReview()
  teardown()
})
</script>

<template>
  <section v-if="mayView" class="evidence" aria-labelledby="evidence-heading">
    <h4 id="evidence-heading">证据回看（来源推理机直连）</h4>
    <p v-if="loading" class="muted">正在读取证据引用…</p>
    <p v-else-if="listError" role="alert">{{ listError }}</p>
    <p v-else-if="!items.length" class="muted">该实例所选来源主机尚无中心登记的证据引用。</p>
    <ul v-else class="evidence__list">
      <li v-for="item in items" :key="item.evidence_id">
        <button v-if="item.status === 'available'" type="button" @click="play(item)">
          {{ kindLabel(item) }}（锚点 {{ item.anchor }}）
        </button>
        <span v-else class="muted">
          {{ kindLabel(item) }}：引用{{ referenceState(item) }}，不可回看。
        </span>
      </li>
    </ul>
    <p v-if="failure" role="alert">{{ failure }}</p>
    <div v-if="sourceUrl && selected" class="evidence__player">
      <video
        v-if="selected.kind === 'clip'"
        :src="sourceUrl"
        controls
        playsinline
        crossorigin="anonymous"
        referrerpolicy="no-referrer"
        aria-label="证据片段回放"
        @canplay="onDecoded"
        @error="onMediaError"
      >
        <track kind="captions" srclang="zh" label="中文" src="data:text/vtt,WEBVTT" />
      </video>
      <img
        v-else
        :src="sourceUrl"
        alt="证据锚点关键帧"
        crossorigin="anonymous"
        referrerpolicy="no-referrer"
        @load="onDecoded"
        @error="onMediaError"
      />
      <p v-if="playing" role="status">证据已开始解码播放。</p>
    </div>
    <div v-if="selected" class="evidence__review" aria-labelledby="evidence-review-heading">
      <h5 id="evidence-review-heading">人工复核</h5>
      <p class="muted">
        复核对象：{{ selected.evidence_id }}（素材代次 {{ selected.generation }}）。复核结论只追加，
        不改写机器判定、证据或他人记录。
      </p>
      <p v-if="reviewsLoading" class="muted">正在读取复核历史…</p>
      <p v-if="reviewsError" role="alert">{{ reviewsError }}</p>
      <button
        v-if="!mayReview && !reviewsLoading && !reviewsError && !reviews.length"
        type="button"
        @click="loadReviews(selected, revision)"
      >
        读取复核历史
      </button>
      <p v-if="mayReview && !reviewsLoading && !reviewsError && !reviews.length" class="muted">
        该证据尚无人工复核结论。
      </p>
      <p v-if="reviewsError && reviews.length" class="muted">
        历史列表可能不完整；下列为本次会话已确认保存的回执。
      </p>
      <ul v-if="reviews.length" class="evidence__reviews">
        <li v-for="row in reviews" :key="row.review_id">
          <strong>{{ conclusionLabels[row.conclusion] }}</strong>
          — {{ row.reviewer_name }}（{{ reviewTime(row.reviewed_at) }}，素材代次
          {{ row.generation }}）
        </li>
      </ul>
      <form v-if="mayReview" class="evidence__form" @submit.prevent="submitReview">
        <fieldset :disabled="saving">
          <legend>提交复核结论</legend>
          <label for="review-conclusion-confirmed">
            <input
              id="review-conclusion-confirmed"
              v-model="conclusion"
              type="radio"
              value="confirmed"
              name="conclusion"
            />
            确认（同意原判定）
          </label>
          <label for="review-conclusion-rejected">
            <input
              id="review-conclusion-rejected"
              v-model="conclusion"
              type="radio"
              value="rejected"
              name="conclusion"
            />
            否定（不同意原判定）
          </label>
          <button type="submit">{{ saving ? '正在保存…' : '保存复核结论' }}</button>
        </fieldset>
      </form>
      <p v-else class="muted">当前会话只有查看权限，不能提交复核结论。</p>
      <p v-if="saveError" role="alert">{{ saveError }}</p>
      <p v-if="savedNotice" ref="savedRef" role="status" tabindex="-1">{{ savedNotice }}</p>
    </div>
  </section>
</template>

<style scoped>
.evidence {
  margin-top: 0.75rem;
}
.evidence__list {
  list-style: none;
  padding: 0;
  display: flex;
  gap: 0.5rem;
  flex-wrap: wrap;
}
.evidence__player video,
.evidence__player img {
  max-width: 100%;
}
.evidence__review {
  margin-top: 0.75rem;
}
.evidence__reviews {
  list-style: none;
  padding: 0;
}
.evidence__form fieldset {
  display: flex;
  gap: 0.75rem;
  align-items: center;
  flex-wrap: wrap;
}
.evidence .muted {
  color: var(--el-text-color-secondary);
}
</style>
