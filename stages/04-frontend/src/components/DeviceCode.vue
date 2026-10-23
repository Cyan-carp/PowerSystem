<script setup lang="ts">
import { computed } from 'vue'
import { splitDeviceCode, stationTone } from '../lib/device-code'
const props = defineProps<{ code: string; station?: string }>()
const parts = computed(() => splitDeviceCode(props.code))
// The dot carries the station, so it must come from the ledger field and not
// from the code text: the same station always shows the same colour.
const tone = computed(() => stationTone(props.station || parts.value.station))
const constant = computed(() => [parts.value.station, parts.value.kind].filter(Boolean).join('-'))
</script>
<template>
  <span class="dev-code">
    <span class="dev-dot" :class="`dev-dot--${tone}`" :title="station || constant || code" />
    <template v-if="parts.seq">
      <span class="dev-const">{{ constant }}</span>
      <strong class="dev-seq">{{ parts.seq }}</strong>
    </template>
    <span v-else class="dev-plain">{{ code }}</span>
  </span>
</template>
