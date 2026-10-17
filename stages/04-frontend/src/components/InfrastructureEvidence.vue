<script setup lang="ts">
import type { InfrastructureEvidence } from '../types'
import { infrastructureNotice, infrastructureToolLabel, vantageLabel } from '../lib/infrastructure-evidence'
import { dateTime } from '../lib/format'
defineProps<{ evidence: InfrastructureEvidence[]; historical?: boolean }>()
function time(value?: string | null): string {
  if (!value) return '未提供'
  const stamp = new Date(value)
  return Number.isNaN(stamp.getTime()) ? value : `${dateTime(value)}（北京时间）`
}
</script>

<template>
  <section v-if="evidence.length" class="infra-evidence" aria-label="基础设施程序取证">
    <header><strong>基础设施程序取证</strong><span>与模型分析分别记录</span></header>
    <article v-for="item in evidence" :key="item.call_id" :data-call-id="item.call_id">
      <div class="infra-heading"><strong>{{ infrastructureToolLabel(item.tool) }}</strong><span :class="{ 'infra-failed': item.status === 'error' }">{{ item.status === 'ok' ? '已读取' : '读取失败' }} · {{ vantageLabel(item.vantage) }}</span></div>
      <p v-if="item.asset_id">{{ item.asset_name || item.asset_id }} · 资产版本 {{ item.asset_version }}</p>
      <p>采集时间 {{ time(item.collected_at) }}</p><p v-if="item.sampled_at">采样时间 {{ time(item.sampled_at) }}</p>
      <p v-if="infrastructureNotice(item, historical)" class="infra-notice">{{ infrastructureNotice(item, historical) }}</p>
      <p v-if="item.error" class="infra-failed">{{ item.error.code }} · {{ item.error.message }}</p>
      <details><summary>调用引用</summary><code>{{ item.evidence_ref }}</code></details>
    </article>
  </section>
</template>

<style scoped>
.infra-evidence{border:1px solid var(--line);border-radius:var(--r-lg);margin-top:12px;overflow:hidden}.infra-evidence header,.infra-heading{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap}.infra-evidence header{padding:12px;background:var(--bg)}.infra-evidence header span,.infra-heading span,.infra-evidence p,.infra-evidence details{font-size:12px;color:var(--ink-3)}.infra-evidence article{padding:12px;border-top:1px solid var(--line);overflow-wrap:anywhere}.infra-evidence p{margin:6px 0}.infra-evidence .infra-notice{color:var(--warning)}.infra-evidence .infra-failed{color:var(--danger)}.infra-evidence code{display:block;white-space:normal;margin-top:6px}
</style>
