<script setup lang="ts">
import type { InfrastructureEvidence } from '../lib/ops-types'
import { infrastructureNotice, infrastructureToolLabel, vantageLabel } from '../lib/task-labels'
import { stamp } from '../lib/format'

defineProps<{ evidence: InfrastructureEvidence[]; historical?: boolean }>()

function time(value?: string | null): string {
  if (!value) return '未提供'
  const parsed = new Date(value)
  return Number.isNaN(parsed.getTime()) ? value : `${stamp(value)}（北京时间）`
}
</script>

<template>
  <section v-if="evidence.length" class="infra-evidence" aria-label="基础设施程序取证">
    <header><strong>基础设施程序取证</strong><span>与模型分析分别记录</span></header>
    <article v-for="item in evidence" :key="item.call_id" :data-call-id="item.call_id">
      <div class="infra-heading">
        <strong>{{ infrastructureToolLabel(item.tool) }}</strong>
        <span :class="{ 'is-failed': item.status === 'error' }">
          {{ item.status === 'ok' ? '已读取' : '读取失败' }} · {{ vantageLabel(item.vantage) }}
        </span>
      </div>
      <p v-if="item.asset_id">{{ item.asset_name || item.asset_id }} · 资产版本 {{ item.asset_version }}</p>
      <p>采集时间 {{ time(item.collected_at) }}</p>
      <p v-if="item.sampled_at">采样时间 {{ time(item.sampled_at) }}</p>
      <p v-if="infrastructureNotice(item, historical)" class="infra-notice">{{ infrastructureNotice(item, historical) }}</p>
      <p v-if="item.error" class="is-failed">{{ item.error.code }} · {{ item.error.message }}</p>
      <details><summary>调用引用</summary><code>{{ item.evidence_ref }}</code></details>
    </article>
  </section>
</template>

<style scoped>
.infra-evidence {
  border: 1px solid var(--line);
  border-radius: var(--r-md);
  margin-top: 12px;
  overflow: hidden;
}

.infra-evidence header,
.infra-heading {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}

.infra-evidence header {
  padding: 10px 12px;
  background: var(--card-soft);
}

.infra-evidence header span,
.infra-heading span,
.infra-evidence p,
.infra-evidence details {
  font-size: 11.5px;
  color: var(--ink-4);
}

.infra-evidence article {
  padding: 10px 12px;
  border-top: 1px solid var(--line);
  overflow-wrap: anywhere;
}

.infra-evidence p {
  margin: 5px 0;
}

.infra-evidence .infra-notice {
  color: var(--warn-deep);
}

.infra-evidence .is-failed {
  color: var(--bad);
}

.infra-evidence code {
  display: block;
  white-space: normal;
  margin-top: 5px;
}
</style>
