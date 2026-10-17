<script setup lang="ts">
import { computed } from 'vue'
import type { OpsTask } from '../types'
import { taskFactGroups } from '../lib/task-presentation'
const props = defineProps<{ task: OpsTask; historical?: boolean }>()
const groups = computed(() => taskFactGroups(props.task))
const statuses: Record<string, string> = { succeeded: '读取成功', failed: '读取失败', running: '执行中', pending: '待执行', skipped: '已跳过', cancelled: '已取消' }
</script>
<template>
  <section v-if="groups.length" class="task-facts" aria-label="程序事实">
    <h3>程序事实 <span>来自工具实际返回</span></h3>
    <p class="fact-boundary">{{ historical ? '历史取证，不代表当前状态。' : '仅代表本次采集时点。' }}只覆盖下列返回字段；未返回字段不作推断，台账返回条目不代表全项目资产总数。</p>
    <article v-for="group in groups" :key="group.step">
      <header><b>步骤 {{ group.step }} · {{ group.tool }}</b><span :class="{ failed: group.status === 'failed' }">{{ statuses[group.status] || group.status }}</span></header>
      <dl v-if="group.fields.length"><div v-for="(field, index) in group.fields" :key="index"><dt>{{ field.label }}</dt><dd>{{ field.value }}</dd></div></dl>
      <p v-else class="fact-boundary">此步骤没有可直接展示的已知摘要字段，完整返回见下方“程序执行步骤”。</p>
      <p v-if="group.limited" class="failed">证据不完整或已截断，不能作完整判断。</p>
      <details v-if="group.digest"><summary>运行镜像摘要</summary><code>{{ group.digest }}</code></details>
      <p v-if="group.collected" class="fact-boundary">采集于 {{ group.collected }}（北京时间）</p>
    </article>
  </section>
</template>
<style scoped>
.task-facts{margin:20px 0}.task-facts h3{font-size:16px;margin:0 0 8px}.task-facts h3 span{font-size:12px;font-weight:400;color:var(--ink-3);margin-left:10px}
.fact-boundary{font-size:12px;line-height:1.7;color:var(--ink-3);margin:8px 0}.task-facts article{border:1px solid var(--line);border-radius:4px;padding:14px 16px;margin-top:10px;background:var(--bg)}
.task-facts header{display:flex;flex-wrap:wrap;justify-content:space-between;gap:8px;font-size:12px;color:var(--ink-2)}.task-facts header span{color:var(--brand)}
.task-facts dl{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:16px;margin:16px 0 8px}.task-facts dt{font-size:12px;color:var(--ink-3);margin-bottom:6px}.task-facts dd{margin:0;font-size:15px;font-weight:600;overflow-wrap:anywhere;font-variant-numeric:tabular-nums}
.task-facts details{font-size:12px;color:var(--ink-3);margin-top:12px}.task-facts code{display:block;overflow-wrap:anywhere;padding-top:8px}.task-facts .failed{color:var(--danger)}
@media(max-width:900px){.task-facts dl{grid-template-columns:repeat(2,minmax(0,1fr))}}@media(max-width:600px){.task-facts dl{grid-template-columns:1fr}}
</style>
