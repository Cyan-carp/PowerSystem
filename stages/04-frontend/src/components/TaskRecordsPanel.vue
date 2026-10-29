<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from 'vue'
import Icon from './Icon.vue'
import OpsTaskCard from './OpsTaskCard.vue'
import { api } from '../lib/api'
import { stamp } from '../lib/format'
import {
  activeTaskStatuses,
  allDeletableSelected,
  canDeleteTask,
  retainedSelection,
  taskLabels,
  toggleSelection,
} from '../lib/ops-task'
import type { OpsStatus, OpsTask } from '../lib/ops-types'
import { askConfirm, toast } from '../lib/toast'

type Filter = 'all' | 'unlinked' | 'active'

const props = defineProps<{ open: boolean; initialFilter?: Filter }>()
const emit = defineEmits<{
  'update:open': [value: boolean]
  refreshNotice: []
  openSession: [id: string]
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
const visibleRecords = computed(() =>
  filter.value === 'unlinked' ? records.value.filter(unlinked) : records.value,
)
const selectableRecords = computed(() => visibleRecords.value.filter((task) => canDeleteTask(task.status)))
const someSelected = computed(() => selectedIDs.value.length > 0)
const allSelected = computed(() => allDeletableSelected(selectedIDs.value, visibleRecords.value))

let cursors = new Map<string, { offset: number; more: boolean }>()
let listVersion = 0
let detailVersion = 0
let listController: AbortController | undefined
let detailController: AbortController | undefined

async function loadRecords(reset = false): Promise<void> {
  if (!props.open) return
  listController?.abort()
  const controller = new AbortController()
  listController = controller
  const version = ++listVersion
  if (reset) {
    records.value = []
    more.value = false
    const statuses = filter.value === 'active' ? activeTaskStatuses : ['all']
    cursors = new Map(statuses.map((status) => [status, { offset: 0, more: true }]))
  }
  loading.value = true
  error.value = ''
  try {
    const pages = await Promise.all(
      [...cursors]
        .filter(([, cursor]) => cursor.more)
        .map(async ([status, cursor]) => ({
          status,
          data: await api.opsTasks(undefined, cursor.offset, {
            status: status === 'all' ? undefined : (status as OpsStatus),
            limit: 20,
            signal: controller.signal,
          }),
        })),
    )
    if (version !== listVersion || !props.open) return
    const unique = new Map(records.value.map((task) => [task.task_id, task]))
    for (const { status, data } of pages) {
      for (const task of data.list) unique.set(task.task_id, task)
      cursors.set(status, {
        offset: (cursors.get(status)?.offset || 0) + data.list.length,
        more: data.list.length === data.page_size,
      })
    }
    records.value = [...unique.values()].sort(
      (a, b) =>
        (Date.parse(b.created_at || '') || 0) - (Date.parse(a.created_at || '') || 0) ||
        b.task_id.localeCompare(a.task_id),
    )
    const kept = retainedSelection(selectedIDs.value, visibleRecords.value)
    if (selectMode.value && kept.length !== selectedIDs.value.length) {
      toast('warn', '部分已选任务已不在当前列表中，已自动取消选择')
    }
    selectedIDs.value = kept
    more.value = [...cursors.values()].some((cursor) => cursor.more)
  } catch {
    if (version === listVersion && !controller.signal.aborted) {
      error.value = '任务记录读取失败，已加载的记录保留，请重试。'
    }
    controller.abort()
  } finally {
    if (version === listVersion) loading.value = false
  }
}

async function openDetail(id: string): Promise<void> {
  detailController?.abort()
  const controller = new AbortController()
  detailController = controller
  const version = ++detailVersion
  selected.value = undefined
  detailError.value = ''
  detailLoading.value = true
  try {
    const task = await api.opsTask(id, controller.signal)
    if (version === detailVersion && props.open) selected.value = task
  } catch {
    if (version === detailVersion && !controller.signal.aborted) {
      detailError.value = '任务详情暂时无法读取，请重新选择记录。'
    }
  } finally {
    if (version === detailVersion) detailLoading.value = false
  }
}

function chooseFilter(value: Filter): void {
  exitSelectMode()
  detailVersion++
  detailController?.abort()
  detailLoading.value = false
  selected.value = undefined
  detailError.value = ''
  filter.value = value
  void loadRecords(true)
}

function backToRecords(): void {
  exitSelectMode()
  selected.value = undefined
  emit('refreshNotice')
  void loadRecords(true)
}

function enterSelectMode(): void {
  if (batchDeleting.value) return
  selectMode.value = true
  selectedIDs.value = []
}

function exitSelectMode(): void {
  selectMode.value = false
  selectedIDs.value = []
}

function toggleSelect(id: string): void {
  if (batchDeleting.value) return
  selectedIDs.value = toggleSelection(selectedIDs.value, id)
}

function toggleSelectAll(): void {
  selectedIDs.value = allSelected.value ? [] : selectableRecords.value.map((task) => task.task_id)
}

function chooseRow(task: OpsTask): void {
  if (!selectMode.value) {
    void openDetail(task.task_id)
    return
  }
  if (!canDeleteTask(task.status)) return
  toggleSelect(task.task_id)
}

async function deleteSelected(): Promise<void> {
  const ids = [...selectedIDs.value]
  if (!ids.length || batchDeleting.value) return
  const ok = await askConfirm({
    title: '批量删除任务记录',
    body: `将删除 ${ids.length} 条任务记录。删除后这些任务不再出现在列表中，任务步骤、工具调用、审批与安全审查记录仍全部保留，可从对话与详情继续追溯。`,
    confirmText: '删除',
    danger: true,
  })
  if (!ok) return
  batchDeleting.value = true
  let failed = 0
  for (const id of ids) {
    try {
      await api.deleteOpsTask(id)
    } catch {
      failed += 1
    }
  }
  batchDeleting.value = false
  records.value = records.value.filter((task) => !ids.includes(task.task_id))
  exitSelectMode()
  if (props.open) await loadRecords(true)
  if (failed) toast('bad', `${failed} 条任务记录删除失败，请刷新后重试`)
  else toast('ok', `已删除 ${ids.length} 条任务记录`)
}

function close(): void {
  emit('update:open', false)
}

function stopRequests(): void {
  listVersion++
  detailVersion++
  listController?.abort()
  detailController?.abort()
  loading.value = false
  detailLoading.value = false
  selected.value = undefined
  exitSelectMode()
}

const explain = (): void => {
  toast('info', '任务结论解释入口尚未接回本前端，可在完整控制台的助手页查看。')
}

watch(
  () => props.open,
  (open) => {
    if (open) {
      detailError.value = ''
      selected.value = undefined
      exitSelectMode()
      filter.value = props.initialFilter || 'all'
      void loadRecords(true)
    } else {
      stopRequests()
      emit('refreshNotice')
    }
  },
)

onUnmounted(stopRequests)
</script>

<template>
  <Teleport to="body">
    <div v-if="open" class="tr-scrim" @click="close"></div>
    <aside v-if="open" class="tr-drawer" role="dialog" aria-modal="true" aria-label="任务记录">
      <header class="tr-head">
        <div>
          <h2>任务记录</h2>
          <p class="tr-sub">查看本人已受理的任务，打开记录只读取已有状态与结果，不会重新执行诊断。</p>
        </div>
        <button class="icon-btn" title="关闭" @click="close"><Icon name="close" :size="16" /></button>
      </header>

      <div class="tr-body">
        <template v-if="selected">
          <div class="tr-toolbar">
            <button class="btn btn-sm" @click="backToRecords">返回任务列表</button>
            <button v-if="!unlinked(selected)" class="btn btn-sm" @click="emit('openSession', selected.session_id)">
              打开关联会话
            </button>
          </div>
          <p v-if="unlinked(selected)" class="tr-note">这项任务未关联到已保存的对话轮次，仍可在此查看已有结果。</p>
          <OpsTaskCard :key="selected.task_id" :initial="selected" historical @explain="explain" />
        </template>

        <template v-else>
          <div class="tr-toolbar" role="group" aria-label="筛选任务记录">
            <button
              v-for="option in ([['all', '全部任务'], ['unlinked', '未关联对话'], ['active', '未完成任务']] as const)"
              :key="option[0]"
              type="button"
              class="tr-filter"
              :class="{ 'is-on': filter === option[0] }"
              :aria-pressed="filter === option[0]"
              :disabled="batchDeleting"
              @click="chooseFilter(option[0])"
            >
              {{ option[1] }}
            </button>

            <button
              v-if="!selectMode"
              class="btn btn-sm tr-manage"
              :disabled="loading || batchDeleting || !selectableRecords.length"
              @click="enterSelectMode"
            >
              批量删除
            </button>
            <template v-else>
              <button class="tr-check" :class="{ 'is-on': allSelected }" :disabled="batchDeleting" @click="toggleSelectAll">
                <span class="tr-box" :class="{ 'is-on': allSelected, 'is-half': someSelected && !allSelected }">
                  <Icon v-if="allSelected" name="check" :size="10" />
                </span>
                全选可删任务
              </button>
              <button
                class="btn btn-sm tr-danger"
                :disabled="!selectedIDs.length || batchDeleting"
                @click="deleteSelected"
              >
                {{ batchDeleting ? '删除中…' : `删除${selectedIDs.length ? ` (${selectedIDs.length})` : ''}` }}
              </button>
              <button class="btn btn-sm" :disabled="batchDeleting" @click="exitSelectMode">取消</button>
            </template>

            <button class="btn btn-sm tr-refresh" :disabled="batchDeleting" @click="loadRecords(true)">
              {{ loading ? '刷新中…' : '刷新' }}
            </button>
          </div>

          <p v-if="selectMode" class="tr-note">
            仅已结束的任务可勾选；进行中的任务需等待结束或取消后再删除。删除只影响本列表，任务步骤、工具调用、审批与安全审查记录全部保留。
          </p>
          <p v-if="error || detailError" class="tr-error" role="alert">{{ error || detailError }}</p>
          <p v-if="detailLoading" class="tr-note" role="status">正在读取任务详情…</p>
          <p v-else-if="loading && !records.length" class="tr-note" role="status">正在读取任务记录…</p>
          <p v-else-if="!visibleRecords.length && !error" class="tr-note">
            {{ more ? '已加载记录中暂无匹配任务，可继续加载。' : '暂无符合条件的任务记录。' }}
          </p>

          <div v-for="task in visibleRecords" :key="task.task_id" class="tr-item">
            <span v-if="selectMode" class="tr-item-check" :class="{ 'is-locked': !canDeleteTask(task.status) }">
              <span
                class="tr-box"
                :class="{ 'is-on': selectedIDs.includes(task.task_id) }"
                role="checkbox"
                :aria-checked="selectedIDs.includes(task.task_id)"
                :aria-label="`选择任务：${task.intent || '运维任务'}`"
                @click.prevent="canDeleteTask(task.status) && toggleSelect(task.task_id)"
              >
                <Icon v-if="selectedIDs.includes(task.task_id)" name="check" :size="10" />
              </span>
            </span>
            <button
              type="button"
              class="tr-row"
              :class="{
                selecting: selectMode,
                checked: selectMode && selectedIDs.includes(task.task_id),
                locked: selectMode && !canDeleteTask(task.status),
              }"
              :disabled="detailLoading || batchDeleting"
              @click="chooseRow(task)"
            >
              <span class="tr-intent">{{ task.intent || '运维任务' }}</span>
              <span class="tr-state">{{ taskLabels[task.status] }}</span>
              <span class="tr-meta">
                <time :datetime="task.created_at">{{ stamp(task.created_at) }}</time>
                <span>{{ unlinked(task) ? '未关联对话' : '已关联会话' }}</span>
                <span class="tr-id">{{ task.task_id }}</span>
              </span>
            </button>
          </div>

          <button v-if="more" class="btn btn-sm tr-more" :disabled="detailLoading || batchDeleting" @click="loadRecords()">
            {{ loading ? '加载中…' : '加载更多记录' }}
          </button>
        </template>
      </div>
    </aside>
  </Teleport>
</template>

<style scoped>
.tr-scrim {
  position: fixed;
  inset: 0;
  background: rgba(11, 19, 34, 0.28);
  z-index: 90;
}

.tr-drawer {
  position: fixed;
  top: 0;
  right: 0;
  bottom: 0;
  width: min(960px, 96vw);
  background: var(--card);
  box-shadow: var(--shadow-3);
  z-index: 91;
  display: flex;
  flex-direction: column;
  animation: tr-in 0.22s cubic-bezier(0.22, 0.8, 0.32, 1);
}

@keyframes tr-in {
  from {
    transform: translateX(24px);
    opacity: 0.4;
  }
}

.tr-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 14px;
  padding: 16px 20px 12px;
  border-bottom: 1px solid var(--line);
}

.tr-head h2 {
  font-size: 15px;
  font-weight: 650;
  color: var(--ink);
}

.tr-sub {
  margin-top: 4px;
  font-size: 11.5px;
  color: var(--ink-4);
}

.tr-body {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: 14px 20px 24px;
}

.tr-toolbar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  margin-bottom: 12px;
}

.tr-refresh {
  margin-left: auto;
}

/* 窄屏时工具条会换行，按钮文字不能被压成两行 */
.tr-toolbar .btn,
.tr-filter,
.tr-check {
  white-space: nowrap;
}

.tr-filter {
  height: 26px;
  padding: 0 11px;
  border-radius: var(--r-md);
  border: 1px solid var(--line);
  background: var(--card);
  color: var(--ink-3);
  font-size: 11.5px;
}

.tr-filter:hover:not(:disabled) {
  border-color: var(--line-strong);
  color: var(--ink);
}

.tr-filter.is-on {
  border-color: var(--brand-line);
  background: var(--brand-soft);
  color: var(--brand-deep);
  font-weight: 600;
}

.tr-check {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  height: 26px;
  padding: 0 8px;
  font-size: 11.5px;
  color: var(--ink-2);
}

.tr-box {
  display: grid;
  place-items: center;
  width: 14px;
  height: 14px;
  border-radius: 3px;
  border: 1px solid var(--line-strong);
  background: #fff;
  color: #fff;
  flex: 0 0 14px;
  cursor: pointer;
}

.tr-box.is-on {
  background: var(--brand);
  border-color: var(--brand);
}

.tr-box.is-half {
  background: linear-gradient(90deg, var(--brand) 50%, #fff 50%);
  border-color: var(--brand);
}

.tr-danger {
  color: var(--bad-deep);
  border-color: #f0c7c2;
}

.tr-danger:hover:not(:disabled) {
  background: var(--bad-soft);
}

.tr-note {
  font-size: 11.5px;
  line-height: 1.8;
  color: var(--ink-4);
  margin: 6px 0;
}

.tr-error {
  font-size: 11.5px;
  color: var(--bad);
  margin: 6px 0;
}

.tr-item {
  display: flex;
  align-items: flex-start;
  border-bottom: 1px solid var(--line-soft);
}

.tr-item-check {
  flex: 0 0 auto;
  margin: 15px 0 0 2px;
}

.tr-item-check.is-locked {
  opacity: 0.4;
}

.tr-item-check.is-locked .tr-box {
  cursor: not-allowed;
}

.tr-row {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 6px 16px;
  flex: 1;
  min-width: 0;
  padding: 12px 8px;
  border: 0;
  background: transparent;
  color: var(--ink);
  font: inherit;
  text-align: left;
  border-radius: var(--r-sm);
}

.tr-row:hover {
  background: var(--card-soft);
}

.tr-row.selecting {
  cursor: pointer;
}

.tr-row.checked {
  background: var(--brand-soft);
}

.tr-row.locked {
  cursor: not-allowed;
}

.tr-row.locked .tr-intent,
.tr-row.locked .tr-state {
  color: var(--ink-4);
}

.tr-row:disabled {
  cursor: wait;
}

.tr-row:focus-visible,
.tr-filter:focus-visible,
.tr-box:focus-visible {
  outline: 2px solid var(--brand);
  outline-offset: 2px;
}

.tr-intent {
  font-size: 12.5px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.tr-state {
  font-size: 11.5px;
  color: var(--ink-3);
  white-space: nowrap;
}

.tr-meta {
  grid-column: 1 / -1;
  display: flex;
  flex-wrap: wrap;
  gap: 5px 14px;
  color: var(--ink-4);
  font-size: 10.5px;
  font-variant-numeric: tabular-nums;
}

.tr-id {
  overflow-wrap: anywhere;
}

.tr-more {
  margin-top: 14px;
}
</style>
