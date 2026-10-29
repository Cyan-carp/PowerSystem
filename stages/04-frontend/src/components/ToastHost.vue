<script setup lang="ts">
import Icon from './Icon.vue'
import { confirmState, dismissToast, settleConfirm, toasts } from '../lib/toast'

const onConfirmKey = (event: KeyboardEvent) => {
  if (event.key === 'Escape' && confirmState.open) settleConfirm(false)
}
</script>

<template>
  <div class="toast-stack" role="status" aria-live="polite">
    <div v-for="item in toasts" :key="item.id" class="toast" :class="`is-${item.kind}`">
      <span class="toast-dot"></span>
      <span class="toast-text">{{ item.text }}</span>
      <button class="toast-close" title="关闭" @click="dismissToast(item.id)">
        <Icon name="close" :size="12" />
      </button>
    </div>
  </div>

  <Teleport to="body">
    <div v-if="confirmState.open" class="confirm-layer">
      <div class="confirm-scrim" @click="settleConfirm(false)"></div>
      <div class="confirm-box" role="dialog" aria-modal="true" :aria-label="confirmState.title" @keydown="onConfirmKey">
        <h3>{{ confirmState.title }}</h3>
        <p>{{ confirmState.body }}</p>
        <div class="confirm-actions">
          <button class="btn" @click="settleConfirm(false)">{{ confirmState.cancelText }}</button>
          <button class="btn" :class="confirmState.danger ? 'btn-danger' : 'btn-primary'" @click="settleConfirm(true)">
            {{ confirmState.confirmText }}
          </button>
        </div>
      </div>
    </div>
  </Teleport>
</template>

<style scoped>
.toast-stack {
  position: fixed;
  left: 50%;
  top: 62px;
  transform: translateX(-50%);
  z-index: 400;
  display: flex;
  flex-direction: column;
  gap: 8px;
  align-items: center;
  pointer-events: none;
}

.toast {
  pointer-events: auto;
  display: flex;
  align-items: center;
  gap: 9px;
  max-width: min(520px, 90vw);
  padding: 9px 10px 9px 12px;
  border-radius: var(--r-md);
  border: 1px solid var(--line);
  background: var(--card);
  box-shadow: var(--shadow-2);
  font-size: 12.5px;
  color: var(--ink-2);
  animation: toast-in 0.18s ease;
}

@keyframes toast-in {
  from {
    opacity: 0;
    transform: translateY(-6px);
  }
}

.toast-dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  flex: 0 0 7px;
  background: var(--info);
}

.toast.is-ok .toast-dot {
  background: var(--ok);
}

.toast.is-warn .toast-dot {
  background: var(--warn);
}

.toast.is-bad .toast-dot {
  background: var(--bad);
}

.toast-text {
  min-width: 0;
  overflow-wrap: anywhere;
}

.toast-close {
  display: grid;
  place-items: center;
  width: 20px;
  height: 20px;
  border-radius: var(--r-sm);
  color: var(--ink-4);
  flex: 0 0 20px;
}

.toast-close:hover {
  background: var(--card-soft);
  color: var(--ink);
}

.confirm-layer {
  position: fixed;
  inset: 0;
  z-index: 500;
  display: grid;
  place-items: center;
}

.confirm-scrim {
  position: absolute;
  inset: 0;
  background: rgba(11, 19, 34, 0.32);
}

.confirm-box {
  position: relative;
  width: min(430px, 92vw);
  padding: 20px 22px 16px;
  border-radius: var(--r-lg);
  background: var(--card);
  box-shadow: var(--shadow-3);
}

.confirm-box h3 {
  font-size: 14px;
  font-weight: 650;
  color: var(--ink);
}

.confirm-box p {
  margin-top: 8px;
  font-size: 12.5px;
  line-height: 1.75;
  color: var(--ink-3);
  overflow-wrap: anywhere;
}

.confirm-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  margin-top: 18px;
}

.btn-danger {
  background: var(--bad);
  border-color: var(--bad);
  color: #fff;
}

.btn-danger:hover {
  background: var(--bad-deep);
  border-color: var(--bad-deep);
}
</style>
