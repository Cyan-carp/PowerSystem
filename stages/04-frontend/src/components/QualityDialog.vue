<script setup lang="ts">
import { onMounted, ref } from 'vue'
import Icon from './Icon.vue'
defineProps<{ title: string }>()
const emit = defineEmits<{ close: [] }>()
const dialog = ref<HTMLDialogElement>()
onMounted(() => dialog.value?.showModal())
</script>
<template>
  <Teleport to="body">
    <dialog ref="dialog" :aria-label="title" @cancel.prevent="emit('close')" @click="($event.target === dialog) && emit('close')">
      <article>
        <header><h2>{{ title }}</h2><button class="icon-btn" aria-label="关闭" autofocus @click="emit('close')"><Icon name="close" :size="18" /></button></header>
        <slot />
      </article>
    </dialog>
  </Teleport>
</template>
<style scoped>
dialog { width:min(680px,calc(100% - 32px)); max-height:85dvh; padding:0; margin:auto; overflow:auto; background:var(--card); color:var(--ink); border:1px solid var(--line); border-radius:var(--r-lg); box-shadow:var(--shadow-3) }
dialog::backdrop { background:var(--overlay) }
article { padding:clamp(18px,3vw,32px) }
header { display:flex; align-items:center; justify-content:space-between; gap:12px; margin-bottom:20px }
h2 { font-size:22px; margin:0 }
</style>
