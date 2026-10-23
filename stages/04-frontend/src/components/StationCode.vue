<script setup lang="ts">
import { computed } from 'vue'
import { stationTone } from '../lib/device-code'
const props = defineProps<{ code: string }>()
const tone = computed(() => stationTone(props.code))
</script>
<template>
  <span class="dev-code"><span class="dev-dot" :class="`dev-dot--${tone}`" />{{ code }}</span>
</template>
