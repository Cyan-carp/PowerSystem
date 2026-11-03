<script setup lang="ts">
import PredictionQuality from '../components/PredictionQuality.vue'
</script>

<template>
  <!-- 独立页面：模型质量关注「模型准不准、要不要换」，与线上故障预测是两件事。
       组件本身自带卡片布局与页签内状态，这里不再包一层容器，避免双层面板边框。 -->
  <PredictionQuality />
</template>
