<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api } from '../lib/api'
import { dateTime } from '../lib/format'
import { activeTaskStatuses, allDeletableSelected, canDeleteTask, retainedSelection, taskLabels, toggleSelection } from '../lib/ops-task'
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
const selectMode = ref(false)
const selectedIDs = ref<string[]>([])
const batchDeleting = ref(false)
const unlinked = (task: OpsTask): boolean => !task.parent_turn_id || task.link_pending === true
const visibleRecords = computed(() => filter.value === 'unlinked' ? records.value.filter(unlinked) : records.value)
const selectableRecords = computed(() => visibleRecords.value.filter(task => canDeleteTask(task.status)))
const someSelected = computed(() => selectedIDs.value.length > 0)
const allSelected = computed(() => allDeletableSelected(selectedIDs.value, visibleRecords.value))
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
    const kept = retainedSelection(selectedIDs.value, visibleRecords.value)
    if (selectMode.value && kept.length !== selectedIDs.value.length) ElMessage.warning('部分已选任务已不在当前列表中，已自动取消选择')
    selectedIDs.value = kept
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
  exitSelectMode()
  detailVersion++; detailController?.abort(); detailLoading.value = false
  selected.value = undefined; detailError.value = ''; filter.value = value
  void loadRecords(true)
}
function backToRecords(): void { exitSelectMode(); selected.value = undefined; emit('refreshNotice'); void loadRecords(true) }
function enterSelectMode(): void { if (batchDeleting.value) return; selectMode.value = true; selectedIDs.value = [] }
function exitSelectMode(): void { selectMode.value = false; selectedIDs.value = [] }
function toggleSelect(id: string): void { if (batchDeleting.value) return; selectedIDs.value = toggleSelection(selectedIDs.value, id) }
function toggleSelectAll(): void { selectedIDs.value = allSelected.value ? [] : selectableRecords.value.map(task => task.task_id) }
function chooseRow(task: OpsTask): void {
  if (!selectMode.value) { void openDetail(task.task_id); return }
  if (!canDeleteTask(task.status)) return
  toggleSelect(task.task_id)
}
async function deleteSelected(): Promise<void> {
  const ids = [...selectedIDs.value]
  if (!ids.length || batchDeleting.value) return
  try {
    await ElMessageBox.confirm(`将删除 ${ids.length} 条任务记录。删除后这些任务不再出现在列表中，任务步骤、工具调用、审批与安全审查记录仍全部保留，可从对话与详情继续追溯。`, '批量删除任务记录', { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消', confirmButtonClass: 'el-button--danger' })
  } catch { return }
  batchDeleting.value = true
  let failed = 0
  for (const id of ids) { try { await api.deleteOpsTask(id) } catch { failed++ } }
  batchDeleting.value = false
  records.value = records.value.filter(task => !ids.includes(task.task_id))
  exitSelectMode()
  if (props.modelValue) await loadRecords(true)
  if (failed) ElMessage.error(`${failed} 条任务记录删除失败，请刷新后重试`)
  else ElMessage.success(`已删除 ${ids.length} 条任务记录`)
}
function stopRequests(): void {
  listVersion++; detailVersion++; listController?.abort(); detailController?.abort()
  loading.value = false; detailLoading.value = false; selected.value = undefined
  exitSelectMode()
}
watch(() => props.modelValue, open => {
  if (open) { detailError.value = ''; selected.value = undefined; exitSelectMode(); filter.value = props.initialFilter; void loadRecords(true) }
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
          <button v-for="option in ([['all', '全部任务'], ['unlinked', '未关联对话'], ['active', '未完成任务']] as const)" :key="option[0]" type="button" class="records-filter" :class="{ active: filter === option[0] }" :aria-pressed="filter === option[0]" :disabled="batchDeleting" @click="chooseFilter(option[0])">{{ option[1] }}</button>
          <el-button v-if="!selectMode" text :disabled="loading || batchDeleting || !selectableRecords.length" @click="enterSelectMode">批量删除</el-button>
          <template v-else>
            <el-checkbox :model-value="allSelected" :indeterminate="someSelected && !allSelected" :disabled="batchDeleting" @change="toggleSelectAll">全选可删任务</el-checkbox>
            <el-button text type="danger" :disabled="!selectedIDs.length || batchDeleting" :loading="batchDeleting" @click="deleteSelected">删除{{ selectedIDs.length ? ` (${selectedIDs.length})` : '' }}</el-button>
            <el-button text :disabled="batchDeleting" @click="exitSelectMode">取消</el-button>
          </template>
          <el-button text :loading="loading" :disabled="batchDeleting" @click="loadRecords(true)">刷新</el-button>
        </div>
        <p v-if="selectMode" class="records-note">仅已结束的任务可勾选；进行中的任务需等待结束或取消后再删除。删除只影响本列表，任务步骤、工具调用、审批与安全审查记录全部保留。</p>
        <p v-if="error || detailError" class="records-error" role="alert">{{ error || detailError }}</p>
        <p v-if="detailLoading" class="records-note" role="status">正在读取任务详情…</p>
        <p v-else-if="loading && !records.length" class="records-note" role="status">正在读取任务记录…</p>
        <p v-else-if="!visibleRecords.length && !error" class="records-note">{{ more ? '已加载记录中暂无匹配任务，可继续加载。' : '暂无符合条件的任务记录。' }}</p>
        <div v-for="task in visibleRecords" :key="task.task_id" class="records-item">
          <el-checkbox v-if="selectMode" class="records-check" :model-value="selectedIDs.includes(task.task_id)" :disabled="!canDeleteTask(task.status) || batchDeleting" :aria-label="`选择任务：${task.intent || '运维任务'}`" @change="toggleSelect(task.task_id)" />
          <button type="button" class="records-row" :class="{ selecting: selectMode, checked: selectMode && selectedIDs.includes(task.task_id), locked: selectMode && !canDeleteTask(task.status) }" :disabled="detailLoading || batchDeleting" :aria-pressed="selectMode ? selectedIDs.includes(task.task_id) : undefined" @click="chooseRow(task)">
            <span class="records-intent">{{ task.intent || '运维任务' }}</span>
            <span class="records-state">{{ taskLabels[task.status] }}</span>
            <span class="records-meta"><time :datetime="task.created_at">{{ dateTime(task.created_at) }}</time><span>{{ unlinked(task) ? '未关联对话' : '已关联会话' }}</span><span class="records-id">{{ task.task_id }}</span></span>
          </button>
        </div>
        <el-button v-if="more" :loading="loading" :disabled="detailLoading || batchDeleting" @click="loadRecords()">加载更多记录</el-button>
      </template>
    </div>
  </el-drawer>
</template>

<style scoped>
.task-records{color:var(--ink);font-size:13px}.records-note{color:var(--ink-3);font-size:12px;line-height:1.8}.records-toolbar{display:flex;flex-wrap:wrap;align-items:center;gap:8px;margin:16px 0}.records-toolbar>.el-button:last-child{margin-left:auto}.records-filter{font:inherit;padding:7px 12px;border:1px solid var(--line);border-radius:3px;background:transparent;color:var(--ink-2)}.records-filter.active{color:var(--brand);border-color:var(--brand-line);background:var(--brand-soft)}.records-item{display:flex;align-items:flex-start;border-bottom:1px solid var(--line)}.records-check{flex:0 0 auto;margin:8px 0 0 10px}.records-row{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:8px 16px;flex:1;min-width:0;padding:14px 10px;border:0;background:transparent;color:var(--ink);font:inherit;text-align:left}.records-row:hover{background:var(--hover)}.records-row.selecting{cursor:pointer}.records-row.checked{background:var(--brand-soft)}.records-row.locked{cursor:not-allowed}.records-row.locked .records-intent,.records-row.locked .records-state{color:var(--ink-3)}.records-row:disabled{cursor:wait}.records-row:focus-visible,.records-filter:focus-visible{outline:2px solid var(--brand);outline-offset:2px}.records-intent{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.records-state{color:var(--ink-2);font-size:12px}.records-meta{grid-column:1/-1;display:flex;flex-wrap:wrap;gap:6px 16px;color:var(--ink-3);font-size:11px;font-variant-numeric:tabular-nums}.records-id{overflow-wrap:anywhere}.records-error{font-size:12px;color:var(--danger)}.task-records>.el-button{margin-top:16px}
</style>
