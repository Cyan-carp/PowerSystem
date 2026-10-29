<script setup lang="ts">
import { onBeforeUnmount, onMounted } from 'vue'
import Icon from './Icon.vue'

defineProps<{ title: string; sub?: string }>()
const emit = defineEmits<{ close: [] }>()

const onKey = (event: KeyboardEvent) => {
  if (event.key === 'Escape') emit('close')
}

onMounted(() => document.addEventListener('keydown', onKey))
onBeforeUnmount(() => document.removeEventListener('keydown', onKey))
</script>

<template>
  <Teleport to="body">
    <div class="scrim" @click="emit('close')"></div>
    <aside class="drawer" role="dialog" aria-modal="true">
      <header class="drawer-head">
        <div>
          <h2>{{ title }}</h2>
          <p v-if="sub" class="sub">{{ sub }}</p>
        </div>
        <button class="icon-btn drawer-close" title="关闭" @click="emit('close')">
          <Icon name="close" :size="17" />
        </button>
      </header>
      <div class="drawer-body">
        <slot />
      </div>
      <footer v-if="$slots.footer" class="drawer-foot">
        <slot name="footer" />
      </footer>
    </aside>
  </Teleport>
</template>
