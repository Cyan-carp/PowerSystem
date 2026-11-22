<script setup lang="ts">
import { onBeforeUnmount, ref, watch } from 'vue'
import { ApiError, supplementary } from '../lib/api'
import { stamp } from '../lib/format'

const props = defineProps<{ predictionId?: number | null }>()
type Status = 'confirmed_fault' | 'not_occurred' | 'watching'
interface Feedback {
  id: number
  prediction_id: number
  actor_name: string
  status: Status
  occurred_at: string | null
  note: string
  created_at: string
  device_code?: string
  window_end_ms?: number
}
const labels: Record<Status, string> = { confirmed_fault: '确认故障', not_occurred: '未发生', watching: '待观察' }
const rows = ref<Feedback[]>([])
const counts = ref<Record<Status, number>>()
const before = ref<number | null>(null)
const windowEnds = ref(0)
const status = ref<Status>('watching')
const occurred = ref('')
const note = ref('')
const open = ref(false)
const ready = ref(false)
const loading = ref(false)
const saving = ref(false)
const readError = ref('')
const writeError = ref('')
let readRequest: AbortController | undefined
let writeRequest: AbortController | undefined
let requestID = ''
const endpoint = () => `/predictions/${props.predictionId}/feedback`

async function load(cursor?: number) {
  readRequest?.abort()
  const controller = new AbortController()
  readRequest = controller
  loading.value = true
  readError.value = ''
  try {
    const page = await supplementary.read<{
      list: Feedback[]; counts?: Record<Status, number>; next_before_id?: number | null; window_ends_at_ms?: number
    }>(props.predictionId ? endpoint() + (cursor ? `?before_id=${cursor}` : '') : '/predictions/quality/human-review', controller.signal)
    if (controller.signal.aborted) return
    rows.value = cursor ? [...rows.value, ...page.list] : page.list
    counts.value = page.counts
    before.value = page.next_before_id ?? null
    windowEnds.value = page.window_ends_at_ms ?? 0
    ready.value = true
  } catch (error) {
    if (!controller.signal.aborted) readError.value = String((error as Error).message || error)
  } finally {
    if (!controller.signal.aborted) loading.value = false
  }
}

async function save() {
  if (saving.value || !ready.value || !props.predictionId) return
  const controller = new AbortController()
  writeRequest = controller
  saving.value = true
  writeError.value = ''
  requestID ||= crypto.randomUUID()
  try {
    const occurredAt = status.value === 'confirmed_fault' ? new Date(`${occurred.value}+08:00`).toISOString() : null
    await supplementary.write(endpoint(), {
      request_id: requestID, previous_id: rows.value[0]?.id ?? null, status: status.value, occurred_at: occurredAt, note: note.value,
    }, controller.signal)
    if (controller.signal.aborted) return
    requestID = ''
    note.value = occurred.value = ''
    open.value = false
    await load()
  } catch (error) {
    if (controller.signal.aborted) return
    writeError.value = String((error as Error).message || error)
    if (error instanceof ApiError && error.status === 409) {
      requestID = ''
      await load()
    }
  } finally {
    if (!controller.signal.aborted) saving.value = false
  }
}

function abortRequests() {
  readRequest?.abort()
  writeRequest?.abort()
}
watch(() => props.predictionId, () => {
  abortRequests()
  rows.value = []
  before.value = null
  open.value = ready.value = saving.value = false
  writeError.value = note.value = occurred.value = requestID = ''
  status.value = 'watching'
  void load()
}, { immediate: true })
watch([status, occurred, note], () => { requestID = '' }, { flush: 'sync' })
onBeforeUnmount(abortRequests)
</script>

<template>
  <section class="card review">
    <header class="card-head">
      <h2 class="card-title">{{ predictionId ? '预测结果反馈' : '人工复核' }}</h2>
      <button v-if="predictionId" class="btn btn-sm" type="button" :disabled="!ready || saving" @click="open = !open">{{ open ? '收起' : '提交反馈' }}</button>
      <button v-else class="btn btn-sm" type="button" :disabled="loading" @click="load()">刷新</button>
    </header>
    <div class="card-body">
      <form v-if="open && predictionId" @submit.prevent="save">
        <fieldset :disabled="saving">
          <label>复核结果<select v-model="status"><option v-for="(label, key) in labels" :key="key" :value="key">{{ label }}</option></select></label>
          <span v-if="status === 'not_occurred' && windowEnds" class="muted">观察窗口结束时间 {{ stamp(new Date(windowEnds).toISOString()) }}</span>
          <label v-if="status === 'confirmed_fault'">实际发生时间（北京时间）<input v-model="occurred" type="datetime-local" required /></label>
          <label>说明<textarea v-model="note" required maxlength="2000" rows="2" /></label>
          <button class="btn btn-primary btn-sm" type="submit">{{ saving ? '保存中…' : '保存反馈' }}</button>
        </fieldset>
        <p v-if="writeError" class="error-box" role="alert">{{ writeError }}</p>
      </form>
      <p v-if="readError" role="alert">{{ readError }} <button type="button" class="btn btn-sm" @click="load()">重试</button></p>
      <p v-else-if="loading && !rows.length" role="status">正在读取人工复核…</p>
      <dl v-if="counts" class="review-counts" aria-label="按每条预测的最新反馈统计">
        <div v-for="(label, key) in labels" :key="key"><dt>{{ label }}</dt><dd>{{ counts[key] }}</dd></div>
      </dl>
      <p v-if="ready && !rows.length" class="muted">还没有人工反馈。</p>
      <article v-for="row in rows" :key="row.id" class="review-row">
        <strong>{{ labels[row.status] }}</strong>
        <span v-if="row.device_code"> · {{ row.device_code }}<span v-if="row.window_end_ms"> · 预测于 {{ stamp(new Date(row.window_end_ms).toISOString()) }}</span></span>
        <span class="muted"> · {{ row.actor_name }} · {{ stamp(row.created_at) }}</span>
        <p v-if="row.occurred_at">实际发生于 {{ stamp(row.occurred_at) }}</p>
        <p class="note">{{ row.note }}</p>
      </article>
      <button v-if="before" class="btn btn-sm" type="button" :disabled="loading" @click="load(before)">更多反馈</button>
    </div>
  </section>
</template>

<style scoped>
.review { margin-block: 12px; }
fieldset { display: grid; gap: 10px; margin: 0 0 10px; padding: 0; border: 0; min-width: 0; }
label { display: grid; gap: 4px; }
input, select, textarea { padding: 8px; border: 1px solid var(--line-strong); border-radius: var(--r-lg); background: var(--field-bg); color: var(--ink); }
button[type='submit'] { justify-self: start; }
.review-counts { display: flex; flex-wrap: wrap; gap: 16px 28px; margin: 0 0 12px; }
.review-counts dt { color: var(--ink-3); font-size: 12px; }
.review-counts dd { margin: 0; font-size: 20px; font-weight: 600; }
.review-row { border-top: 1px solid var(--line); padding-block: 10px; }
.note { white-space: pre-wrap; overflow-wrap: anywhere; }
</style>
