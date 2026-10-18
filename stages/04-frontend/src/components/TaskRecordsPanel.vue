<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from 'vue'
import { api } from '../lib/api'
import { dateTime } from '../lib/format'
import { activeTaskStatuses, taskLabels } from '../lib/ops-task'
import type { OpsStatus, OpsTask } from '../types'
import OpsTaskCard from './OpsTaskCard.vue'

type Filter = 'all' | 'unlinked' | 'active'
const props = defineProps<{ modelValue: boolean; initialFilter: Filter }>()
const emit = defineEmits<{
  'update:modelValue': [value: boolean]
  openSession: [id: string]
  explain: [task: OpsTask]
  source: [id: string]
  refreshNotice: []
}>()
const filter = ref<Filter>('all')
const records = ref<OpsTask[]>([])
const loading = ref(false)
const error = ref('')
const more = ref(false)
const selected = ref<OpsTask>()
const detailLoading = ref(false)
const detailError = ref('')
const unlinked = (task: OpsTask): boolean => !task.parent_turn_id || task.link_pending === true
const visibleRecords = computed(() => filter.value === 'unlinked' ? records.value.filter(unlinked) : records.value)
let cursors = new Map<string, { offset: number; more: boolean }>()
let listVersion = 0, detailVersion = 0
let listController: AbortController | undefined, detailController: AbortController | undefined

async function loadRecords(reset = false): Promise<void> {
  if (!props.modelValue) return
  listController?.abort()
  const controller = new AbortController(); listController = controller
  const version = ++listVersion
  if (reset) {
    records.value = []; more.value = false
    const statuses = filter.value === 'active' ? activeTaskStatuses : ['all']
    cursors = new Map(statuses.map(status => [status, { offset: 0, more: true }]))
  }
  loading.value = true; error.value = ''
  try {
    const pages = await Promise.all([...cursors].filter(([, cursor]) => cursor.more).map(async ([status, cursor]) => ({
      status,
      data: await api.opsTasks(undefined, cursor.offset, { status: status === 'all' ? undefined : status as OpsStatus, limit: 20, signal: controller.signal }),
    })))
    if (version !== listVersion || !props.modelValue) return
    const unique = new Map(records.value.map(task => [task.task_id, task]))
    for (const { status, data } of pages) {
      for (const task of data.list) unique.set(task.task_id, task)
      cursors.set(status, { offset: (cursors.get(status)?.offset || 0) + data.list.length, more: data.list.length === data.page_size })
    }
    records.value = [...unique.values()].sort((a, b) => (Date.parse(b.created_at || '') || 0) - (Date.parse(a.created_at || '') || 0) || b.task_id.localeCompare(a.task_id))
    more.value = [...cursors.values()].some(cursor => cursor.more)
  } catch {
    if (version === listVersion && !controller.signal.aborted) error.value = '任务记录读取失败，已加载的记录保留，请重试。'
    controller.abort()
  } finally { if (version === listVersion) loading.value = false }
}

async function openDetail(id: string): Promise<void> {
  detailController?.abort()
  const controller = new AbortController(); detailController = controller
  const version = ++detailVersion
  selected.value = undefined; detailError.value = ''; detailLoading.value = true
  try {
    const task = await api.opsTask(id, controller.signal)
    if (version === detailVersion && props.modelValue) selected.value = task
  } catch {
    if (version === detailVersion && !controller.signal.aborted) detailError.value = '任务详情暂时无法读取，请重新选择记录。'
  } finally { if (version === detailVersion) detailLoading.value = false }
}

function chooseFilter(value: Filter): void {
  detailVersion++; detailController?.abort(); detailLoading.value = false
  selected.value = undefined; detailError.value = ''; filter.value = value
  void loadRecords(true)
}
function backToRecords(): void { selected.value = undefined; emit('refreshNotice'); void loadRecords(true) }
function stopRequests(): void {
  listVersion++; detailVersion++; listController?.abort(); detailController?.abort()
  loading.value = false; detailLoading.value = false; selected.value = undefined
}
watch(() => props.modelValue, open => {
  if (open) { detailError.value = ''; selected.value = undefined; filter.value = props.initialFilter; void loadRecords(true) }
  else { stopRequests(); emit('refreshNotice') }
})
onUnmounted(stopRequests)
</script>

<template>
  <el-drawer :model-value="modelValue" title="任务记录" size="min(960px, 96vw)" destroy-on-close @update:model-value="emit('update:modelValue', $event)">
    <div class="task-records">
      <p class="records-note">查看本人已受理的任务。打开记录只读取已有状态与结果，不会重新执行诊断。</p>
      <template v-if="selected">
        <div class="records-toolbar"><el-button @click="backToRecords">返回任务列表</el-button><el-button v-if="!unlinked(selected)" @click="emit('openSession', selected.session_id)">打开关联会话</el-button></div>
        <p v-if="unlinked(selected)" class="records-note">这项任务未关联到已保存的对话轮次，仍可在此查看已有结果。</p>
        <OpsTaskCard :key="selected.task_id" :initial="selected" historical @source="emit('source', $event)" @explain="emit('explain', selected)" />
      </template>
      <template v-else>
        <div class="records-toolbar" role="group" aria-label="筛选任务记录">
          <button v-for="option in ([['all', '全部任务'], ['unlinked', '未关联对话'], ['active', '未完成任务']] as const)" :key="option[0]" type="button" class="records-filter" :class="{ active: filter === option[0] }" :aria-pressed="filter === option[0]" @click="chooseFilter(option[0])">{{ option[1] }}</button>
          <el-button text :loading="loading" @click="loadRecords(true)">刷新</el-button>
        </div>
        <p v-if="error || detailError" class="records-error" role="alert">{{ error || detailError }}</p>
        <p v-if="detailLoading" class="records-note" role="status">正在读取任务详情…</p>
        <p v-else-if="loading && !records.length" class="records-note" role="status">正在读取任务记录…</p>
        <p v-else-if="!visibleRecords.length && !error" class="records-note">{{ more ? '已加载记录中暂无匹配任务，可继续加载。' : '暂无符合条件的任务记录。' }}</p>
        <button v-for="task in visibleRecords" :key="task.task_id" type="button" class="records-row" :disabled="detailLoading" @click="openDetail(task.task_id)">
          <span class="records-intent">{{ task.intent || '运维任务' }}</span>
          <span class="records-state">{{ taskLabels[task.status] }}</span>
          <span class="records-meta"><time :datetime="task.created_at">{{ dateTime(task.created_at) }}</time><span>{{ unlinked(task) ? '未关联对话' : '已关联会话' }}</span><span class="records-id">{{ task.task_id }}</span></span>
        </button>
        <el-button v-if="more" :loading="loading" :disabled="detailLoading" @click="loadRecords()">加载更多记录</el-button>
      </template>
    </div>
  </el-drawer>
</template>

<style scoped>
.task-records{color:var(--ink);font-size:13px}.records-note{color:var(--ink-3);font-size:12px;line-height:1.8}.records-toolbar{display:flex;flex-wrap:wrap;align-items:center;gap:8px;margin:16px 0}.records-toolbar>.el-button:last-child{margin-left:auto}.records-filter{font:inherit;padding:7px 12px;border:1px solid var(--line);border-radius:3px;background:transparent;color:var(--ink-2)}.records-filter.active{color:var(--brand);border-color:var(--brand-line);background:var(--brand-soft)}.records-row{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:8px 16px;width:100%;padding:14px 10px;border:0;border-bottom:1px solid var(--line);background:transparent;color:var(--ink);font:inherit;text-align:left}.records-row:hover{background:var(--hover)}.records-row:disabled{cursor:wait}.records-row:focus-visible,.records-filter:focus-visible{outline:2px solid var(--brand);outline-offset:2px}.records-intent{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.records-state{color:var(--ink-2);font-size:12px}.records-meta{grid-column:1/-1;display:flex;flex-wrap:wrap;gap:6px 16px;color:var(--ink-3);font-size:11px;font-variant-numeric:tabular-nums}.records-id{overflow-wrap:anywhere}.records-error{font-size:12px;color:var(--danger)}.task-records>.el-button{margin-top:16px}
</style>
