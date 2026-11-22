<script setup lang="ts">
import { onBeforeUnmount, ref, watch } from 'vue'
import { supplementary } from '../lib/api'
import { stamp } from '../lib/format'

const props = defineProps<{ deviceId?: number }>()
interface Link {
  state: string
  received_at: string | null
  confirmed_at: string | null
  delay_seconds: number | null
}
interface PlatformLink extends Link {
  known: boolean
  pending_in_window: number | null
  pending_outside_window: number | null
  observed_at: string
}
interface Coverage { day: string; covered_ms: number | null; gap_count: number | null; max_gap_ms: number | null }
const link = ref<Link>()
const platform = ref<PlatformLink>()
const coverage = ref<Coverage[]>([])
const error = ref('')
const loading = ref(false)
let timer: ReturnType<typeof setInterval> | undefined
let pending: AbortController | undefined
const states: Record<string, string> = {
  current: '链路正常', processing_delay: '平台处理延迟', offline: '设备离线',
  no_new_reports: '设备无新上报', unknown: '未知',
}

async function load() {
  if (pending || typeof document === 'undefined' || document.visibilityState === 'hidden') return
  const controller = new AbortController()
  pending = controller
  loading.value = true
  try {
    if (props.deviceId) {
      const response = await supplementary.read<{ link: Link; coverage: Coverage[] }>(
        `/devices/${props.deviceId}/telemetry/link-status`, controller.signal,
      )
      if (pending !== controller) return
      link.value = response.link
      coverage.value = response.coverage
    } else {
      const response = await supplementary.read<PlatformLink>('/dashboard/telemetry-link', controller.signal)
      if (pending !== controller) return
      link.value = platform.value = response
    }
    error.value = ''
  } catch {
    if (pending === controller) {
      link.value = platform.value = undefined
      coverage.value = []
      error.value = '链路暂不可读，请稍后刷新。'
    }
  } finally {
    if (pending === controller) {
      pending = undefined
      loading.value = false
    }
  }
}

function stop() {
  clearInterval(timer)
  pending?.abort()
  pending = undefined
}
watch(() => props.deviceId, () => {
  stop()
  link.value = platform.value = undefined
  coverage.value = []
  error.value = ''
  void load()
  if (typeof document !== 'undefined') timer = setInterval(load, 60_000)
}, { immediate: true })
onBeforeUnmount(stop)
</script>

<template>
  <section class="card link-panel">
    <header class="card-head">
      <h2 class="card-title">数据链路状态</h2>
      <button type="button" class="btn btn-sm" :disabled="loading" @click="load">刷新</button>
    </header>
    <div class="card-body">
      <p v-if="error" role="alert">{{ error }}</p>
      <p v-else-if="loading && !link" role="status">正在读取链路…</p>
      <dl v-else-if="link" class="link-values">
        <div><dt>当前状态</dt><dd>{{ states[link.state] ?? '未知' }}</dd></div>
        <div><dt>最近接收</dt><dd>{{ link.received_at ? stamp(link.received_at) : '未知' }}</dd></div>
        <div v-if="deviceId"><dt>最近确认入库</dt><dd>{{ link.confirmed_at ? stamp(link.confirmed_at) : '未知' }}</dd></div>
        <div><dt>{{ deviceId ? '处理延迟' : '活跃设备最大处理延迟' }}</dt><dd>{{ link.delay_seconds == null ? '未知' : `${link.delay_seconds} 秒` }}</dd></div>
        <div v-if="platform"><dt>全平台 7 天内待处理</dt><dd>{{ platform.known ? platform.pending_in_window : '未知' }}</dd></div>
        <div v-if="platform"><dt>全平台 7 天外滞留</dt><dd>{{ platform.known ? platform.pending_outside_window : '未知' }}</dd></div>
      </dl>
      <details v-if="coverage.length">
        <summary>近 7 个完整自然日的覆盖与缺口（北京时区）</summary>
        <ul class="coverage">
          <li v-for="item in coverage" :key="item.day">
            {{ item.day }} · 覆盖 {{ item.covered_ms == null ? '未知' : `${(item.covered_ms / 864000).toFixed(1)}%` }}
            · 缺口 {{ item.gap_count ?? '未知' }}
            <span v-if="item.gap_count && item.max_gap_ms != null">（最长 {{ Math.round(item.max_gap_ms / 1000) }} 秒）</span>
          </li>
        </ul>
      </details>
    </div>
  </section>
</template>

<style scoped>
.link-panel { margin-block: 12px; }
.link-values { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(140px, 100%), 1fr)); gap: 12px 16px; margin: 0; }
.link-values dt { color: var(--ink-3); font-size: 12px; }
.link-values dd { margin: 4px 0 0; font-weight: 600; }
details { margin-top: 14px; }
.coverage { padding-inline-start: 18px; }
</style>
