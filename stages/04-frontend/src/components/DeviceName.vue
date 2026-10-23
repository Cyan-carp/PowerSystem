<script setup lang="ts">
import { computed } from 'vue'
import { splitDeviceName } from '../lib/device-code'
const props = defineProps<{ name: string }>()
const parts = computed(() => splitDeviceName(props.name))
</script>
<template>
  <span class="dev-name">
    <template v-if="parts.seq">
      <span class="dev-const">{{ parts.prefix }}</span><strong class="dev-name-seq">{{ parts.seq }}</strong><span class="dev-const">号{{ parts.suffix }}</span>
    </template>
    <span v-else>{{ name }}</span>
  </span>
</template>
