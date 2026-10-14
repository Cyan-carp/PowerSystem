<script setup lang="ts">
import type { InspectionReport } from '../types'
defineProps<{ report: InspectionReport; historical?: boolean }>()
const statuses = { passed: '该项通过', finding: '发现异常', inconclusive: '无法判断', not_checked: '未检查' }
const categories: Record<string, string> = { host_services:'主机与服务', containers:'容器', database:'数据库', permissions_exposure:'权限与暴露', credentials_egress:'凭据与外发', audit_recovery:'审计与恢复' }
const reasons: Record<string,string> = { capability_unavailable:'现有采集能力不覆盖', inspection_interrupted:'巡检中断，尚未读取', registered_container_running_healthy:'登记容器运行且健康', container_not_running_or_unhealthy:'容器停止或健康异常', filtered_events_not_full_audit:'过滤摘要不代表完整审计', observation_only:'仅资源观察', insufficient_evidence:'仅有观察数据，证据不足以判断安全', empty_or_truncated:'证据为空或截断', incomplete_evidence:'证据不完整', historical_evidence:'历史取证', stale_or_future_evidence:'采样过期或时间异常', time_unknown:'缺少有效时间', asset_version_mismatch:'资产版本不一致', collection_failed:'采集失败', observed_abnormal_event:'观察到异常运行事件', health_not_sufficient:'健康信息不足', service_failed:'登记服务失败', registered_services_active:'登记服务正常运行', service_state_inconclusive:'服务状态无法判断', database_unreachable:'数据库不可达', registered_database_reachable:'登记数据库可达' }
function time(value: string | null): string { return value ? new Date(value).toLocaleString('zh-CN', { timeZone:'Asia/Shanghai' }) : '未取得' }
</script>
<template>
  <section class="inspection-report">
    <strong>{{ historical ? '历史巡检报告' : '本轮项目只读巡检报告' }}</strong>
    <p>请求范围：{{ report.requested_scope.join('、') || '尚未确定' }} · 已取证：{{ report.checked_scope.join('、') || '无' }}</p>
    <p class="limited">本报告只覆盖列出的项目、时点和规则；未检查及无法判断不代表通过，不作全项目安全结论。</p>
    <article v-for="item in report.items" :key="item.check_id" :data-check-status="item.status">
      <header><strong>{{ categories[item.category] || item.category }} · {{ item.asset_id || '本轮范围' }}</strong><span>{{ statuses[item.status] }}</span></header>
      <p>{{ reasons[item.reason] || item.reason }}</p>
      <p v-if="item.tool">{{ item.tool }} · 资产版本 {{ item.asset_version }} · {{ item.vantage }}视角</p>
      <p v-if="item.evidence_refs.length">证据：{{ item.evidence_refs.join('、') }} · {{ historical || item.historical ? '历史' : '本轮' }}</p>
      <p v-if="item.evidence_refs.length">采样 {{ time(item.sampled_at) }} · 采集 {{ time(item.collected_at) }}（北京时间）</p>
    </article>
    <p>报告复核：{{ report.review_status === 'completed' ? '已完成，独立意见见下方' : report.review_status === 'unavailable' ? '不可用，程序事实保留' : report.review_status === 'pending' ? '等待复核' : '尚未复核' }}</p>
    <p v-for="(opinion, index) in report.hypotheses" :key="index">安全模型意见 · {{ opinion.decision }} · {{ opinion.reason }}</p>
    <p v-for="(recommendation, index) in report.recommendations" :key="index">建议 · {{ recommendation }}</p>
  </section>
</template>
<style scoped>
.inspection-report{margin:12px 0;padding:12px;border:1px solid var(--brand-line);border-radius:10px;overflow-wrap:anywhere}.inspection-report p{font-size:13px;line-height:1.6}.inspection-report article{padding:10px 0;border-top:1px solid var(--brand-line)}.inspection-report header{display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap}.limited{color:var(--ink-3)}[data-check-status="finding"] span{color:var(--danger)}
</style>
