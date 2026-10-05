<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { guides, guideVersion } from '../lib/page-help'
const route = useRoute()
const router = useRouter()
const open = ref(false)
const guide = computed(() => guides[String(route.name)])
watch(() => route.name, () => { open.value = false })
function ask(): void {
  const question = guide.value?.question
  open.value = false
  if (question) void router.push({ name: 'agent-chat', query: { question } })
}
</script>
<template>
  <div v-if="guide" class="page-help">
    <el-button text @click="open = !open" :aria-expanded="open">页面帮助</el-button>
    <section v-if="open" class="help-box" aria-label="页面操作指南">
      <header><strong>{{ guide.title }} · 操作指南</strong><el-button text @click="open = false">关闭</el-button></header>
      <p>{{ guide.purpose }}</p>
      <ol><li v-for="step in guide.steps" :key="step">{{ step }}</li></ol>
      <p v-for="note in guide.notes" :key="note" class="help-note">{{ note }}</p>
      <footer><small>{{ guideVersion }} · 登录用户可读演示设备</small><el-button @click="ask">带入问答</el-button></footer>
    </section>
  </div>
</template>
<style scoped>
.page-help { position: relative; }
.help-box { position: absolute; top: 42px; right: 0; width: min(440px, calc(100vw - 32px)); z-index: 2100; padding: 18px; background: white; color: #243746; border: 1px solid #ccd6dc; border-top: 3px solid #087f78; }
header, footer { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
header { border-bottom: 1px solid #dce2e6; padding-bottom: 8px; }
p, li { line-height: 1.65; font-size: 13px; }
.help-note { border-left: 3px solid #b8872a; padding-left: 10px; }
small { color: #627581; }
</style>
