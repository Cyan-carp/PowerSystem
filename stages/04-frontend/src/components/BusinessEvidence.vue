<script setup lang="ts">
import type { RAGEvidence } from '../types'
import { dataTime, evidenceLabel, freshnessLabel } from '../lib/boundary'
defineProps<{ evidence: RAGEvidence; historical?: boolean }>()
</script>
<template>
  <section class="business-evidence" aria-label="平台取证情况">
    <strong>{{ historical ? '该记录的取证情况' : '本轮取证情况' }} · {{ evidenceLabel(evidence.state) }}</strong>
    <p v-if="historical">这是历史记录，打开会话不会重新查询。以下数据时效按该记录的查询时刻计算。</p>
    <p v-if="evidence.state !== 'not_queried'">成功 {{ evidence.success_count }} 次，失败 {{ evidence.failed_count }} 次。查询成功仅说明取得接口结果，不代表正文全部正确或设备正常。</p>
    <p v-else>未重新核验平台事实；需要新数据时可选择补查。</p>
    <details v-if="evidence.cards.length"><summary>查看数据时间与原始值</summary>
      <dl v-for="(card, index) in evidence.cards" :key="index">
        <dt>{{ card.tool }} · {{ card.device_code || (card.device_id ? `设备 ID ${card.device_id}` : '平台数据') }} · {{ card.status }}</dt>
        <dd>查询时间：{{ dataTime(card.queried_at) }}<small>原值：{{ card.queried_at || '缺失' }}</small></dd>
        <template v-if="['get_telemetry', 'get_prediction'].includes(card.tool)">
          <dd>{{ card.tool === 'get_prediction' ? '预测窗口时间' : '采样时间' }}：{{ dataTime(card.sampled_at) }}<small>原值：{{ card.sampled_at || '缺失' }} · ts_ms：{{ card.ts_ms ?? '不适用或缺失' }}</small></dd>
          <dd v-if="card.tool === 'get_telemetry'">接收时间：{{ dataTime(card.received_at) }}<small>原值：{{ card.received_at || '缺失' }}</small></dd>
          <dd>{{ historical ? '查询当时：' : '' }}{{ freshnessLabel(card.freshness) }}</dd>
        </template>
        <dd v-if="card.error">错误：{{ card.error }}</dd>
      </dl>
    </details>
  </section>
</template>
<style scoped>
.business-evidence{border:1px solid var(--line);border-radius:4px;padding:12px;margin:14px 0;color:var(--ink-2);font-size:13px}.business-evidence p{margin:7px 0}.business-evidence summary{cursor:pointer}.business-evidence dl{padding:8px 0;border-top:1px solid var(--line)}.business-evidence dt{font-weight:600}.business-evidence dd{margin:6px 0;overflow-wrap:anywhere}.business-evidence small{display:block;color:var(--ink-3)}
</style>
