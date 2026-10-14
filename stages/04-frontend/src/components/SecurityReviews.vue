<script setup lang="ts">
import type { SecurityReview } from '../types'
defineProps<{ reviews: SecurityReview[]; historical?: boolean }>()
const kinds: Record<string, string> = { sensitive_read: '敏感读取', data_egress: '数据外发', write_plan: '审批前计划', write_preflight: '执行前复核', post_execution: '动作后审', inspection_report: '巡检报告' }
const states: Record<string, string> = { pending: '正在审查', completed: '审查完成', unavailable: '审查不可用', invalid: '审查无效' }
const decisions: Record<string, string> = { allow: '允许继续程序门禁', deny: '安全否决', needs_clarification: '需要补充信息' }
function time(value?: string | null): string { return value ? new Date(value).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai' }) : '—' }
</script>

<template>
  <section v-if="reviews.length" class="security-reviews" aria-label="安全审查">
    <strong>安全审查{{ historical ? ' · 历史记录' : '' }}</strong>
    <p class="muted">允许只表示可继续程序校验；实际执行仍需有效身份、目标、审批及写窗口。后审结果不改写已发生动作。</p>
    <article v-for="review in reviews" :key="review.review_id" :class="{ blocked: review.decision === 'deny' || review.state === 'invalid' }">
      <p><b>{{ kinds[review.kind] || review.kind }}</b> · {{ states[review.state] || review.state }}<span v-if="review.decision"> · {{ decisions[review.decision] }}</span></p>
      <p v-if="review.reason">{{ review.reason }}</p>
      <p v-if="review.error_code">{{ review.error_code }}</p>
      <small>步骤 {{ review.step_no }} · 代次 {{ review.execution_epoch }} · {{ time(review.created_at) }}</small>
      <details><summary>审查依据与有效期</summary><p>策略 {{ review.policy_version }}</p><p>规则 {{ review.rule_hits.join('、') || '—' }}</p><p>证据 {{ review.evidence_refs.join('、') || '—' }}</p><p>有效至 {{ time(review.expires_at) }}；过期后执行前需要重新审查。</p><small>{{ review.review_id }}</small></details>
    </article>
  </section>
</template>

<style scoped>
.security-reviews{border:1px solid var(--brand-line);border-radius:10px;padding:12px;margin:12px 0;background:var(--card);overflow-wrap:anywhere}.security-reviews p{font-size:13px;line-height:1.6}.security-reviews article{padding:8px 0;border-top:1px solid var(--brand-line)}.muted,.security-reviews small{color:var(--ink-3);font-size:12px}.blocked{color:var(--danger)}
</style>
