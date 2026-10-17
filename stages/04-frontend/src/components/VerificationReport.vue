<script setup lang="ts">
import { computed } from 'vue'
import { dataTime } from '../lib/boundary'
const props = defineProps<{ results: Record<string, unknown> }>()
function object(value: unknown): Record<string, unknown> { return value && typeof value === 'object' ? value as Record<string, unknown> : {} }
const rows = computed(() => Object.entries(props.results).map(([name, value]) => ({ name, result: object(value), data: object(object(value).data) })))
const metrics = [['voltage', '电压（V）'], ['current', '电流（A）'], ['temperature', '温度（℃）'], ['power', '功率（kW）'], ['fault_code', '故障码']]
function state(value: unknown): string { return value === 0 ? '停机（0）' : value === 1 ? '运行中（1）' : value === 2 ? '故障状态（2）' : '状态缺失或未知' }
function alarms(value: unknown): Record<string, unknown>[] { return Array.isArray(value) ? value.map(object) : [] }
function alarmStatus(value: unknown): string { return value === 'unhandled' ? '未确认' : value === 'acked' ? '已确认未恢复' : String(value) }
</script>
<template>
  <section v-for="row in rows" :key="row.name" class="verification-report">
    <strong>{{ row.name === 'latest_telemetry' ? '最新遥测' : row.name === 'current_alarms' ? '当前告警' : row.name === 'prediction' ? '预测' : '设备台账' }}</strong>
    <p v-if="row.result.status === 'error'" role="alert">查询失败：{{ row.result.error || row.data.error || '未取得数据' }}</p>
    <template v-else-if="row.name === 'latest_telemetry'">
      <p>接口运行状态：{{ state(row.data.status) }}。该字段不代表完整健康诊断。</p>
      <dl><template v-for="metric in metrics" :key="metric[0]"><dt>{{ metric[1] }}</dt><dd>{{ row.data[metric[0]!] ?? '缺失' }}</dd></template></dl>
    </template>
    <template v-else-if="row.name === 'current_alarms'">
      <p>{{ row.result.complete ? '已完整查询未确认及已确认未恢复的告警。' : '查询不完整，不能据此认定没有告警。' }} 已取得 {{ alarms(row.result.list).length }} 条。</p>
      <p v-for="(alarm, index) in alarms(row.result.list)" :key="index">#{{ alarm.id }} · {{ alarm.metric }} · {{ alarmStatus(alarm.status) }} · 触发 {{ dataTime(typeof alarm.triggered_at === 'string' ? alarm.triggered_at : undefined) }}</p>
      <p v-for="(error, index) in (Array.isArray(row.result.errors) ? row.result.errors : [])" :key="index" role="alert">{{ error }}</p>
    </template>
    <template v-else-if="row.name === 'prediction'">
      <dl><dt>预测概率</dt><dd>{{ row.data.probability ?? '缺失' }}</dd><dt>预测阈值</dt><dd>{{ row.data.threshold ?? '缺失' }}</dd><dt>接口风险等级</dt><dd>{{ row.data.risk_level === 'high' ? '高' : row.data.risk_level === 'low' ? '低' : '缺失' }}</dd><dt>模型版本</dt><dd>{{ row.data.model_version ?? '缺失' }}</dd></dl>
      <p>{{ row.data.stale === true ? '预测已过期' : row.data.stale === false ? '接口标记预测未过期' : '预测时效未知' }}；预测等级不替代现场诊断。</p>
    </template>
    <details><summary>展开原始接口结果</summary><pre>{{ JSON.stringify(row.result, null, 2) }}</pre></details>
  </section>
</template>
<style scoped>
.verification-report{margin:14px 0;padding:12px;border:1px solid var(--line);border-radius:4px;font-size:13px}.verification-report p{margin:8px 0;overflow-wrap:anywhere}.verification-report dl{display:grid;grid-template-columns:140px 1fr;gap:8px}.verification-report dd{margin:0;overflow-wrap:anywhere}.verification-report summary{cursor:pointer;color:var(--ink-3)}.verification-report pre{max-height:320px;overflow:auto;white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px}
</style>
