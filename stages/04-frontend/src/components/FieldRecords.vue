<script setup lang="ts">
import { onBeforeUnmount, ref, watch } from 'vue'
import { supplementary } from '../lib/api'
import { stamp } from '../lib/format'

const props = defineProps<{ target: 'devices' | 'alarms'; targetId: number }>()
interface RecordItem {
  id: number
  alarm_id: number | null
  actor_name: string
  created_at: string
  symptoms: string
  measures: string
  result: string
}
const rows = ref<RecordItem[]>([])
const before = ref<number | null>(null)
const form = ref({ symptoms: '', measures: '', result: '' })
const open = ref(false)
const loading = ref(false)
const saving = ref(false)
const readError = ref('')
const writeError = ref('')
let readRequest: AbortController | undefined
let writeRequest: AbortController | undefined
let requestID = ''
const endpoint = () => `/${props.target}/${props.targetId}/field-records`

async function load(cursor?: number) {
  readRequest?.abort()
  const controller = new AbortController()
  readRequest = controller
  loading.value = true
  readError.value = ''
  try {
    const page = await supplementary.read<{ list: RecordItem[]; next_before_id: number | null }>(
      endpoint() + (cursor ? `?before_id=${cursor}` : ''), controller.signal,
    )
    if (controller.signal.aborted) return
    rows.value = cursor ? [...rows.value, ...page.list] : page.list
    before.value = page.next_before_id
  } catch (error) {
    if (!controller.signal.aborted) readError.value = String((error as Error).message || error)
  } finally {
    if (!controller.signal.aborted) loading.value = false
  }
}

async function save() {
  if (saving.value) return
  const controller = new AbortController()
  writeRequest = controller
  saving.value = true
  writeError.value = ''
  requestID ||= crypto.randomUUID()
  try {
    await supplementary.write(endpoint(), { ...form.value, request_id: requestID }, controller.signal)
    if (controller.signal.aborted) return
    form.value = { symptoms: '', measures: '', result: '' }
    requestID = ''
    open.value = false
    await load()
  } catch (error) {
    if (!controller.signal.aborted) writeError.value = String((error as Error).message || error)
  } finally {
    if (!controller.signal.aborted) saving.value = false
  }
}

function abortRequests() {
  readRequest?.abort()
  writeRequest?.abort()
}

watch(() => [props.target, props.targetId], () => {
  abortRequests()
  rows.value = []
  before.value = null
  form.value = { symptoms: '', measures: '', result: '' }
  open.value = saving.value = false
  writeError.value = requestID = ''
  void load()
}, { immediate: true })
watch(form, () => { requestID = '' }, { deep: true, flush: 'sync' })
onBeforeUnmount(abortRequests)
</script>

<template>
  <section class="card records">
    <header class="card-head">
      <h2 class="card-title">现场记录</h2>
      <button type="button" class="btn btn-sm" :disabled="saving" @click="open = !open">{{ open ? '收起' : '添加记录' }}</button>
    </header>
    <div class="card-body">
      <form v-if="open" class="record-form" @submit.prevent="save">
        <fieldset :disabled="saving">
          <label>异常现象<textarea v-model="form.symptoms" required maxlength="2000" rows="2" /></label>
          <label>处理措施<textarea v-model="form.measures" required maxlength="2000" rows="2" /></label>
          <label>处理结果<textarea v-model="form.result" required maxlength="2000" rows="2" /></label>
          <button class="btn btn-primary btn-sm" type="submit">{{ saving ? '保存中…' : '保存记录' }}</button>
        </fieldset>
        <p v-if="writeError" class="error-box" role="alert">{{ writeError }}</p>
      </form>
      <p v-if="readError" role="alert">{{ readError }} <button class="btn btn-sm" type="button" @click="load()">重试</button></p>
      <p v-else-if="loading && !rows.length" role="status">正在读取现场记录…</p>
      <p v-else-if="!rows.length" class="muted">还没有现场记录。</p>
      <article v-for="row in rows" :key="row.id" class="record-row">
        <header>{{ row.actor_name }} · {{ stamp(row.created_at) }}<span v-if="row.alarm_id"> · 告警 #{{ row.alarm_id }}</span></header>
        <dl>
          <dt>异常现象</dt><dd>{{ row.symptoms }}</dd>
          <dt>处理措施</dt><dd>{{ row.measures }}</dd>
          <dt>处理结果</dt><dd>{{ row.result }}</dd>
        </dl>
      </article>
      <button v-if="before" class="btn btn-sm" type="button" :disabled="loading" @click="load(before)">更多记录</button>
    </div>
  </section>
</template>

<style scoped>
.records { margin-block: 12px; }
.record-form { margin-bottom: 12px; }
fieldset { display: grid; gap: 10px; padding: 0; margin: 0; border: 0; min-width: 0; }
label { display: grid; gap: 4px; }
textarea { resize: vertical; padding: 8px; border: 1px solid var(--line-strong); border-radius: var(--r-lg); background: var(--field-bg); }
button[type='submit'] { justify-self: start; }
.record-row { border-top: 1px solid var(--line); padding-block: 10px; }
.record-row header, dt { color: var(--ink-3); font-size: 12px; }
.record-row dl { display: grid; grid-template-columns: auto 1fr; gap: 4px 10px; margin-bottom: 0; }
dd { margin: 0; white-space: pre-wrap; overflow-wrap: anywhere; }
</style>
