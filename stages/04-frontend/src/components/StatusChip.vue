<script setup lang="ts">
withDefaults(defineProps<{ tone?: string; label: string; dot?: boolean }>(), {
  tone: 'idle',
  dot: true,
})
</script>

<template>
  <span class="chip" :class="`is-${tone}`">
    <i v-if="dot"></i>{{ label }}
  </span>
</template>
