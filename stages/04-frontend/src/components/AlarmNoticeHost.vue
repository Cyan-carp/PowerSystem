<script setup lang="ts">
import { RouterLink } from 'vue-router'
import Icon from './Icon.vue'
import { alarmNotices, dismissAlarmNotice } from '../lib/alarm-notices'
import { LEVEL_CN, METRIC_CN } from '../lib/format'
</script>

<template>
  <aside v-if="alarmNotices.length" class="alarm-notices" aria-label="新告警提醒" aria-live="assertive">
    <article v-for="alarm in alarmNotices" :key="alarm.id" class="alarm-notice" :class="alarm.level" role="alert">
      <div class="notice-title">
        <Icon name="alert" :size="16" />
        <strong>{{ LEVEL_CN[alarm.level] }}告警</strong>
        <button type="button" class="notice-close" aria-label="关闭告警提醒" @click="dismissAlarmNotice(alarm.id)">
          <Icon name="close" :size="15" />
        </button>
      </div>
      <p>设备 #{{ alarm.device_id }} · {{ METRIC_CN[alarm.metric] || alarm.metric }}</p>
      <p class="notice-value">当前值 {{ alarm.value }} · 规则阈值 {{ alarm.threshold }}</p>
      <RouterLink :to="{ path: '/alarms', query: { id: String(alarm.id) } }" @click="dismissAlarmNotice(alarm.id)">查看并处理告警 <span aria-hidden="true">→</span></RouterLink>
    </article>
  </aside>
</template>

<style scoped>
.alarm-notices { position: fixed; right: 24px; bottom: 24px; z-index: 1200; display: grid; gap: 12px; width: min(360px, calc(100vw - 32px)); max-height: calc(100vh - 48px); overflow-y: auto; }
.alarm-notice { border: 1px solid var(--line); border-left: 3px solid var(--warning, #ba762c); border-radius: 12px; background: var(--surface, #fff); padding: 16px 18px; color: var(--ink); box-shadow: 0 8px 24px #00000012; font-size: 13px; }
.alarm-notice.urgent { border-left-color: var(--danger, #bb4b3e); }
.notice-title { display: flex; align-items: center; gap: 8px; }
.notice-title strong { font-size: 14px; }
.notice-close { margin-left: auto; display: grid; place-items: center; border: 0; background: transparent; color: var(--ink-3); cursor: pointer; padding: 4px; }
.alarm-notice p { margin: 10px 0 0; }
.alarm-notice .notice-value { margin-top: 6px; color: var(--ink-3); }
.alarm-notice a { display: inline-block; margin-top: 14px; color: var(--brand); font-weight: 600; }
@media (max-width: 600px) { .alarm-notices { right: 16px; bottom: 16px; } }
</style>
