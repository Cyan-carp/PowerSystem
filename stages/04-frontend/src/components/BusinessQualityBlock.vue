<script setup lang="ts">
import type { BusinessQuality } from '../lib/ops-types'

defineProps<{ quality?: BusinessQuality | null; historical?: boolean }>()

const states: Record<string, string> = {
  not_queried: '未取得业务证据',
  failed: '业务查询失败',
  partial_failed: '部分查询失败',
  queried: '已取得业务证据',
}
</script>

<template>
  <section v-if="quality" class="business-quality" aria-label="业务结论程序校验">
    <strong>{{ historical || quality.scope === 'historical' ? '历史事实，仅说明当时记录' : states[quality.state] }}</strong>
    <span> · {{ quality.render_state === 'validated' ? '事实引用已校验' : '已降级为程序事实' }} · {{ quality.version }}</span>
    <details>
      <summary>结构化事实与调用引用 · {{ quality.facts.length }}</summary>
      <div v-for="fact in quality.facts" :key="fact.id">
        <p><code>{{ fact.id }}</code> · {{ fact.text }}</p>
        <small>{{ fact.call_refs.join(' · ') }}</small>
      </div>
      <p v-for="missing in quality.missing" :key="missing">证据不足：{{ missing }}</p>
      <small>摘要 {{ quality.digest }}</small>
    </details>
  </section>
</template>

<style scoped>
.business-quality {
  padding: 10px 12px;
  margin: 12px 0;
  background: var(--card-soft);
  border: 1px solid var(--line);
  border-radius: var(--r-md);
  font-size: 11.5px;
  line-height: 1.75;
  color: var(--ink-2);
  overflow-wrap: anywhere;
}

.business-quality > span,
.business-quality small {
  color: var(--ink-4);
}

.business-quality summary {
  cursor: pointer;
}

.business-quality p {
  margin: 6px 0;
}
</style>
