<script setup lang="ts">
import { computed, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { Close, QuestionFilled } from '@element-plus/icons-vue'
import { guides, guideVersion } from '../lib/page-help'
import { closeInspector, isInspectorOpen, toggleInspector } from '../lib/inspector'

const route = useRoute()
const router = useRouter()
const open = computed(() => isInspectorOpen('help'))
const guide = computed(() => guides[String(route.name)])

watch(() => route.name, () => { if (open.value) closeInspector() })

function ask(): void {
  const question = guide.value?.question
  closeInspector()
  if (question) void router.push({ name: 'agent-chat', query: { question } })
}
</script>

<template>
  <button
    v-if="guide" class="dock-trigger" :class="{ on: open }" type="button"
    :aria-expanded="open" aria-controls="inspector-dock"
    @click="toggleInspector('help')"
  >
    <el-icon><QuestionFilled /></el-icon>
    <span>指南</span>
  </button>
  <Teleport defer to="#inspector-dock">
    <section v-if="open && guide" class="inspector" aria-label="页面操作指南">
      <header class="inspector-head">
        <div class="inspector-heading">
          <p class="inspector-eyebrow">页面指南 · {{ guideVersion }}</p>
          <h2>{{ guide.title }}</h2>
        </div>
        <button class="icon-btn" type="button" aria-label="关闭指南" @click="closeInspector">
          <el-icon><Close /></el-icon>
        </button>
      </header>
      <div class="inspector-body">
        <p class="guide-lead">{{ guide.purpose }}</p>
        <ol class="guide-steps">
          <li v-for="(step, index) in guide.steps" :key="step">
            <span class="guide-num">{{ String(index + 1).padStart(2, '0') }}</span>
            <p>{{ step }}</p>
          </li>
        </ol>
        <div v-if="guide.notes?.length" class="guide-notes">
          <p v-for="note in guide.notes" :key="note"><span class="guide-mark">!</span>{{ note }}</p>
        </div>
      </div>
      <footer class="inspector-foot">
        <el-button type="primary" @click="ask">带入问答</el-button>
        <span class="foot-spacer" />
        <span class="foot-meta">登录用户可读演示设备</span>
      </footer>
    </section>
  </Teleport>
</template>

<style scoped>
.guide-lead {
  margin: 0 0 20px; padding-bottom: 16px; border-bottom: 1px solid var(--line);
  font-size: 13px; line-height: 1.85; color: var(--ink-2);
}
.guide-steps { list-style: none; margin: 0; padding: 0; }
.guide-steps li { position: relative; display: grid; grid-template-columns: 30px minmax(0, 1fr); gap: 12px; padding-bottom: 16px; }
.guide-steps li::before {
  content: ""; position: absolute; left: 14px; top: 24px; bottom: 4px; width: 1px; background: var(--line);
}
.guide-steps li:last-child { padding-bottom: 0; }
.guide-steps li:last-child::before { display: none; }
.guide-num {
  display: grid; place-items: center; width: 29px; height: 20px; border-radius: 4px;
  background: var(--card-2); border: 1px solid var(--line);
  font-family: var(--font-num); font-size: 10.5px; color: var(--ink-2); font-variant-numeric: tabular-nums;
}
.guide-steps p { margin: 0; font-size: 12.5px; line-height: 1.75; color: var(--ink-2); }
.guide-notes { margin-top: 22px; display: grid; gap: 8px; }
.guide-notes p {
  display: flex; gap: 9px; margin: 0; padding: 10px 12px; border-radius: 8px;
  background: var(--warning-soft); color: #8a5300; font-size: 11.5px; line-height: 1.7;
}
.guide-mark {
  flex: none; display: grid; place-items: center; width: 15px; height: 15px; margin-top: 1px;
  border-radius: 50%; background: var(--warning); color: #fff; font-size: 10px; font-weight: 700;
}
</style>
